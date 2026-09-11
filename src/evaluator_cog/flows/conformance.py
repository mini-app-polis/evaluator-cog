"""Conformance checking flow for evaluator-cog.

A single parameterized flow (conformance_check_flow) handles both modes:

run_llm=False (default, daily schedule):
  Runs deterministic rule checks only. No LLM calls. No token cost.
  Posts findings with source='conformance_deterministic'.
  run_id prefix: 'deterministic-{version}-{uuid}'

run_llm=True (triggered manually or via Prefect automation, weekly):
  Runs deterministic pass first to get checked_rule_ids, then calls
  the LLM for soft-rule assessment. Posts LLM findings only.
  Posts findings with source='conformance_llm'.
  run_id prefix: 'conformance-{version}-{uuid}'

Both modes additionally run applies_to-absent checks once per invocation:
  EVAL-003 and MONO-003 post with source='data_quality' (runtime
  data-quality on stored findings and on the ecosystem inventory).
  EVAL-007 posts with source='standards_drift' (catalog vs evaluator).
"""

from __future__ import annotations

import datetime
import io
import os
import shutil
import tempfile
import time
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import yaml
from mini_app_polis import logger as logger_mod
from mini_app_polis.pipeline_status import RunReport, make_failure_hook
from prefect import flow, get_run_logger
from prefect.concurrency.sync import concurrency

from evaluator_cog.engine.api_client import PostResult, post_findings
from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.evaluator_config import EvaluatorConfig, load_evaluator_config
from evaluator_cog.engine.llm import (
    _anthropic_messages_create,
    _parse_findings_from_claude,
    build_conformance_prompt,
)

log = logger_mod.get_logger()

_ECOSYSTEM_YAML_URL = "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/ecosystem.yaml"

#: Where the compiled standards catalog is served.
#:
#: Always production, and deliberately NOT resolved through
#: ``KAIANO_API_BASE_URL`` / ``KAIANO_API_BASE_URL_DEV``. Catalogs are
#: published only from ecosystem-standards' release job on ``main``, so the
#: development API's store is empty — a dev evaluation pointed there would
#: get ``no_catalog_published`` and read it as the catalog being broken.
#: Rule text is not environment-specific; there is nothing to separate.
#:
#: Overridable by environment for a local API or a pinned version, which is
#: the only reason this is not a bare constant.
_STANDARDS_CATALOG_URL = os.environ.get(
    "ECOSYSTEM_STANDARDS_CATALOG_URL",
    "https://api.kaianolevine.com/v1/standards/catalog",
)

#: Identifies this client to the API's edge. Cloudflare's browser integrity
#: check rejects unidentified automation, and the path that fetches every
#: rule must not be where that is discovered.
_USER_AGENT = (
    "evaluator-cog/conformance (+https://github.com/mini-app-polis/evaluator-cog)"
)

_VALID_RULE_STATUSES: frozenset[str] = frozenset({"requirement", "convention", "gap"})

#: GitHub org every registry entry resolves under unless it declares its own.
_DEFAULT_ORG = "mini-app-polis"


# Accumulates every post_findings outcome in one flow invocation.
#
# A run that computes findings and delivers none of them is a systemic
# fault — no route, no credential, no service — not N unlucky findings,
# and it must fail the flow rather than log a warning. Before this, the
# 2026-09-03 runs computed ~162 findings across 13 repos, posted zero,
# and still finished Completed with a green Healthchecks ping, because
# post_findings swallowed every error and the "posted N findings" log
# line reported the length of the list handed over rather than what the
# API accepted.
_RUN_TALLY = PostResult()

#: The catalog for this flow run. One fetch, reused by every caller, reset
#: at the start of each run so a long-lived worker picks up a new release
#: rather than grading against whatever was current when it booted.
_CATALOG: dict | None = None

#: Coverage, as opposed to delivery. _RUN_TALLY answers "did the findings
#: reach the API"; this answers "was every declared repo actually looked
#: at, and looked at completely". A run can be perfect on the first and
#: wrong on the second, which is what a green SUCCESS on a run that
#: silently skipped three repos looks like.
#:
#: None until a flow run resets it, so the helpers below are no-ops when
#: this module's functions are called outside a run (tests, one-off
#: scripts) rather than writing into a report nobody will send.
_RUN_REPORT: RunReport | None = None

#: Every repo name flagged by :func:`_report_issue` this run, so the
#: message can say how many repos came through clean without counting a
#: flagged one twice when two things went wrong with it.
#:
#: Holds whatever string the call site had — a declared service id in most
#: places, a monorepo repo name in :func:`_download_repo`. Only the
#: intersection with declared service ids is ever counted, so the entries
#: that name no service are ignored rather than skewing the total.
_RUN_FLAGGED: set[str] = set()

#: Registry entries whose download returned 404 this run, as
#: ``{"label": "<org>/<repo>", "url": <zipball url>}``. Only a 404 lands
#: here: a 403, 429, 5xx or timeout means the run could not tell whether
#: the repo exists, which is not the same fact and must not be reported
#: as one. XSTACK-008 reads this at the end of the run.
_UNRESOLVED_DOWNLOADS: list[dict[str, str]] = []


def _reset_run_tally() -> None:
    """Start a fresh tally and coverage report. Called at the top of each run."""
    global _CATALOG
    _CATALOG = None
    global _RUN_TALLY, _RUN_REPORT, _RUN_FLAGGED
    _RUN_TALLY = PostResult()
    _RUN_REPORT = RunReport(flow_name="conformance-check", repo=_REPO)
    _RUN_FLAGGED = set()
    _UNRESOLVED_DOWNLOADS.clear()


def _report_issue(reason: str, repo_id: str, exc: BaseException | None = None) -> None:
    """Flag a repo this run did not fully evaluate. Makes the run WARN."""
    if _RUN_REPORT is None:
        return
    detail = f"{type(exc).__name__}: {exc}" if exc is not None else None
    _RUN_REPORT.issue(reason, repo_id, detail=detail)
    _RUN_FLAGGED.add(repo_id)


def _report_note(reason: str, repo_id: str) -> None:
    """Record an ordinary skip. Counted in the message, severity unchanged."""
    if _RUN_REPORT is not None:
        _RUN_REPORT.note(reason, repo_id)


def _post_tracked(label: str, prefect_log: Any = None, **kwargs: Any) -> PostResult:
    """post_findings + accumulate + log what the API actually accepted.

    ``label`` names the emitter (a rule id, or a repo) so a partial
    failure says which one. The logged number is ``result.posted``, never
    ``len(findings)`` — reporting the size of the list you handed over is
    how a total outage came to be logged as success three times in one
    run.

    Pass ``prefect_log`` wherever a run logger is in scope. Omitting it
    falls back to the shared-library logger, which reaches the service's
    stdout but not the Prefect run view — so a caller that omits it goes
    quiet in the window an operator is actually watching, while the
    callers around it keep reporting. Every call site in this module
    passes it; the default exists only for callers with no run context.
    """
    emit = prefect_log if prefect_log is not None else log
    result = post_findings(**kwargs)
    _RUN_TALLY.merge(result)
    if result.posted:
        emit.info("%s: posted %d findings", label, result.posted)
    if result.duplicates:
        # This is why deejay-cog and evaluator-cog looked absent from the
        # 6.9.1 run rather than suppressed: each computed exactly one
        # finding, that finding was dropped as a duplicate, and nothing
        # here logged it. posted was 0 and failed was 0, so the repo
        # produced no line at all and read as though it had never been
        # processed. A dropped finding is a decision and belongs in the
        # run view, not only in stdout.
        emit.warning(
            "%s: %d of %d findings suppressed as duplicates — %s",
            label,
            result.duplicates,
            result.duplicates + result.attempted,
            "; ".join(result.duplicate_details) or "no detail recorded",
        )
    if result.failed:
        emit.warning(
            "%s: %d of %d findings failed to POST — last error: %s",
            label,
            result.failed,
            result.attempted,
            result.last_error,
        )
    return result


class FindingDeliveryError(RuntimeError):
    """Raised when a run computed findings and delivered none of them."""


def _assert_findings_were_delivered(prefect_log: Any) -> None:
    """Fail the flow when nothing reached the API.

    Raising is the point. Prefect marks the run Failed, the flow's
    failure hooks fire, and ``_on_completion`` does not run — so
    Healthchecks.io is not pinged green for a run that delivered
    nothing. A partial failure has already been warned about per
    emitter and does not fail the run.
    """
    if not _RUN_TALLY.total_failure:
        return
    raise FindingDeliveryError(
        f"{_RUN_TALLY.attempted} findings were computed and none reached "
        f"api-kaianolevine-com. The evaluation itself ran; delivery did "
        f"not. Check KAIANO_API_BASE_URL and EVALUATOR_COG_API_KEY on "
        f"this service. Last error: {_RUN_TALLY.last_error}"
    )


def _on_completion(flow, flow_run, state) -> None:
    """Ping Healthchecks.io after successful conformance run. Never raises."""
    import urllib.request

    url = os.getenv("HEALTHCHECKS_URL_EVALUATOR", "").strip()
    if not url:
        return
    with suppress(Exception):
        _timeout = int(os.environ.get("EVALUATOR_HEALTHCHECK_TIMEOUT_SECONDS", "10"))
        urllib.request.urlopen(url, timeout=_timeout)


def _fetch_yaml(url: str) -> dict:
    """Fetch and parse a YAML file from a URL. Never raises — returns {} on failure."""
    timeout = float(os.environ.get("EVALUATOR_HTTP_TIMEOUT_SECONDS", "20"))
    try:
        r = httpx.get(url, timeout=timeout)
        r.raise_for_status()
        return yaml.safe_load(r.text) or {}
    except Exception as exc:
        log.warning("conformance: failed to fetch %s: %s", url, exc)
        return {}


def _fetch_catalog() -> dict:
    """Fetch the compiled standards catalog. Cached for the flow run.

    One request replaces the index, every domain file and package.json —
    and replaces deriving at runtime the structure the compiler already
    resolved.

    **Raises on failure, deliberately.** The functions this replaced each
    returned partial data on a fetch error so a run could limp on. With a
    single source that is the wrong trade: an empty catalog means zero
    rules, which means zero findings, which is indistinguishable from a
    clean fleet. A run that could not read the rules has evaluated nothing
    and must fail rather than report success.
    """
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG
    timeout = float(os.environ.get("EVALUATOR_HTTP_TIMEOUT_SECONDS", "20"))
    try:
        response = httpx.get(
            _STANDARDS_CATALOG_URL,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        # One failure type for every way this can go wrong, naming the
        # address. A transport error, a 5xx and an HTML challenge page are
        # different problems with the same consequence — no rules — and the
        # caller needs the address to tell them apart.
        raise RuntimeError(
            f"Cannot read the standards catalog at {_STANDARDS_CATALOG_URL}: {exc}"
        ) from exc
    catalog = body.get("data") if isinstance(body, dict) else None
    if not isinstance(catalog, dict) or not catalog.get("rules"):
        raise RuntimeError(
            f"Standards catalog at {_STANDARDS_CATALOG_URL} returned no rules"
        )
    _CATALOG = catalog
    return catalog


def _catalog_rules() -> list[dict]:
    """Every rule the evaluator will consider.

    ``checkable: false`` rules are filtered here. The catalog carries them
    so they stay readable and joinable to the findings of versions that did
    check them, but there is no check to run and nothing is emitted for
    them — see the ``gap`` status in index.yaml.
    """
    return [rule for rule in _fetch_catalog()["rules"] if rule.get("checkable")]


def _get_standards_version() -> str:
    """The version of the catalog under evaluation. Raises on failure."""
    version = str(_fetch_catalog().get("version") or "")
    if not version:
        raise RuntimeError("Standards catalog carries no version")
    return version


def _get_active_repos(ecosystem: dict) -> list[dict]:
    """Return all active services from ecosystem.yaml."""
    services = ecosystem.get("services", [])
    return [s for s in services if s.get("status") == "active"]


def _get_monorepos(ecosystem: dict) -> dict[str, dict]:
    """
    Return a dict of {monorepo_id: monorepo_record} from ecosystem.yaml.
    Keys match the `monorepo` field on service entries.
    """
    return {m["id"]: m for m in ecosystem.get("monorepos", []) if m.get("id")}


def _read_workspace_package_json(monorepo_root: Path) -> str:
    """
    Read the workspace root package.json text for XSTACK-001 monorepo check.
    Returns empty string if not found.
    """
    pkg = monorepo_root / "package.json"
    if pkg.exists():
        try:
            return pkg.read_text().lower()
        except Exception:
            pass
    return ""


def _fetch_catalog_schema() -> dict:
    """Traits, repo types and statuses, in the shapes the dispatcher expects.

    The catalog carries these already resolved; this only reshapes them.
    """
    catalog = _fetch_catalog()
    schema = catalog.get("schema") or {}

    raw_traits = schema.get("traits") or {}
    traits: dict[str, dict] = {}
    if isinstance(raw_traits, dict):
        for name, body in raw_traits.items():
            if not isinstance(body, dict):
                continue
            traits[str(name)] = {
                "description": str(body.get("description") or "").strip(),
                "exempts": [
                    str(r) for r in (body.get("exempts") or []) if isinstance(r, str)
                ],
                "downgrades": [
                    {
                        "rule": str(d.get("rule") or "").strip(),
                        "to": str(d.get("to") or "").strip().upper(),
                        "reason": str(d.get("reason") or "").strip(),
                    }
                    for d in (body.get("downgrades") or [])
                    if isinstance(d, dict)
                ],
            }

    raw_repo_types = schema.get("repo_types") or {}
    repo_types: set[str] = (
        {str(k) for k in raw_repo_types} if isinstance(raw_repo_types, dict) else set()
    )

    raw_statuses = catalog.get("statuses") or {}
    statuses: set[str] = (
        {str(k) for k in raw_statuses} if isinstance(raw_statuses, dict) else set()
    )

    return {"traits": traits, "repo_types": repo_types, "statuses": statuses}


def _fetch_full_rule_catalog() -> dict[str, dict]:
    """Every checkable rule's dispatch metadata, keyed by rule id.

    ``applies_to`` is None when the rule is not a repo-source scan
    (ADR-004); the compiler collapses an explicit empty list to None for
    the same reason. ``check_mode`` arrives resolved rather than being
    parsed out of ``check_notes`` on every run.
    """
    return {
        str(rule["id"]): {
            "applies_to": rule.get("applies_to"),
            "modifies": [str(x) for x in (rule.get("modifies") or [])],
            "status": str(rule.get("status") or "").strip(),
            "dimension": str(rule.get("dimension") or "").strip(),
            "check_mode": rule.get("check_mode"),
        }
        for rule in _catalog_rules()
        if rule.get("id")
    }


def _fetch_standards_for_service(
    service: dict, evaluator_cfg: EvaluatorConfig | None = None
) -> list[dict]:
    """Checkable rules in scope for one service, for the LLM prompt.

    Scope is the rule's ``applies_to`` against the repo's type. ``[all]``
    matches everything, which is the catalog's default posture — a rule
    narrowed to a type list is one whose check cannot tell "no subject
    here" apart from "violated".
    """
    repo_type = evaluator_cfg.repo_type if evaluator_cfg is not None else None
    dod_type = service.get("dod_type")

    def _to_rule_dict(rule: dict) -> dict:
        rule_id = str(rule.get("id") or "")
        status = str(rule.get("status") or "").strip()
        if status not in _VALID_RULE_STATUSES:
            raise ValueError(
                f"Rule {rule_id}: invalid status '{status}'. "
                f"Must be one of {sorted(_VALID_RULE_STATUSES)}."
            )
        return {
            "id": rule_id,
            "title": rule.get("title", ""),
            "status": status,
            "severity": rule.get("severity", "INFO"),
            "check_notes": (rule.get("check_notes") or "").strip(),
            "check_mode": rule.get("check_mode"),
        }

    rules: list[dict] = []
    for rule in _catalog_rules():
        applies_to = rule.get("applies_to") or []
        if (
            "all" in applies_to
            or (repo_type and repo_type in applies_to)
            or (dod_type and dod_type in applies_to)
        ):
            rules.append(_to_rule_dict(rule))
    return rules


def _parse_check_exceptions(raw: list) -> tuple[list[str], dict[str, str]]:
    """
    Parse check_exceptions from ecosystem.yaml.
    Supports both legacy flat strings and new structured {rule, reason} objects.
    Returns:
      - exception_ids: list of rule ID strings (for backwards-compat filtering)
      - exception_reasons: dict of rule_id -> reason string (for finding output)
    """
    exception_ids = []
    exception_reasons = {}
    for item in raw or []:
        if isinstance(item, str):
            # Legacy format — plain rule ID string
            rule_id = item.split("#")[0].strip()
            exception_ids.append(rule_id)
        elif isinstance(item, dict):
            # New structured format
            rule_id = item.get("rule", "").strip()
            reason = item.get("reason", "").strip()
            if rule_id:
                exception_ids.append(rule_id)
                if reason:
                    exception_reasons[rule_id] = reason
    return exception_ids, exception_reasons


def _deduplicate_sibling_findings(
    findings_by_service: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """
    Given findings keyed by service_id, collapse findings that are identical
    across siblings (same rule_id + same finding text) into the first sibling's
    list only, tagged with a note that the sibling shares the same issue.

    This keeps the API payload unchanged — we post to the first sibling's repo
    with an updated finding text that names the affected sibling, and skip
    posting the duplicate to the second sibling entirely.

    Example: both deejaytools-com-api and deejaytools-com-app fail XSTACK-001
    with identical finding text. Result: one finding posted under
    deejaytools-com-api mentioning deejaytools-com-app, nothing posted under
    deejaytools-com-app for that rule.
    """
    if len(findings_by_service) < 2:
        return findings_by_service

    service_ids = list(findings_by_service.keys())
    primary_id = service_ids[0]
    sibling_ids = service_ids[1:]

    primary_index: dict[tuple[str, str], dict] = {}
    for f in findings_by_service[primary_id]:
        key = (str(f.get("rule_id", "")), str(f.get("finding", "")))
        primary_index[key] = f

    deduplicated = {
        sid: list(findings) for sid, findings in findings_by_service.items()
    }

    for sibling_id in sibling_ids:
        remaining = []
        for f in findings_by_service[sibling_id]:
            key = (str(f.get("rule_id", "")), str(f.get("finding", "")))
            if key in primary_index:
                primary_f = primary_index[key]
                existing_finding = primary_f.get("finding", "")
                tag = f"(also affects {sibling_id})"
                if tag not in existing_finding:
                    primary_f["finding"] = existing_finding + f" {tag}"
            else:
                remaining.append(f)
        deduplicated[sibling_id] = remaining

    return deduplicated


def _declared_branch(record: dict | None) -> str:
    """The branch a registry entry says it develops on, else ``main``.

    Read from the service record for a plain repo and from the monorepo
    record for a monorepo, because that is where the repo is named in
    each case.
    """
    if not isinstance(record, dict):
        return "main"
    branch = str(record.get("branch") or "").strip()
    return branch or "main"


def _declared_org(record: dict | None) -> str:
    """The GitHub org a registry entry says it lives in, else the fleet default.

    Mirrors :func:`_declared_branch`. Almost every repo is under
    ``mini-app-polis`` and omits the field, but not all of them are: with
    the org hardcoded into the download URL, a repo in a personal org
    404'd on every single run. It was registered, it carried an
    evaluator.yaml declaring itself governed, and it had never once been
    evaluated — the exact state XSTACK-006 exists to make visible.
    """
    if not isinstance(record, dict):
        return _DEFAULT_ORG
    org = str(record.get("org") or "").strip()
    return org or _DEFAULT_ORG


#: Attempts per repo download, including the first. GitHub's secondary
#: rate limit clears in seconds, so a small number of tries with backoff
#: covers it; a larger number would only make a genuinely broken repo
#: take longer to report.
_DOWNLOAD_ATTEMPTS = 3
#: Base backoff. Doubles per attempt, and is overridden by Retry-After
#: when GitHub sends one.
_DOWNLOAD_BACKOFF_SECONDS = 2.0
#: Cap on an honoured Retry-After, so a long one cannot stall the run.
_DOWNLOAD_BACKOFF_CAP_SECONDS = 30.0

_unauthenticated_warned = False


def _warn_unauthenticated_once() -> None:
    """Say once per process that downloads are unauthenticated."""
    global _unauthenticated_warned
    if _unauthenticated_warned:
        return
    _unauthenticated_warned = True
    log.warning(
        "conformance: GITHUB_TOKEN is not set — repo downloads are "
        "unauthenticated (60 requests/hour, tight burst limits). Expect "
        "services to be throttled out of runs."
    )


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """How long to wait before the next attempt.

    GitHub's own Retry-After wins when present — guessing shorter than
    what the server asked for is how a retry storm starts — capped so one
    long value cannot stall the whole run.
    """
    if response is not None:
        raw = response.headers.get("Retry-After", "").strip()
        if raw:
            try:
                return min(float(raw), _DOWNLOAD_BACKOFF_CAP_SECONDS)
            except ValueError:
                pass
    return min(_DOWNLOAD_BACKOFF_SECONDS * (2**attempt), _DOWNLOAD_BACKOFF_CAP_SECONDS)


def _fetch_zipball(
    url: str, headers: dict[str, str], timeout: float, repo_id: str
) -> bytes | None:
    """Fetch a repo zipball, retrying transient failures.

    The single-attempt version of this was why services vanished from
    runs. One 403 from GitHub's secondary rate limit — which clears in
    seconds — and the repo was dropped for the entire run, with nothing
    in the report to say it had not been looked at. The failures came in
    contiguous blocks, four consecutive services in one run, which is
    what a burst limit looks like and not what a broken repo looks like.

    A 404 is not retried: the repo, the org or the branch is wrong, and
    trying again cannot change that. Everything else — 403, 429, 5xx,
    timeouts, connection errors — is worth another attempt.
    """
    last_detail = "unknown"
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        response: httpx.Response | None = None
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.get(url, headers=headers)
                if response.status_code == 404:
                    log.warning(
                        "conformance: %s not found (404) — check the repo "
                        "name, org and branch in ecosystem.yaml",
                        repo_id,
                    )
                    _UNRESOLVED_DOWNLOADS.append({"label": repo_id, "url": url})
                    return None
                if response.is_success:
                    return response.content
                remaining = response.headers.get("X-RateLimit-Remaining", "?")
                last_detail = (
                    f"HTTP {response.status_code} (X-RateLimit-Remaining={remaining})"
                )
        except Exception as exc:
            last_detail = f"{type(exc).__name__}: {exc}"

        if attempt == _DOWNLOAD_ATTEMPTS - 1:
            break
        delay = _retry_delay(response, attempt)
        log.warning(
            "conformance: %s download failed (%s) — retrying in %.1fs "
            "(attempt %d of %d)",
            repo_id,
            last_detail,
            delay,
            attempt + 2,
            _DOWNLOAD_ATTEMPTS,
        )
        time.sleep(delay)

    log.warning(
        "conformance: %s could not be downloaded after %d attempts (%s)",
        repo_id,
        _DOWNLOAD_ATTEMPTS,
        last_detail,
    )
    return None


def _download_repo(
    repo_id: str, tmp_dir: str, branch: str = "main", org: str = _DEFAULT_ORG
) -> Path | None:
    """
    Download a repo from GitHub as a zip archive and extract it.
    Returns the extracted repo path or None on failure.

    ``branch`` is the ref to read, defaulting to ``main``. Not every repo
    in the fleet develops on ``main``: deejaytools-com works on ``dev``,
    which sat ten commits ahead while the conformance run kept reading a
    branch that had none of the work on it. The repo was reported for
    missing security workflows it had, and no amount of fixing the repo
    would have cleared it. A registry entry declares its branch with
    ``branch:`` in ecosystem.yaml; everything else keeps ``main``.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    else:
        # Worth saying out loud once per run. Every repo in the fleet is
        # public, so an absent token does not 404 — the download still
        # works, just at 60 requests an hour with much tighter burst
        # limits instead of 5000. A run makes roughly one core API call
        # per service, so unauthenticated is the difference between
        # "always fine" and "a few services throttled every run", and
        # the symptom is services silently missing from the report
        # rather than anything that looks like an auth failure.
        _warn_unauthenticated_once()

    url = f"https://api.github.com/repos/{org}/{repo_id}/zipball/{branch}"
    dest = Path(tmp_dir) / repo_id

    try:
        timeout = float(os.environ.get("EVALUATOR_CLONE_TIMEOUT_SECONDS", "60"))
        content = _fetch_zipball(url, headers, timeout, f"{org}/{repo_id}")
        if content is None:
            return None

        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            zf.extractall(tmp_dir)
            top_level = next(
                (
                    p
                    for p in [Path(tmp_dir) / n.split("/")[0] for n in zf.namelist()]
                    if p.is_dir()
                ),
                None,
            )
            if top_level:
                if dest.exists():
                    shutil.rmtree(dest)
                top_level.rename(dest)

        log.info("conformance: downloaded %s@%s", repo_id, branch)
        return dest
    except Exception as exc:
        log.warning("conformance: failed to download %s: %s", repo_id, exc)
        # Also recorded as a STATUS finding by _post_not_evaluated at the
        # call site. Two sinks on purpose: the finding is the durable row
        # in Pipeline Health, this is the line in the channel.
        _report_issue("repo_download_failed", repo_id, exc)
        return None


def run_conformance_check(
    *,
    repo_id: str,
    repo_path: Path,
    standards_version: str,
    service_type: str = "worker",
    dod_type: str | None = None,
    language: str = "python",
    cog_subtype: str | None = None,
    check_exceptions: list[str] | None = None,
    exception_reasons: dict[str, str] | None = None,
    standards_rules: list[dict] | None = None,
    run_id: str = "conformance",
    monorepo_root: Path | None = None,
    workspace_package_json_text: str | None = None,
    monorepo_context: dict | None = None,
    post: bool = True,
    post_llm_only: bool = False,
    evaluator_config: EvaluatorConfig | None = None,
    rule_applies_to: dict[str, list[str]] | None = None,
    rule_catalog: dict[str, dict] | None = None,
    catalog_schema: dict | None = None,
) -> list[dict[str, Any]]:
    """
    Run deterministic + LLM conformance checks against a cloned repo.
    Posts findings to api-kaianolevine-com when post=True. Never raises.
    """
    try:
        prefect_log = get_run_logger()
    except Exception:
        import logging

        prefect_log = logging.getLogger(__name__)

    # Deterministic checks
    try:
        result = run_all_checks(
            repo_path,
            language=language,
            service_type=service_type,
            dod_type=dod_type,
            cog_subtype=cog_subtype,
            check_exceptions=check_exceptions,
            exception_reasons=exception_reasons,
            monorepo_root=monorepo_root,
            workspace_package_json_text=workspace_package_json_text,
            evaluator_config=evaluator_config,
            rule_catalog=rule_catalog,
            catalog_schema=catalog_schema,
        )
        deterministic_findings = result.findings
        checked_rule_ids = result.checked_rule_ids
    except Exception as exc:
        log.exception("conformance: run_all_checks failed for %s: %s", repo_id, exc)
        deterministic_findings = []
        checked_rule_ids = set()
        # Not caught by _post_not_evaluated: structurally this repo *was*
        # evaluated, so it posts a clean STATUS row and counts toward the
        # total. Zero deterministic findings from zero deterministic
        # checks is indistinguishable from a repo that passed them all.
        _report_issue("deterministic_checks_failed", repo_id, exc)

    prefect_log.info(
        "conformance: %d deterministic findings for %s",
        len(deterministic_findings),
        repo_id,
    )

    # LLM soft-rule assessment
    llm_findings: list[dict[str, Any]] = []
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        try:
            prompt = build_conformance_prompt(
                repo_id=repo_id,
                service_type=service_type,
                dod_type=dod_type,
                language=language,
                standards_version=standards_version,
                deterministic_findings=deterministic_findings,
                standards_rules=standards_rules or [],
                checked_rule_ids=checked_rule_ids,
                check_exceptions=check_exceptions,
                exception_reasons=exception_reasons,
                all_skipped_ids=evaluator_config.all_skipped_ids
                if evaluator_config is not None
                else None,
                monorepo_context=monorepo_context,
                repo_path=repo_path,
            )
            model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
            raw = _anthropic_messages_create(
                api_key=api_key,
                model=model,
                max_tokens=2048,
                user_prompt=prompt,
            )
            llm_findings, _ = _parse_findings_from_claude(raw)
            # Drop spurious "passing" findings — the prompt instructs the LLM
            # to return {"findings":[]} when a rule is clean or not applicable,
            # but it sometimes emits a finding explaining the pass instead.
            # These are noise: they store ERROR/WARN rows that say "no violation
            # found", which then trip EVAL-003's remediation-quality gate.
            _passing_markers = (
                "no violation found",
                "passes — no",
                "passes - no",
                " passes.",
                " passes —",
                " passes -",
                "no action needed",
                "no finding",
                "all clean",
            )
            _raw_count = len(llm_findings)
            _dropped_findings = [
                f
                for f in llm_findings
                if any(
                    m in (f.get("finding") or "").lower()
                    or m in (f.get("suggestion") or "").lower()
                    for m in _passing_markers
                )
            ]
            llm_findings = [f for f in llm_findings if f not in _dropped_findings]
            if _dropped_findings:
                for _f in _dropped_findings:
                    prefect_log.warning(
                        "conformance: dropped spurious passing finding for %s [%s] %s",
                        repo_id,
                        _f.get("rule_id") or "?",
                        (_f.get("finding") or "")[:200],
                    )
            prefect_log.info(
                "conformance: %d LLM findings for %s", len(llm_findings), repo_id
            )
        except Exception as exc:
            log.warning("conformance: LLM assessment failed for %s: %s", repo_id, exc)
            _report_issue("llm_assessment_failed", repo_id, exc)
    else:
        prefect_log.warning(
            "conformance: ANTHROPIC_API_KEY not set, skipping LLM assessment for %s",
            repo_id,
        )
        # A note, not an issue. A missing key is one configuration fact
        # that would otherwise fire once per repo and turn every LLM run
        # WARN for a single cause. Counted so the message says how many
        # repos went unassessed; not escalated, because the count is the
        # information and the run is otherwise fine.
        _report_note("llm_skipped_no_api_key", repo_id)

    all_findings = deterministic_findings + llm_findings
    findings_to_post = llm_findings if post_llm_only else all_findings

    if post and not findings_to_post:
        findings_to_post = [
            {
                "rule_id": "STATUS",
                "dimension": "structural_conformance",
                "severity": "SUCCESS",
                "finding": f"{repo_id} passed all {'LLM' if post_llm_only else 'conformance'} checks for standards v{standards_version}.",
                "suggestion": "",
            }
        ]

    if post:
        _post_tracked(
            repo_id,
            prefect_log,
            findings=findings_to_post,
            run_id=run_id,
            repo=repo_id,
            flow_name="conformance",
            source="conformance_llm",
            standards_version=standards_version,
        )

    return all_findings


def _run_standalone_conformance(
    service: dict,
    repo_path: Path,
    standards_version: str,
    run_id: str,
    prefect_log: Any,
    rule_applies_to: dict[str, list[str]] | None = None,
    rule_catalog: dict[str, dict] | None = None,
    catalog_schema: dict | None = None,
) -> None:
    """Run full conformance for a single cloned service (posts immediately)."""
    repo_id = service.get("id", "")
    if not repo_id:
        return
    service_type = service.get("type", "worker")
    _raw_language = str(service.get("language") or "python")
    language = "typescript" if _raw_language == "astro" else _raw_language
    cog_subtype = str(service.get("cog_subtype") or "").strip() or None
    dod_type = service.get("dod_type")
    raw_exc = service.get("check_exceptions") or []
    check_exceptions, exception_reasons = _parse_check_exceptions(raw_exc)

    # Load evaluator.yaml from cloned repo (preferred), fall back to ecosystem.yaml
    evaluator_cfg = load_evaluator_config(
        repo_path,
        fallback_type=service.get("type") or dod_type,
        fallback_exceptions=check_exceptions,
        fallback_exception_reasons=exception_reasons,
        rule_catalog=rule_catalog,
        catalog_schema=catalog_schema,
    )

    standards_rules = _fetch_standards_for_service(service, evaluator_cfg)
    try:
        all_findings = run_conformance_check(
            repo_id=repo_id,
            repo_path=repo_path,
            standards_version=standards_version,
            service_type=service_type,
            dod_type=dod_type,
            language=language,
            cog_subtype=cog_subtype,
            check_exceptions=check_exceptions,
            exception_reasons=exception_reasons,
            standards_rules=standards_rules,
            run_id=run_id,
            post=True,
            post_llm_only=True,
            evaluator_config=evaluator_cfg,
            rule_applies_to=rule_applies_to,
            rule_catalog=rule_catalog,
            catalog_schema=catalog_schema,
        )
        _ = all_findings
        prefect_log.info(
            "conformance: LLM pass complete for %s (config: %s)",
            repo_id,
            evaluator_cfg.source,
        )
    except Exception as exc:
        prefect_log.warning("conformance: check failed for %s: %s", repo_id, exc)
        _report_issue("repo_check_failed", repo_id, exc)


def _build_conformance_run_id(standards_version: str) -> str:
    """Build a per-execution run_id for conformance findings."""
    flow_run_id = ""
    try:
        from prefect.runtime import flow_run

        flow_run_id = str(flow_run.id or "").strip()
    except Exception:
        flow_run_id = ""

    unique_suffix = flow_run_id or datetime.datetime.now(datetime.UTC).strftime(
        "%Y%m%dT%H%M%S"
    )
    return f"conformance-{standards_version}-{unique_suffix}"


def _post_not_evaluated(
    repo_id: str,
    reason: str,
    *,
    run_id: str,
    flow_name: str,
    source: str,
    standards_version: str,
    prefect_log: Any,
) -> None:
    """Record that a declared service could not be evaluated in this run.

    Every service that is evaluated posts a row — its findings, or a
    STATUS/SUCCESS row saying it passed. A service that could not be
    downloaded, or whose checks raised, used to post nothing and only
    log, which meant it vanished from the report entirely: the totals
    said twelve rows and eight successes, and nothing anywhere said
    that three declared services had not been looked at. A silent
    absence reads as a clean bill of health, which is the one thing it
    is not.

    Posted under the same synthetic STATUS id the success row uses, so
    it needs no catalog rule of its own — it is the same statement
    about the run, with the opposite value.
    """
    _post_tracked(
        repo_id,
        prefect_log,
        findings=[
            {
                "rule_id": "STATUS",
                "dimension": "structural_conformance",
                "severity": "ERROR",
                "finding": (
                    f"{repo_id} was declared active but was not evaluated in "
                    f"this run: {reason}. Its conformance is unknown, not "
                    f"clean."
                ),
                "suggestion": (
                    "Check the run log for this repo. Until it evaluates, "
                    "treat its last known findings as stale rather than "
                    "treating its absence from this run as a pass."
                ),
            }
        ],
        run_id=run_id,
        repo=repo_id,
        flow_name=flow_name,
        source=source,
        standards_version=standards_version,
    )


def _build_deterministic_run_id(standards_version: str) -> str:
    """Build a per-execution run_id for deterministic conformance findings."""
    flow_run_id = ""
    try:
        from prefect.runtime import flow_run

        flow_run_id = str(flow_run.id or "").strip()
    except Exception:
        flow_run_id = ""

    unique_suffix = flow_run_id or datetime.datetime.now(datetime.UTC).strftime(
        "%Y%m%dT%H%M%S"
    )
    return f"deterministic-{standards_version}-{unique_suffix}"


def _run_standalone_deterministic(
    service: dict,
    repo_path: Path,
    standards_version: str,
    run_id: str,
    prefect_log: Any,
    monorepo_root: Path | None = None,
    workspace_package_json_text: str | None = None,
    rule_applies_to: dict[str, list[str]] | None = None,
    rule_catalog: dict[str, dict] | None = None,
    catalog_schema: dict | None = None,
) -> None:
    """Run deterministic-only checks for a single service and post immediately."""
    repo_id = service.get("id", "")
    if not repo_id:
        return

    service_type = service.get("type", "worker")
    _raw_language = str(service.get("language") or "python")
    language = "typescript" if _raw_language == "astro" else _raw_language
    cog_subtype = str(service.get("cog_subtype") or "").strip() or None
    dod_type = service.get("dod_type")
    raw_exc = service.get("check_exceptions") or []
    check_exceptions, exception_reasons = _parse_check_exceptions(raw_exc)

    # Load evaluator.yaml from cloned repo (preferred), fall back to ecosystem.yaml
    check_root = monorepo_root or repo_path
    evaluator_cfg = load_evaluator_config(
        check_root,
        fallback_type=service.get("type") or dod_type,
        fallback_exceptions=check_exceptions,
        fallback_exception_reasons=exception_reasons,
        rule_catalog=rule_catalog,
        catalog_schema=catalog_schema,
    )
    # For monorepo apps the evaluator.yaml may live at the app path
    if monorepo_root and not (check_root / "evaluator.yaml").exists():
        evaluator_cfg = load_evaluator_config(
            repo_path,
            fallback_type=service.get("type") or dod_type,
            fallback_exceptions=check_exceptions,
            fallback_exception_reasons=exception_reasons,
            rule_catalog=rule_catalog,
            catalog_schema=catalog_schema,
        )

    prefect_log.info(
        "deterministic: %s using config from %s", repo_id, evaluator_cfg.source
    )

    _repo_started = time.monotonic()
    try:
        result = run_all_checks(
            repo_path,
            language=language,
            service_type=service_type,
            dod_type=dod_type,
            cog_subtype=cog_subtype,
            check_exceptions=check_exceptions,
            exception_reasons=exception_reasons,
            monorepo_root=monorepo_root,
            workspace_package_json_text=workspace_package_json_text,
            evaluator_config=evaluator_cfg,
            rule_catalog=rule_catalog,
            catalog_schema=catalog_schema,
            progress=lambda note: prefect_log.info(
                "deterministic: %s: %s", repo_id, note
            ),
        )
        findings = result.findings
        prefect_log.info(
            "deterministic: %d findings for %s (%.1fs)",
            len(findings),
            repo_id,
            time.monotonic() - _repo_started,
        )
    except Exception as exc:
        prefect_log.warning(
            "deterministic: run_all_checks failed for %s: %s", repo_id, exc
        )
        _post_not_evaluated(
            repo_id,
            f"the deterministic checks raised ({type(exc).__name__}: {exc})",
            run_id=run_id,
            flow_name="deterministic-conformance",
            source="conformance_deterministic",
            standards_version=standards_version,
            prefect_log=prefect_log,
        )
        return

    if not findings:
        findings = [
            {
                "rule_id": "STATUS",
                "dimension": "structural_conformance",
                "severity": "SUCCESS",
                "finding": f"{repo_id} passed all deterministic checks for standards v{standards_version}.",
                "suggestion": "",
            }
        ]

    _post_tracked(
        repo_id,
        prefect_log,
        findings=findings,
        run_id=run_id,
        repo=repo_id,
        flow_name="deterministic-conformance",
        source="conformance_deterministic",
        standards_version=standards_version,
    )


def _run_applies_to_absent_checks(
    *,
    ecosystem: dict,
    rule_catalog: dict[str, dict],
    standards_version: str,
    evaluator_standards_version: str,
    run_id: str,
    prefect_log: Any,
) -> None:
    """Run applies_to-absent checks once per flow invocation."""
    from evaluator_cog.engine.deterministic import (
        check_eval_003,
        check_eval_007,
        check_mono_003,
        check_xstack_006,
        check_xstack_007,
        check_xstack_008,
    )

    # EVAL-003 — finding quality (runtime data-quality on stored findings)
    try:
        eval_003_findings = check_eval_003()
        if eval_003_findings:
            _post_tracked(
                "EVAL-003",
                prefect_log,
                findings=eval_003_findings,
                run_id=run_id,
                repo="ecosystem-standards",
                flow_name="eval-003",
                source="data_quality",
                standards_version=standards_version,
            )
    except Exception as exc:
        prefect_log.warning("EVAL-003: check failed: %s", exc)

    # MONO-003 — monorepo dedup integrity of ecosystem.yaml inventory
    try:
        mono_003_findings = check_mono_003(ecosystem=ecosystem)
        if mono_003_findings:
            _post_tracked(
                "MONO-003",
                prefect_log,
                findings=mono_003_findings,
                run_id=run_id,
                repo="ecosystem-standards",
                flow_name="mono-003",
                source="data_quality",
                standards_version=standards_version,
            )
    except Exception as exc:
        prefect_log.warning("MONO-003: check failed: %s", exc)

    # XSTACK-006 / XSTACK-007 — cross-repo coherence.
    #
    # Both carry `applies_to: None`, so resolve_dispatch returns
    # SKIP_SCOPE for them on every repo and they can never run on the
    # per-repo path. That is correct: their read sources are the GitHub
    # org listing and the ecosystem.yaml registry, not any one repo's
    # source tree. This lane is where a rule with no single repo subject
    # belongs, which is why EVAL-003 and MONO-003 already live here.
    #
    # The registry passed in is the one fetched for this run, not a
    # cached copy — XSTACK-006 requires reading it at the version under
    # evaluation so a repo registered in the same release that creates
    # it is not reported as unregistered.
    for _rule_id, _check in (
        ("XSTACK-006", check_xstack_006),
        ("XSTACK-007", check_xstack_007),
    ):
        try:
            _findings = _check(ecosystem=ecosystem)
            if _findings:
                _post_tracked(
                    _rule_id,
                    prefect_log,
                    findings=_findings,
                    run_id=run_id,
                    repo="ecosystem-standards",
                    flow_name=_rule_id.lower(),
                    source="standards_drift",
                    standards_version=standards_version,
                )
        except Exception as exc:
            prefect_log.warning("%s: check failed: %s", _rule_id, exc)

    # XSTACK-008 — every registered repo resolved where the registry says.
    #
    # Reads this run's own download results rather than the registry
    # alone, and adds no GitHub calls: the downloads already happened.
    # It runs after every repo has been attempted, so the record is
    # complete by the time it is read.
    try:
        xstack_008_findings = check_xstack_008(unresolved=_UNRESOLVED_DOWNLOADS)
        if xstack_008_findings:
            _post_tracked(
                "XSTACK-008",
                prefect_log,
                findings=xstack_008_findings,
                run_id=run_id,
                repo="ecosystem-standards",
                flow_name="xstack-008",
                source="standards_drift",
                standards_version=standards_version,
            )
    except Exception as exc:
        prefect_log.warning("XSTACK-008: check failed: %s", exc)

    # EVAL-007 — standards/evaluator drift
    try:
        eval_007_findings = check_eval_007(
            rule_catalog=rule_catalog,
            current_standards_version=standards_version,
            evaluator_standards_version=evaluator_standards_version,
        )
        if eval_007_findings:
            _post_tracked(
                "EVAL-007",
                prefect_log,
                findings=eval_007_findings,
                run_id=run_id,
                repo="ecosystem-standards",
                flow_name="eval-007",
                source="standards_drift",
                standards_version=standards_version,
            )
    except Exception as exc:
        prefect_log.warning("EVAL-007: check failed: %s", exc)


_REPO = "evaluator-cog"
_report_failure = make_failure_hook("conformance-check", repo=_REPO)


@flow(
    name="conformance-check",
    log_prints=True,
    on_completion=[_on_completion],
    on_failure=[_report_failure],
    on_crashed=[_report_failure],
)
def conformance_check_flow(run_llm: bool = False) -> None:
    """
    Clone each active repo and run conformance checks.

    When run_llm=False (default): deterministic checks only, no LLM calls.
    Posts findings with source='conformance_deterministic'. Runs daily.

    When run_llm=True: deterministic pass first (for checked_rule_ids),
    then LLM soft-rule assessment. Posts LLM findings only with
    source='conformance_llm'. Triggered manually or via Prefect automation.

    In both modes, applies_to-absent introspection checks also run once
    per invocation:
      EVAL-003, MONO-003 → source='data_quality'
      EVAL-007           → source='standards_drift'
    """
    try:
        prefect_log = get_run_logger()
    except Exception:
        import logging

        prefect_log = logging.getLogger(__name__)
    _reset_run_tally()
    flow_label = "conformance" if run_llm else "deterministic"

    standards_version = _get_standards_version()
    prefect_log.info("%s: standards version %s", flow_label, standards_version)
    catalog_schema = _fetch_catalog_schema()
    rule_catalog = _fetch_full_rule_catalog()
    prefect_log.info(
        "%s: loaded %d traits, %d repo types, %d rules from catalog",
        flow_label,
        len(catalog_schema.get("traits", {})),
        len(catalog_schema.get("repo_types", set())),
        len(rule_catalog),
    )

    rule_applies_to = {
        rule_id: meta["applies_to"]
        for rule_id, meta in rule_catalog.items()
        if isinstance(meta, dict) and isinstance(meta.get("applies_to"), list)
    }
    if not rule_catalog:
        prefect_log.warning(
            "%s: full rule catalog empty — type-based auto-exceptions "
            "disabled for this run",
            flow_label,
        )

    ecosystem = _fetch_yaml(_ECOSYSTEM_YAML_URL)
    active_repos = _get_active_repos(ecosystem)

    if not active_repos:
        prefect_log.warning("%s: no active repos found in ecosystem.yaml", flow_label)
        return

    prefect_log.info("%s: checking %d active repos", flow_label, len(active_repos))
    run_id = (
        _build_conformance_run_id(standards_version)
        if run_llm
        else _build_deterministic_run_id(standards_version)
    )

    with concurrency("evaluator-cog-writes", occupy=1):
        monorepos_registry = _get_monorepos(ecosystem)

        standalone_services = [s for s in active_repos if not s.get("monorepo")]
        monorepo_service_groups: dict[str, list[dict]] = {}
        for s in active_repos:
            mono_id = s.get("monorepo")
            if mono_id:
                monorepo_service_groups.setdefault(str(mono_id), []).append(s)

        # One service id must run at most once per flow (duplicate ecosystem rows, etc.).
        seen_repo_ids: set[str] = set()

        with tempfile.TemporaryDirectory() as tmp_dir:
            for service in standalone_services:
                repo_id = service.get("id", "")
                repo_name = service.get("repo") or repo_id
                if not repo_id:
                    continue
                if repo_id in seen_repo_ids:
                    prefect_log.warning(
                        "%s: skipping duplicate service %s",
                        flow_label,
                        repo_id,
                    )
                    continue
                seen_repo_ids.add(repo_id)

                prefect_log.info("%s: processing %s", flow_label, repo_id)

                repo_path = _download_repo(
                    repo_name,
                    tmp_dir,
                    _declared_branch(service),
                    _declared_org(service),
                )
                if repo_path is None:
                    prefect_log.warning(
                        "%s: skipping %s — could not clone", flow_label, repo_id
                    )
                    _post_not_evaluated(
                        repo_id,
                        f"the repository could not be downloaded "
                        f"({repo_name}@{_declared_branch(service)})",
                        run_id=run_id,
                        flow_name=(
                            "conformance-check"
                            if run_llm
                            else "deterministic-conformance"
                        ),
                        source=(
                            "conformance_check"
                            if run_llm
                            else "conformance_deterministic"
                        ),
                        standards_version=standards_version,
                        prefect_log=prefect_log,
                    )
                    continue

                try:
                    if run_llm:
                        _run_standalone_conformance(
                            service,
                            repo_path,
                            standards_version,
                            run_id,
                            prefect_log,
                            rule_applies_to=rule_applies_to,
                            rule_catalog=rule_catalog,
                            catalog_schema=catalog_schema,
                        )
                    else:
                        _run_standalone_deterministic(
                            service,
                            repo_path,
                            standards_version,
                            run_id,
                            prefect_log,
                            rule_applies_to=rule_applies_to,
                            rule_catalog=rule_catalog,
                            catalog_schema=catalog_schema,
                        )
                except Exception as exc:
                    prefect_log.error(
                        "%s: unhandled error processing %s — skipping: %s",
                        flow_label,
                        repo_id,
                        exc,
                        exc_info=True,
                    )
                    # The third silent-skip path. The two inside
                    # _run_standalone_deterministic cover a failed
                    # download and a raising check; anything that goes
                    # wrong around them — loading evaluator.yaml, parsing
                    # check_exceptions — lands here instead, and used to
                    # log and post nothing, which is the same invisible
                    # absence by a different route.
                    _post_not_evaluated(
                        repo_id,
                        f"processing raised before findings could be "
                        f"computed ({type(exc).__name__}: {exc})",
                        run_id=run_id,
                        flow_name=(
                            "conformance-check"
                            if run_llm
                            else "deterministic-conformance"
                        ),
                        source=(
                            "conformance_check"
                            if run_llm
                            else "conformance_deterministic"
                        ),
                        standards_version=standards_version,
                        prefect_log=prefect_log,
                    )

            for mono_id, services in monorepo_service_groups.items():
                mono_record = monorepos_registry.get(mono_id)
                if not mono_record:
                    for svc in services:
                        rid = svc.get("id", "")
                        rname = svc.get("repo") or rid
                        if not rid:
                            continue
                        if rid in seen_repo_ids:
                            prefect_log.warning(
                                "%s: skipping duplicate service %s",
                                flow_label,
                                rid,
                            )
                            continue
                        seen_repo_ids.add(rid)
                        rp = _download_repo(
                            rname, tmp_dir, _declared_branch(svc), _declared_org(svc)
                        )
                        if rp is None:
                            continue
                        try:
                            if run_llm:
                                _run_standalone_conformance(
                                    svc,
                                    rp,
                                    standards_version,
                                    run_id,
                                    prefect_log,
                                    rule_applies_to=rule_applies_to,
                                    rule_catalog=rule_catalog,
                                    catalog_schema=catalog_schema,
                                )
                            else:
                                _run_standalone_deterministic(
                                    svc,
                                    rp,
                                    standards_version,
                                    run_id,
                                    prefect_log,
                                    rule_applies_to=rule_applies_to,
                                    rule_catalog=rule_catalog,
                                    catalog_schema=catalog_schema,
                                )
                        except Exception as exc:
                            prefect_log.error(
                                "%s: unhandled error processing %s — skipping: %s",
                                flow_label,
                                rid,
                                exc,
                                exc_info=True,
                            )
                            _post_not_evaluated(
                                rid,
                                f"processing raised before findings could be "
                                f"computed ({type(exc).__name__}: {exc})",
                                run_id=run_id,
                                flow_name=(
                                    "conformance-check"
                                    if run_llm
                                    else "deterministic-conformance"
                                ),
                                source=(
                                    "conformance_check"
                                    if run_llm
                                    else "conformance_deterministic"
                                ),
                                standards_version=standards_version,
                                prefect_log=prefect_log,
                            )
                    continue

                repo_name = mono_record.get("repo") or mono_id
                prefect_log.info("%s: cloning monorepo %s", flow_label, repo_name)
                monorepo_root = _download_repo(
                    repo_name,
                    tmp_dir,
                    _declared_branch(mono_record),
                    _declared_org(mono_record),
                )
                if monorepo_root is None:
                    prefect_log.warning(
                        "%s: skipping monorepo %s — could not clone",
                        flow_label,
                        mono_id,
                    )
                    # One failed clone hides every app inside it, so the
                    # row goes against each declared service rather than
                    # against the monorepo, which is not a repo the
                    # report has a column for.
                    for _svc in monorepo_service_groups.get(str(mono_id), []):
                        _svc_id = _svc.get("id", "")
                        if not _svc_id:
                            continue
                        # Against each service for the same reason the
                        # finding is: _download_repo flagged the monorepo,
                        # which is not a repo the report has a column for,
                        # and the services it hid are what went unevaluated.
                        _report_issue("repo_download_failed", _svc_id)
                        _post_not_evaluated(
                            _svc_id,
                            f"its monorepo could not be downloaded "
                            f"({repo_name}@{_declared_branch(mono_record)})",
                            run_id=run_id,
                            flow_name=(
                                "conformance-check"
                                if run_llm
                                else "deterministic-conformance"
                            ),
                            source=(
                                "conformance_check"
                                if run_llm
                                else "conformance_deterministic"
                            ),
                            standards_version=standards_version,
                            prefect_log=prefect_log,
                        )
                    continue

                workspace_package_json_text = _read_workspace_package_json(
                    monorepo_root
                )

                monorepo_context = {
                    "monorepo_id": mono_id,
                    "package_manager": mono_record.get("package_manager", "pnpm"),
                    "workspace_deps": mono_record.get("workspace_deps", []),
                    "sibling_apps": [
                        {
                            "service_id": app.get("service_id") or app.get("id"),
                            "path": app.get("path"),
                        }
                        for app in mono_record.get("apps", [])
                    ],
                }

                findings_by_service: dict[str, list[dict[str, Any]]] = {}

                for service in services:
                    repo_id = service.get("id", "")
                    if not repo_id:
                        continue
                    if repo_id in seen_repo_ids:
                        prefect_log.warning(
                            "%s: skipping duplicate service %s",
                            flow_label,
                            repo_id,
                        )
                        continue
                    seen_repo_ids.add(repo_id)

                    try:
                        monorepo_path = str(service.get("monorepo_path") or "")
                        repo_path = (
                            monorepo_root / monorepo_path
                            if monorepo_path
                            else monorepo_root
                        )

                        if not repo_path.is_dir():
                            prefect_log.warning(
                                "%s: monorepo_path '%s' not found in %s for %s",
                                flow_label,
                                monorepo_path,
                                mono_id,
                                repo_id,
                            )
                            continue

                        prefect_log.info(
                            "%s: processing monorepo app %s at %s",
                            flow_label,
                            repo_id,
                            monorepo_path,
                        )

                        service_type = service.get("type", "worker")
                        _raw_language = str(service.get("language") or "typescript")
                        language = (
                            "typescript" if _raw_language == "astro" else _raw_language
                        )
                        cog_subtype = (
                            str(service.get("cog_subtype") or "").strip() or None
                        )
                        dod_type = service.get("dod_type")
                        raw_exc = service.get("check_exceptions") or []
                        check_exceptions, exception_reasons = _parse_check_exceptions(
                            raw_exc
                        )
                        standards_rules = (
                            _fetch_standards_for_service(service) if run_llm else []
                        )

                        if run_llm:
                            try:
                                _check_root = monorepo_root
                                _evaluator_cfg = load_evaluator_config(
                                    _check_root,
                                    fallback_type=service.get("type") or dod_type,
                                    fallback_exceptions=check_exceptions,
                                    fallback_exception_reasons=exception_reasons,
                                    rule_catalog=rule_catalog,
                                    catalog_schema=catalog_schema,
                                )
                                if (
                                    monorepo_root
                                    and not (_check_root / "evaluator.yaml").exists()
                                ):
                                    _evaluator_cfg = load_evaluator_config(
                                        repo_path,
                                        fallback_type=service.get("type") or dod_type,
                                        fallback_exceptions=check_exceptions,
                                        fallback_exception_reasons=exception_reasons,
                                        rule_catalog=rule_catalog,
                                        catalog_schema=catalog_schema,
                                    )
                                run_conformance_check(
                                    repo_id=repo_id,
                                    repo_path=repo_path,
                                    standards_version=standards_version,
                                    service_type=service_type,
                                    dod_type=dod_type,
                                    language=language,
                                    cog_subtype=cog_subtype,
                                    check_exceptions=check_exceptions,
                                    exception_reasons=exception_reasons,
                                    standards_rules=standards_rules,
                                    run_id=run_id,
                                    monorepo_root=monorepo_root,
                                    workspace_package_json_text=workspace_package_json_text,
                                    monorepo_context=monorepo_context,
                                    post=True,
                                    post_llm_only=True,
                                    evaluator_config=_evaluator_cfg,
                                    rule_applies_to=rule_applies_to,
                                    rule_catalog=rule_catalog,
                                    catalog_schema=catalog_schema,
                                )
                                prefect_log.info(
                                    "conformance: posted LLM findings for monorepo app %s",
                                    repo_id,
                                )
                            except Exception as exc:
                                prefect_log.warning(
                                    "conformance: check failed for monorepo app %s: %s",
                                    repo_id,
                                    exc,
                                )
                        else:
                            try:
                                check_root = monorepo_root
                                evaluator_cfg = load_evaluator_config(
                                    check_root,
                                    fallback_type=service.get("type") or dod_type,
                                    fallback_exceptions=check_exceptions,
                                    fallback_exception_reasons=exception_reasons,
                                    rule_catalog=rule_catalog,
                                    catalog_schema=catalog_schema,
                                )
                                if (
                                    monorepo_root
                                    and not (check_root / "evaluator.yaml").exists()
                                ):
                                    evaluator_cfg = load_evaluator_config(
                                        repo_path,
                                        fallback_type=service.get("type") or dod_type,
                                        fallback_exceptions=check_exceptions,
                                        fallback_exception_reasons=exception_reasons,
                                        rule_catalog=rule_catalog,
                                        catalog_schema=catalog_schema,
                                    )
                                result = run_all_checks(
                                    repo_path,
                                    language=language,
                                    service_type=service_type,
                                    dod_type=dod_type,
                                    cog_subtype=cog_subtype,
                                    check_exceptions=check_exceptions,
                                    exception_reasons=exception_reasons,
                                    monorepo_root=monorepo_root,
                                    workspace_package_json_text=workspace_package_json_text,
                                    evaluator_config=evaluator_cfg,
                                    rule_catalog=rule_catalog,
                                    catalog_schema=catalog_schema,
                                )
                                findings_by_service[repo_id] = result.findings
                            except Exception as exc:
                                prefect_log.warning(
                                    "deterministic: check failed for monorepo app %s: %s",
                                    repo_id,
                                    exc,
                                )
                    except Exception as exc:
                        prefect_log.error(
                            "%s: unhandled error processing monorepo app %s — skipping: %s",
                            flow_label,
                            repo_id,
                            exc,
                            exc_info=True,
                        )

                if not run_llm:
                    if len(findings_by_service) > 1:
                        findings_by_service = _deduplicate_sibling_findings(
                            findings_by_service
                        )

                    for service_id, findings in findings_by_service.items():
                        if not findings:
                            findings = [
                                {
                                    "rule_id": "STATUS",
                                    "dimension": "structural_conformance",
                                    "severity": "SUCCESS",
                                    "finding": (
                                        f"{service_id} passed all deterministic checks for "
                                        f"standards v{standards_version}."
                                    ),
                                    "suggestion": "",
                                }
                            ]
                        _post_tracked(
                            service_id,
                            prefect_log,
                            findings=findings,
                            run_id=run_id,
                            repo=service_id,
                            flow_name="conformance-check",
                            source="conformance_deterministic",
                            standards_version=standards_version,
                        )

            # ── Non-repo-scan rules (ADR-004: applies_to absent) ─────────────
            _run_applies_to_absent_checks(
                ecosystem=ecosystem,
                rule_catalog=rule_catalog,
                standards_version=standards_version,
                evaluator_standards_version=standards_version,
                run_id=run_id,
                prefect_log=prefect_log,
            )

    prefect_log.info(
        "%s: complete — %d findings offered, %d posted, %d duplicate, %d failed",
        flow_label,
        _RUN_TALLY.attempted,
        _RUN_TALLY.posted,
        _RUN_TALLY.duplicates,
        _RUN_TALLY.failed,
    )

    # The run's own outcome, as a notification. Not a finding: what this
    # flow computed about other repos is graded and stays in the
    # evaluations table; whether the flow itself worked is not.
    #
    # Skipped when nothing was delivered at all, because the assertion
    # below is about to fail the run and the failure hook will report it.
    # Two messages for one event is how a channel earns being ignored.
    if not _RUN_TALLY.total_failure and _RUN_REPORT is not None:
        # The repos that came through whole. Counted against the declared
        # list rather than by incrementing as we go, so a repo flagged for
        # two separate reasons is still one repo missing from the total —
        # and so the message reads "processed=11, repo_download_failed=1"
        # rather than "nothing to do" on a run that evaluated the fleet.
        _RUN_REPORT.ok(
            sum(
                1
                for _svc in active_repos
                if _svc.get("id") and _svc["id"] not in _RUN_FLAGGED
            )
        )
        # Delivery failure is an issue like any other, so a run that
        # posted nine of ten batches is WARN for the same reason a run
        # that skipped a repo is.
        if _RUN_TALLY.failed:
            _RUN_REPORT.issue("delivery_failed", f"{_RUN_TALLY.failed} finding(s)")
        _RUN_REPORT.count("flow", flow_label)
        _RUN_REPORT.count("offered", _RUN_TALLY.attempted)
        _RUN_REPORT.count("posted", _RUN_TALLY.posted)
        _RUN_REPORT.count("duplicate", _RUN_TALLY.duplicates)
        # A run that evaluated nothing had nothing to say. A run that
        # offered findings reports either way — "162 offered, 0 posted"
        # and "162 offered, 162 posted" must not look alike from outside,
        # which is the whole lesson of September 3rd.
        _RUN_REPORT.send(notable=_RUN_TALLY.attempted > 0)
    # Last statement in the flow, deliberately: everything above has
    # already run and reported, and this only decides whether the run is
    # allowed to be called a success. Raising here marks the run Failed,
    # fires the failure hooks, and stops _on_completion from pinging
    # Healthchecks green for a run that delivered nothing.
    _assert_findings_were_delivered(prefect_log)
