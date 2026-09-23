"""Conformance checking, and the two shapes it is asked for.

``handler(event)`` is the unit of work: one repository, everything needed
to evaluate it passed in, nothing looked up. A release triggers one
through the API; a whole-fleet pass is N of them, dispatched by the API
from the registry. It does not know which of those it is serving, and
that is the point.

``run_introspection()`` is the rest of a fleet pass: the six checks that
carry ``applies_to: None`` and grade the inventory, the stored findings
and the catalog itself. No repository owns them, so they get their own
job.

``run_fleet_sweep()`` used to be here — one message the evaluator expanded
into a serial loop over the fleet. The API fans out instead, so a failure
retries one repository rather than redelivering a pass that re-evaluates
everything that already succeeded. The registry-to-events translation it
did now lives in api-kaianolevine-com's ``services/fleet_registry.py``.

Two modes, in both shapes:

mode='deterministic' (the default, and the release path):
  Rule checks only. No LLM calls, no token cost.
  Posts findings with source='conformance_deterministic'.
  run_id prefix: 'deterministic-{version}-{uuid}'

mode='llm':
  Deterministic pass first to get checked_rule_ids, then the LLM for
  soft-rule assessment. Posts LLM findings only.
  Posts findings with source='conformance_llm'.
  run_id prefix: 'conformance-{version}-{uuid}'

The sweep additionally runs the applies_to-absent checks once per pass:
  EVAL-003 posts with source='data_quality' (runtime data-quality on
  stored findings).
  EVAL-007 posts with source='standards_drift' (catalog vs evaluator).
These have no repository to attach to, so a per-repository invoke is not
a place they could run.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from mini_app_polis import logger as logger_mod
from mini_app_polis.pipeline_status import RunReport

from evaluator_cog._version import __version__
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

#: How a 404 is written into the not-evaluated row, and read back out.
#:
#: XSTACK-008 must never collapse "I could not tell" into "it is not
#: there": a 403, 429, 5xx or timeout means the run could not determine
#: whether the repository exists, and only a 404 says it does not. That
#: distinction used to live solely in ``RunContext.unresolved_downloads``,
#: in the memory of the process that did the download — which was fine
#: for a sweep that ran the checks at the end of its own loop, and is not
#: fine now. Under fan-out each repository is a separate job with its own
#: context, so no single one sees the fleet, and the introspection pass
#: that runs the check sees none of them.
#:
#: So the 404 goes into the row's text, in a shape that can be parsed
#: back. The phrasing is load-bearing; change it here and in the pattern
#: below together.
_NOT_FOUND_REASON_FMT = "the repository does not exist at {org}/{repo}@{ref} (404)"
_NOT_FOUND_RE = re.compile(
    r"does not exist at (?P<org>[^/\s]+)/(?P<repo>[^@\s]+)@(?P<ref>\S+) \(404\)"
)

#: This cog, as the notification channel and the version stamp know it.
#: Must match [project] name in pyproject.toml.
_REPO = "evaluator-cog"


def stamp_versions(text: str, standards_version: str = "") -> str:
    """Append the running code's version, and the catalog's, to ``text``.

    ``(processor=3.40.1, standards=4.2.0)`` on a line of its own. The
    library would stamp ``processor`` itself, but it reads the installed
    distribution's metadata and the Lambda deploy strips every
    ``*.dist-info`` from the zip — there the lookup fails and, by design,
    nothing is stamped. ``_version.py`` is source, ships in the zip and is
    built from the release tag, so it is the version that is running. The
    library skips its own stamp when the text already carries one.

    ``standards`` is omitted when not known — a job that died before the
    catalog resolved — rather than guessed. Empty text is left alone: the
    library skips an empty report, and a version alone is not a message.
    Any counters the library appends land after the stamp, so the last
    line of a message is its metadata.
    """
    if not text or "(processor=" in text:
        return text
    parts = [f"processor={__version__}"]
    if standards_version:
        parts.append(f"standards={standards_version}")
    return f"{text}\n({', '.join(parts)})"


@dataclass
class VersionedRunReport(RunReport):
    """A :class:`RunReport` that says which code, and which catalog, ran.

    ``standards_version`` is set by whatever resolves the catalog version
    the run grades against — :func:`handler`, :func:`run_introspection` —
    via :func:`_record_standards_version`.
    """

    standards_version: str = ""

    def text(self) -> str:
        """The library's message body, with the version stamp as its last line.

        Stamped here rather than at send time so every path that renders the
        report — :meth:`send`, and the tests that read it — sees the same
        text. See :func:`stamp_versions` for the format.
        """
        return stamp_versions(super().text(), self.standards_version)


def _record_standards_version(standards_version: str, *, ctx: RunContext) -> None:
    """Put the catalog version this run grades against on its report."""
    if isinstance(ctx.report, VersionedRunReport):
        ctx.report.standards_version = standards_version


@dataclass
class RunContext:
    """Everything one run accumulates, scoped to that run.

    Was four module globals and a lock. The globals meant two evaluations
    in one process corrupted each other's accounting — and not only under
    genuine concurrency. ``/invoke`` reset them from the request thread
    while a previously accepted evaluation was still running under that
    lock, so a second release arriving mid-evaluation wiped the first
    one's tally and report before either got near a write. The lock
    serialised the work and not the state it was protecting.

    One of these per job. The caller constructs it, because only the
    caller knows what a job is: one repository for a release, the whole
    fleet for a sweep. Passing it explicitly rather than reaching for it
    is the point — a function that needs a run says so in its signature,
    and omitting it is a :class:`TypeError` rather than silent
    cross-talk between two runs.
    """

    #: Every ``post_findings`` outcome in this run.
    #:
    #: A run that computes findings and delivers none of them is a
    #: systemic fault — no route, no credential, no service — not N
    #: unlucky findings, and it must fail the run rather than log a
    #: warning. Before this, the 2026-09-03 runs computed ~162 findings
    #: across 13 repos, posted zero, and still finished Completed with a
    #: green Healthchecks ping, because ``post_findings`` swallowed every
    #: error and the "posted N findings" log line reported the length of
    #: the list handed over rather than what the API accepted.
    tally: PostResult = field(default_factory=PostResult)

    #: Coverage, as opposed to delivery. :attr:`tally` answers "did the
    #: findings reach the API"; this answers "was every declared repo
    #: actually looked at, and looked at completely". A run can be
    #: perfect on the first and wrong on the second, which is what a
    #: green SUCCESS on a run that silently skipped three repos looks
    #: like.
    #:
    #: ``None`` on a context with no report to send — tests, one-off
    #: scripts — so the helpers below write nowhere rather than into a
    #: report nobody will read. :meth:`for_run` builds one that will.
    report: RunReport | None = None

    #: Every repo name flagged by :func:`_report_issue` this run, so the
    #: message can say how many repos came through clean without counting
    #: a flagged one twice when two things went wrong with it.
    #:
    #: Holds whatever string the call site had — a declared service id in
    #: most places, a repo name in :func:`_download_repo`. Only
    #: the intersection with declared service ids is ever counted, so the
    #: entries that name no service are ignored rather than skewing the
    #: total.
    flagged: set[str] = field(default_factory=set)

    #: Registry entries whose download returned 404 this run, as
    #: ``{"label": "<org>/<repo>", "url": <zipball url>}``. Only a 404
    #: lands here: a 403, 429, 5xx or timeout means the run could not tell
    #: whether the repo exists, which is not the same fact and must not be
    #: reported as one. XSTACK-008 reads this at the end of the run.
    unresolved_downloads: list[dict[str, str]] = field(default_factory=list)

    #: The catalog for this run. One fetch, reused by every caller, and
    #: scoped to the run rather than to the process so a long-lived worker
    #: grades against the release current when the job starts rather than
    #: whatever was current when it booted.
    catalog: dict | None = None

    @classmethod
    def for_run(cls, flow_name: str = "conformance-check") -> RunContext:
        """A context whose coverage report will be sent when the run ends.

        ``flow_name`` is what the report is filed and titled under, and it
        should match the name this run's *findings* carry — see
        :func:`flow_name_for_mode`. It was hardcoded to the llm name, so a
        deterministic run announced itself as "conformance-check" while
        writing rows under "deterministic-conformance", and correlating
        the notification with the rows meant knowing that.
        """
        return cls(report=VersionedRunReport(flow_name=flow_name, repo=_REPO))


def _report_issue(
    reason: str,
    repo_id: str,
    exc: BaseException | None = None,
    *,
    ctx: RunContext,
) -> None:
    """Flag a repo this run did not fully evaluate. Makes the run WARN."""
    if ctx.report is None:
        return
    detail = f"{type(exc).__name__}: {exc}" if exc is not None else None
    ctx.report.issue(reason, repo_id, detail=detail)
    ctx.flagged.add(repo_id)


def _report_note(reason: str, repo_id: str, *, ctx: RunContext) -> None:
    """Record an ordinary skip. Counted in the message, severity unchanged."""
    if ctx.report is not None:
        ctx.report.note(reason, repo_id)


def _post_tracked(
    label: str,
    prefect_log: Any = None,
    *,
    ctx: RunContext,
    **kwargs: Any,
) -> PostResult:
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
    ctx.tally.merge(result)
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
            result.attempted,
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


def _assert_findings_were_delivered(prefect_log: Any, *, ctx: RunContext) -> None:
    """Fail the run when nothing reached the API.

    Raising is the point. The caller stops before pinging Healthchecks.io,
    and the adapter reports an ERROR to the notification channel — so a run
    that delivered nothing cannot read as a healthy one from outside. A
    partial failure has already been warned about per emitter and does not
    fail the run.
    """
    if not ctx.tally.total_failure:
        return
    raise FindingDeliveryError(
        f"{ctx.tally.attempted} findings were computed and none reached "
        f"api-kaianolevine-com. The evaluation itself ran; delivery did "
        f"not. Check KAIANO_API_BASE_URL and EVALUATOR_COG_API_KEY on "
        f"this service. Last error: {ctx.tally.last_error}"
    )


def _ping_healthcheck() -> None:
    """Tell Healthchecks.io a fleet pass finished clean. Never raises.

    Called at the tail of :func:`run_introspection`, which is the
    once-per-pass job now that the sweep is gone. It was the sweep's tail
    before that, and a Prefect ``on_completion`` hook before that — which
    is why it is a ping and not a check: the fact being reported is that a
    pass reached its end, and a per-repository job is not that fact.

    Moving it mattered. Healthchecks.io watches for *absence*, so deleting
    the sweep without rehoming this would have stopped the pings and fired
    the check — reporting the evaluator dead at the moment it started
    working properly.
    """
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
        r = _get_with_retry(url, timeout=timeout)
        r.raise_for_status()
        return yaml.safe_load(r.text) or {}
    except Exception as exc:
        log.warning("conformance: failed to fetch %s: %s", url, exc)
        return {}


def _fetch_catalog(*, ctx: RunContext) -> dict:
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
    if ctx.catalog is not None:
        return ctx.catalog
    timeout = float(os.environ.get("EVALUATOR_HTTP_TIMEOUT_SECONDS", "20"))
    try:
        response = _get_with_retry(
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
    ctx.catalog = catalog
    return catalog


def _catalog_rules(*, ctx: RunContext) -> list[dict]:
    """Every rule the evaluator will consider.

    ``checkable: false`` rules are filtered here. The catalog carries them
    so they stay readable and joinable to the findings of versions that did
    check them, but there is no check to run and nothing is emitted for
    them — see the ``gap`` status in index.yaml.
    """
    return [rule for rule in _fetch_catalog(ctx=ctx)["rules"] if rule.get("checkable")]


def _get_standards_version(*, ctx: RunContext) -> str:
    """The version of the catalog under evaluation. Raises on failure."""
    version = str(_fetch_catalog(ctx=ctx).get("version") or "")
    if not version:
        raise RuntimeError("Standards catalog carries no version")
    return version


def _fetch_catalog_schema(*, ctx: RunContext) -> dict:
    """Traits, repo types and statuses, in the shapes the dispatcher expects.

    The catalog carries these already resolved; this only reshapes them.
    """
    catalog = _fetch_catalog(ctx=ctx)
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


def _fetch_full_rule_catalog(*, ctx: RunContext) -> dict[str, dict]:
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
        for rule in _catalog_rules(ctx=ctx)
        if rule.get("id")
    }


def _fetch_standards_for_service(
    service: dict,
    evaluator_cfg: EvaluatorConfig | None = None,
    *,
    ctx: RunContext,
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
    for rule in _catalog_rules(ctx=ctx):
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


#: Statuses worth another attempt: GitHub's secondary rate limit (403),
#: throttling (429) and server-side failures. Anything else — a 404, a 401 —
#: is an answer, and asking again cannot change it.
_RETRYABLE_STATUS = frozenset({403, 429, 500, 502, 503, 504})


def _get_with_retry(
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """GET ``url`` with the zipball download's retry policy.

    Returns the last response, whatever its status — the caller decides
    what a non-2xx means, as it did before. Raises the last transport
    error if no attempt got a response at all.

    The catalog fetch and the ecosystem.yaml fetch were single attempts.
    One 503 from the API behind the catalog failed the whole run, and the
    queue's redelivery re-ran it from the top minutes later — the right
    backstop for a run that cannot succeed, the wrong answer to a blip.
    """
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        last = attempt == _DOWNLOAD_ATTEMPTS - 1
        response: httpx.Response | None = None
        try:
            response = httpx.get(url, timeout=timeout, headers=headers)
        except httpx.TransportError as exc:
            if last:
                raise
            detail = f"{type(exc).__name__}: {exc}"
        else:
            if last or response.status_code not in _RETRYABLE_STATUS:
                return response
            detail = f"HTTP {response.status_code}"
        delay = _retry_delay(response, attempt)
        log.warning(
            "conformance: GET %s failed (%s) — retrying in %.1fs (attempt %d of %d)",
            url,
            detail,
            delay,
            attempt + 2,
            _DOWNLOAD_ATTEMPTS,
        )
        time.sleep(delay)
    raise AssertionError("unreachable: the last attempt returns or raises")


def _fetch_zipball(
    url: str,
    headers: dict[str, str],
    timeout: float,
    repo_id: str,
    *,
    ctx: RunContext,
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
                    ctx.unresolved_downloads.append({"label": repo_id, "url": url})
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
    repo_id: str,
    tmp_dir: str,
    branch: str = "main",
    org: str = _DEFAULT_ORG,
    *,
    ctx: RunContext,
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
        content = _fetch_zipball(url, headers, timeout, f"{org}/{repo_id}", ctx=ctx)
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
        _report_issue("repo_download_failed", repo_id, exc, ctx=ctx)
        return None


def run_conformance_check(
    *,
    ctx: RunContext,
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
    # Named for the run logger it used to resolve. There is no Prefect
    # runtime to resolve one from any more, and the shared logger is what
    # reaches the service's stdout either way.
    prefect_log = log

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
        _report_issue("deterministic_checks_failed", repo_id, exc, ctx=ctx)

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
            _report_issue("llm_assessment_failed", repo_id, exc, ctx=ctx)
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
        _report_note("llm_skipped_no_api_key", repo_id, ctx=ctx)

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
            ctx=ctx,
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
    *,
    ctx: RunContext,
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

    # evaluator.yaml from the cloned repo, falling back to the ecosystem
    # record.
    evaluator_cfg = load_evaluator_config(
        repo_path,
        fallback_type=service.get("type") or dod_type,
        fallback_exceptions=check_exceptions,
        fallback_exception_reasons=exception_reasons,
        rule_catalog=rule_catalog,
        catalog_schema=catalog_schema,
    )

    standards_rules = _fetch_standards_for_service(service, evaluator_cfg, ctx=ctx)
    try:
        all_findings = run_conformance_check(
            ctx=ctx,
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
        _report_issue("repo_check_failed", repo_id, exc, ctx=ctx)


def _run_suffix() -> str:
    """A suffix no two runs share.

    Was the Prefect flow run id, falling back to a UTC timestamp. Both
    ends of that are gone: there is no flow run to ask, and a timestamp
    resolved to the second is not unique between two releases that land
    together — which is exactly what the release trigger makes ordinary.
    Two runs sharing a run_id merge their findings into one.
    """
    return uuid.uuid4().hex[:12]


def _build_conformance_run_id(standards_version: str) -> str:
    """Build a per-execution run_id for conformance findings."""
    unique_suffix = _run_suffix()
    return f"conformance-{standards_version}-{unique_suffix}"


def _post_not_evaluated(
    repo_id: str,
    reason: str,
    *,
    ctx: RunContext,
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
        ctx=ctx,
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
    unique_suffix = _run_suffix()
    return f"deterministic-{standards_version}-{unique_suffix}"


def _post_service_findings(
    repo_id: str,
    findings: list[dict],
    *,
    ctx: RunContext,
    standards_version: str,
    run_id: str,
    flow_name: str,
    prefect_log: Any,
) -> None:
    """Deliver one service's deterministic findings.

    A service with nothing to report gets the SUCCESS row rather than
    silence: "evaluated and clean" and "not evaluated" must not look alike
    from the outside.
    """
    if not findings:
        findings = [
            {
                "rule_id": "STATUS",
                "dimension": "structural_conformance",
                "severity": "SUCCESS",
                "finding": (
                    f"{repo_id} passed all deterministic checks for "
                    f"standards v{standards_version}."
                ),
                "suggestion": "",
            }
        ]
    _post_tracked(
        repo_id,
        prefect_log,
        ctx=ctx,
        findings=findings,
        run_id=run_id,
        repo=repo_id,
        flow_name=flow_name,
        source="conformance_deterministic",
        standards_version=standards_version,
    )


def _evaluate_service_deterministic(
    service: dict,
    repo_path: Path,
    standards_version: str,
    run_id: str,
    prefect_log: Any,
    rule_catalog: dict[str, dict] | None = None,
    catalog_schema: dict | None = None,
    *,
    ctx: RunContext,
) -> list[dict] | None:
    """Compute one service's deterministic findings. Does not deliver them.

    Returns the findings, or ``None`` when the service could not be
    evaluated — in which case a not-evaluated row has already been posted,
    so the caller has nothing left to report.

    Computing and delivering are separate so the caller decides when
    findings are sent.
    """
    repo_id = service.get("id", "")
    if not repo_id:
        return None

    service_type = service.get("type", "worker")
    _raw_language = str(service.get("language") or "python")
    language = "typescript" if _raw_language == "astro" else _raw_language
    cog_subtype = str(service.get("cog_subtype") or "").strip() or None
    dod_type = service.get("dod_type")
    raw_exc = service.get("check_exceptions") or []
    check_exceptions, exception_reasons = _parse_check_exceptions(raw_exc)

    # evaluator.yaml from the cloned repo, falling back to the ecosystem
    # record.
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
            evaluator_config=evaluator_cfg,
            rule_catalog=rule_catalog,
            catalog_schema=catalog_schema,
            progress=lambda note: prefect_log.info(
                "deterministic: %s: %s", repo_id, note
            ),
        )
    except Exception as exc:
        prefect_log.warning(
            "deterministic: run_all_checks failed for %s: %s", repo_id, exc
        )
        _post_not_evaluated(
            repo_id,
            f"the deterministic checks raised ({type(exc).__name__}: {exc})",
            ctx=ctx,
            run_id=run_id,
            flow_name="deterministic-conformance",
            source="conformance_deterministic",
            standards_version=standards_version,
            prefect_log=prefect_log,
        )
        return None

    prefect_log.info(
        "deterministic: %d findings for %s (%.1fs)",
        len(result.findings),
        repo_id,
        time.monotonic() - _repo_started,
    )
    return result.findings


def _run_standalone_deterministic(
    service: dict,
    repo_path: Path,
    standards_version: str,
    run_id: str,
    prefect_log: Any,
    rule_applies_to: dict[str, list[str]] | None = None,
    rule_catalog: dict[str, dict] | None = None,
    catalog_schema: dict | None = None,
    *,
    ctx: RunContext,
) -> None:
    """Evaluate one service and post immediately.

    Computes and delivers in one step.
    """
    repo_id = service.get("id", "")
    if not repo_id:
        return
    findings = _evaluate_service_deterministic(
        service,
        repo_path,
        standards_version,
        run_id,
        prefect_log,
        rule_catalog=rule_catalog,
        catalog_schema=catalog_schema,
        ctx=ctx,
    )
    if findings is None:
        return
    _post_service_findings(
        repo_id,
        findings,
        ctx=ctx,
        standards_version=standards_version,
        run_id=run_id,
        flow_name="deterministic-conformance",
        prefect_log=prefect_log,
    )


#: How many checks carry ``applies_to: None`` (ADR-004): EVAL-003,
#: XSTACK-006, XSTACK-007, XSTACK-008 and EVAL-007. Declared so a run can
#: say "four of five ran" rather than reporting a partial pass as a whole
#: one. Adding a check to the lane means changing this too.
_APPLIES_TO_ABSENT_CHECKS = 5


def _run_applies_to_absent_checks(
    *,
    ctx: RunContext,
    ecosystem: dict,
    rule_catalog: dict[str, dict],
    standards_version: str,
    evaluator_standards_version: str,
    run_id: str,
    prefect_log: Any,
) -> int:
    """Run applies_to-absent checks once per flow invocation.

    Returns how many of them completed. Each is wrapped individually — a
    check that raises is logged and the rest still run — so the count is
    the only thing that distinguishes "five checks found nothing" from "five
    checks all blew up", which are the same silence from outside.
    """
    completed = 0
    from evaluator_cog.engine.deterministic import (
        check_eval_003,
        check_eval_007,
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
                ctx=ctx,
                findings=eval_003_findings,
                run_id=run_id,
                repo="ecosystem-standards",
                flow_name="eval-003",
                source="data_quality",
                standards_version=standards_version,
            )
        completed += 1
    except Exception as exc:
        prefect_log.warning("EVAL-003: check failed: %s", exc)

    # XSTACK-006 / XSTACK-007 — cross-repo coherence.
    #
    # Both carry `applies_to: None`, so resolve_dispatch returns
    # SKIP_SCOPE for them on every repo and they can never run on the
    # per-repo path. That is correct: their read sources are the GitHub
    # org listing and the ecosystem.yaml registry, not any one repo's
    # source tree. This lane is where a rule with no single repo subject
    # belongs, which is why EVAL-003 already lives here.
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
                    ctx=ctx,
                    findings=_findings,
                    run_id=run_id,
                    repo="ecosystem-standards",
                    flow_name=_rule_id.lower(),
                    source="standards_drift",
                    standards_version=standards_version,
                )
            completed += 1
        except Exception as exc:
            prefect_log.warning("%s: check failed: %s", _rule_id, exc)

    # XSTACK-008 — every registered repo resolved where the registry says.
    #
    # Reads this run's own download results rather than the registry
    # alone, and adds no GitHub calls: the downloads already happened.
    # It runs after every repo has been attempted, so the record is
    # complete by the time it is read.
    try:
        xstack_008_findings = check_xstack_008(unresolved=ctx.unresolved_downloads)
        if xstack_008_findings:
            _post_tracked(
                "XSTACK-008",
                prefect_log,
                ctx=ctx,
                findings=xstack_008_findings,
                run_id=run_id,
                repo="ecosystem-standards",
                flow_name="xstack-008",
                source="standards_drift",
                standards_version=standards_version,
            )
        completed += 1
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
                ctx=ctx,
                findings=eval_007_findings,
                run_id=run_id,
                repo="ecosystem-standards",
                flow_name="eval-007",
                source="standards_drift",
                standards_version=standards_version,
            )
        completed += 1
    except Exception as exc:
        prefect_log.warning("EVAL-007: check failed: %s", exc)

    return completed


def flow_name_for_mode(mode: str) -> str:
    """What a run of ``mode`` files its findings under.

    One definition, because two drifted: ``handler`` derives this for the
    rows and the adapter needs the same string for the run report, and a
    report titled differently from the rows it describes is a join nobody
    can make from the outside.
    """
    return "conformance-check" if mode == "llm" else "deterministic-conformance"


@dataclass(frozen=True)
class EvaluationEvent:
    """One repository to evaluate, and everything needed to do it.

    The whole input. No flow state, no registry lookup, no ambient
    configuration — which is what lets the same function serve a
    scheduled sweep and an HTTP invoke without knowing which it is.

    A repository, not a service: ``services`` is every service the
    repository carries — in practice, one.
    """

    org: str
    repo: str
    ref: str
    services: tuple[dict, ...]
    run_id: str
    mode: str = "deterministic"
    #: The catalog version the dispatcher pinned, when it pinned one.
    #:
    #: Empty for a release-triggered evaluation, which resolves the
    #: version itself and should: one repository graded against whatever
    #: is published when it runs is correct.
    #:
    #: Set for one repository within a fan-out, where it is the opposite.
    #: N messages each resolving their own version means a catalog release
    #: landing mid-pass grades some repositories against the old rules and
    #: some against the new, inside a run id that claims one version for
    #: all of them. The dispatcher resolves it once so the pass is
    #: internally consistent.
    standards_version: str = ""


@dataclass
class EvaluationResult:
    """What one invocation did. Counts, not findings — those went to the API."""

    repo: str
    evaluated: list[str] = field(default_factory=list)
    not_evaluated: list[str] = field(default_factory=list)


def handler(event: EvaluationEvent, *, log: Any, ctx: RunContext) -> EvaluationResult:
    """Evaluate one repository. The unit of work, and the whole of it.

    Downloads the ref into its own temporary directory, evaluates every
    service the repository carries, delivers the findings, and cleans up.
    Everything it needs comes from the event and the published catalog.

    Deliberately self-sufficient rather than handed its context: the
    catalog fetch is cached for the run, so re-deriving the schema and
    rule catalog here costs a dictionary rebuild and buys a function that
    can be called by an HTTP adapter with nothing else in scope. That is
    the point of the shape — the later move to a function runtime is a new
    caller, not a rewrite.

    Never raises. A repository that could not be downloaded, a service
    whose declared path is absent, and a check that blew up are all
    reported as not-evaluated rows rather than exceptions, because the
    caller cannot do anything with them that the report does not already
    say — and because a run that skipped a repository silently is the
    failure this evaluator has been bitten by most.
    """
    # Attribute the coverage report to the run whose findings it
    # describes. Without this the report falls back to
    # mini_app_polis.pipeline_status.get_run_id(), whose resolution order
    # is Prefect's — so with Prefect gone every report this cog sent
    # arrived as "local-run", joinable to nothing. The sweep sets this
    # first because it owns the run; a per-repository invoke sets it here.
    if ctx.report is not None and not ctx.report.run_id:
        ctx.report.run_id = event.run_id

    standards_version = event.standards_version or _get_standards_version(ctx=ctx)
    _record_standards_version(standards_version, ctx=ctx)
    catalog_schema = _fetch_catalog_schema(ctx=ctx)
    rule_catalog = _fetch_full_rule_catalog(ctx=ctx)
    rule_applies_to = {
        rule_id: meta["applies_to"]
        for rule_id, meta in rule_catalog.items()
        if isinstance(meta, dict) and isinstance(meta.get("applies_to"), list)
    }

    run_llm = event.mode == "llm"
    flow_name = flow_name_for_mode(event.mode)
    source = "conformance_check" if run_llm else "conformance_deterministic"

    result = EvaluationResult(repo=event.repo)
    service_ids = [
        str(service.get("id") or "") for service in event.services if service.get("id")
    ]
    if not service_ids:
        return result

    with tempfile.TemporaryDirectory() as tmp_dir:
        root = _download_repo(event.repo, tmp_dir, event.ref, event.org, ctx=ctx)
        if root is None:
            # One failed download hides every service inside it. The row
            # goes against each of them rather than against the repository.
            for service_id in service_ids:
                log.warning(
                    "%s: skipping %s — could not download %s@%s",
                    event.mode,
                    service_id,
                    event.repo,
                    event.ref,
                )
                _report_issue("repo_download_failed", service_id, ctx=ctx)
                # A 404 and an unreachable GitHub are both "not
                # evaluated", and only the first is evidence the registry
                # is wrong. _download_repo records a 404 on the context;
                # anything else it could not tell apart from a bad day.
                not_found = any(
                    entry.get("label") == f"{event.org}/{event.repo}"
                    for entry in ctx.unresolved_downloads
                )
                reason = (
                    _NOT_FOUND_REASON_FMT.format(
                        org=event.org, repo=event.repo, ref=event.ref
                    )
                    if not_found
                    else (
                        f"the repository could not be downloaded "
                        f"({event.repo}@{event.ref})"
                    )
                )
                _post_not_evaluated(
                    service_id,
                    reason,
                    ctx=ctx,
                    run_id=event.run_id,
                    flow_name=flow_name,
                    source=source,
                    standards_version=standards_version,
                    prefect_log=log,
                )
                result.not_evaluated.append(service_id)
            return result

        findings_by_service: dict[str, list[dict[str, Any]]] = {}

        for service in event.services:
            service_id = str(service.get("id") or "")
            if not service_id:
                continue

            log.info("%s: processing %s", event.mode, service_id)

            try:
                if run_llm:
                    _run_standalone_conformance(
                        service,
                        root,
                        standards_version,
                        event.run_id,
                        log,
                        rule_applies_to=rule_applies_to,
                        rule_catalog=rule_catalog,
                        catalog_schema=catalog_schema,
                        ctx=ctx,
                    )
                    result.evaluated.append(service_id)
                else:
                    computed = _evaluate_service_deterministic(
                        service,
                        root,
                        standards_version,
                        event.run_id,
                        log,
                        rule_catalog=rule_catalog,
                        catalog_schema=catalog_schema,
                        ctx=ctx,
                    )
                    if computed is None:
                        # It reported its own failure; nothing left to say.
                        result.not_evaluated.append(service_id)
                    else:
                        findings_by_service[service_id] = computed
            except Exception as exc:
                # Anything around the checks rather than inside them —
                # loading evaluator.yaml, parsing check_exceptions. This
                # used to log and post nothing, which is the same
                # invisible absence by a different route.
                log.error(
                    "%s: unhandled error processing %s — skipping: %s",
                    event.mode,
                    service_id,
                    exc,
                    exc_info=True,
                )
                _post_not_evaluated(
                    service_id,
                    f"processing raised before findings could be computed "
                    f"({type(exc).__name__}: {exc})",
                    ctx=ctx,
                    run_id=event.run_id,
                    flow_name=flow_name,
                    source=source,
                    standards_version=standards_version,
                    prefect_log=log,
                )
                result.not_evaluated.append(service_id)

        if not run_llm:
            for service_id, service_findings in findings_by_service.items():
                _post_service_findings(
                    service_id,
                    service_findings,
                    ctx=ctx,
                    standards_version=standards_version,
                    run_id=event.run_id,
                    flow_name=flow_name,
                    prefect_log=log,
                )
                result.evaluated.append(service_id)

    return result


def _unresolved_from_run(pass_run_id: str, *, log: Any) -> list[dict[str, str]]:
    """Rebuild XSTACK-008's input from what a fan-out pass recorded.

    The check reads a list of 404s. Under the sweep that list was built in
    memory as the loop downloaded each repository; under fan-out there is
    no such loop and no shared memory, so it is read back out of the rows
    those jobs posted.

    Only 404s come back. Rows written for an unreachable GitHub do not
    match the pattern and are left where they are, which is the whole
    point — see :data:`_NOT_FOUND_REASON_FMT`.

    An empty list on failure, not an exception. A pass whose rows cannot
    be read should not take the other five checks down with it, and
    XSTACK-008 finding nothing is the same answer it gives when every
    repository resolved. That is a real weakness of this path and worth
    naming: it can only under-report.
    """
    if not pass_run_id:
        return []

    from mini_app_polis.api import KaianoApiClient

    try:
        api = KaianoApiClient.from_env(_REPO)
        response = api.get(f"/v1/evaluations?run_id={pass_run_id}&limit=500")
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        log.warning(
            "introspection: could not read run %s for XSTACK-008: %s",
            pass_run_id,
            exc,
        )
        return []

    if isinstance(response, dict):
        rows = response.get("data") or response.get("items") or []
    elif isinstance(response, list):
        rows = response
    else:
        rows = []

    unresolved: list[dict[str, str]] = []
    for row in rows:
        match = _NOT_FOUND_RE.search(str(row.get("finding") or ""))
        if not match:
            continue
        org, repo, ref = match.group("org"), match.group("repo"), match.group("ref")
        entry = {
            "label": f"{org}/{repo}",
            # Rebuilt rather than stored. The check parses org, repo and
            # branch back out of this with its own regex, and handing it
            # the shape it already understands keeps it a pure function
            # over a list rather than something that knows about rows.
            "url": f"https://api.github.com/repos/{org}/{repo}/zipball/{ref}",
        }
        if entry not in unresolved:
            unresolved.append(entry)

    log.info(
        "introspection: %d registered repo(s) did not resolve in run %s",
        len(unresolved),
        pass_run_id,
    )
    return unresolved


def run_introspection(
    *,
    run_id: str,
    pass_run_id: str = "",
    standards_version: str = "",
    log: Any,
    ctx: RunContext,
) -> None:
    """Run the checks that are scoped to no repository at all.

    EVAL-003, XSTACK-006, XSTACK-007, XSTACK-008 and EVAL-007
    carry ``applies_to: None`` (ADR-004). They grade the inventory, the
    stored findings and the catalog itself, so there is no per-repository
    invocation any of them belongs to — which is why they lived at the tail
    of the sweep, the one place in the old design that ran once per pass.

    Fan-out removed that place. This is its replacement: its own job, its
    own message, dispatched deliberately rather than on a schedule.

    ``pass_run_id`` names the fan-out pass to grade, and only XSTACK-008
    uses it. Omitted, five of the six checks still run correctly against
    the registry, the catalog and the stored findings; XSTACK-008 reports
    nothing, which is indistinguishable from every repository resolving.
    Pass it whenever there is a pass to name.
    """
    ctx.catalog = None

    # Attribute the report to this run. Without it RunReport falls back to
    # mini_app_polis.pipeline_status.get_run_id(), whose resolution order
    # is Prefect's — and with Prefect gone that always lands on
    # "local-run", joinable to nothing. The repository path sets this in
    # handler and the sweep sets it before its loop; this is the third
    # place that has to, and the first pass shipped without it.
    if ctx.report is not None:
        ctx.report.run_id = run_id

    standards_version = standards_version or _get_standards_version(ctx=ctx)
    _record_standards_version(standards_version, ctx=ctx)
    rule_catalog = _fetch_full_rule_catalog(ctx=ctx)
    ecosystem = _fetch_yaml(_ECOSYSTEM_YAML_URL)

    if not rule_catalog:
        log.warning(
            "introspection: full rule catalog empty — EVAL-007 will have "
            "nothing to compare against"
        )

    ctx.unresolved_downloads = _unresolved_from_run(pass_run_id, log=log)

    completed = _run_applies_to_absent_checks(
        ctx=ctx,
        ecosystem=ecosystem,
        rule_catalog=rule_catalog,
        standards_version=standards_version,
        evaluator_standards_version=standards_version,
        run_id=run_id,
        prefect_log=log,
    )

    # The checks that ran, so the report says what it did. Without this the
    # tally is empty and RunReport renders "nothing to do" — over a pass
    # that had just posted a finding, which is the opposite of true and
    # exactly the reading a quiet channel trains you to skim past.
    if ctx.report is not None:
        ctx.report.ok(completed)
        if completed < _APPLIES_TO_ABSENT_CHECKS:
            # A check that raised was logged and skipped, and the run went
            # on. Said here as well, because "five of six ran" and "six ran
            # and found nothing" are the same silence from outside.
            ctx.report.issue(
                "check_failed",
                f"{_APPLIES_TO_ABSENT_CHECKS - completed} of "
                f"{_APPLIES_TO_ABSENT_CHECKS} did not complete",
            )

    # Before the report, matching what the sweep did: the ping says the
    # pass reached its end, and it should not depend on a notification
    # channel being reachable.
    _ping_healthcheck()

    log.info(
        "introspection: complete — %d of %d checks, %d findings offered, "
        "%d posted, %d duplicate, %d failed",
        completed,
        _APPLIES_TO_ABSENT_CHECKS,
        ctx.tally.attempted,
        ctx.tally.posted,
        ctx.tally.duplicates,
        ctx.tally.failed,
    )
