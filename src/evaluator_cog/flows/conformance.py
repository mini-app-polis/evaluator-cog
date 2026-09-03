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
import json
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
from evaluator_cog.engine.routing import classify_check_mode

log = logger_mod.get_logger()

_ECOSYSTEM_YAML_URL = "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/ecosystem.yaml"
_STANDARDS_VERSION_URL = os.environ.get(
    "ECOSYSTEM_STANDARDS_VERSION_URL",
    "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/package.json",
)
_STANDARDS_BASE_URL = "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/standards"
_ECOSYSTEM_STANDARDS_INDEX_URL = os.environ.get(
    "ECOSYSTEM_STANDARDS_INDEX_URL",
    "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/index.yaml",
)
_VALID_RULE_STATUSES: frozenset[str] = frozenset({"requirement", "convention", "gap"})

# Canonical fallback list used only when index.yaml cannot be fetched.
# Keep this in sync with ecosystem-standards/index.yaml::files (any entry
# whose `file` begins with "standards/"). The runtime domain list is
# derived from index.yaml on every call; this list is the safety net.
_FALLBACK_STANDARDS_DOMAINS: tuple[str, ...] = (
    "api",
    "auth",
    "config",
    "cross-stack",
    "delivery",
    "documentation",
    "evaluation",
    "frontend",
    "meta",
    "monorepo",
    "pipeline",
    "principles",
    "python",
    "testing",
    "versioning",
)


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


def _reset_run_tally() -> None:
    """Start a fresh tally. Called once at the top of each flow run."""
    global _RUN_TALLY
    _RUN_TALLY = PostResult()


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


def _get_standards_version() -> str:
    """Fetch current standards version from live package.json. Raises on failure."""
    try:
        timeout = float(os.environ.get("EVALUATOR_HTTP_TIMEOUT_SECONDS", "20"))
        r = httpx.get(_STANDARDS_VERSION_URL, timeout=timeout)
        r.raise_for_status()
        data = json.loads(r.text) or {}
        version = data.get("version")
        if not version:
            raise ValueError("version field absent from package.json")
        return str(version)
    except Exception as exc:
        log.error(
            "conformance: failed to fetch standards version from package.json: %s", exc
        )
        raise RuntimeError(
            f"Cannot determine standards version — package.json fetch failed: {exc}"
        ) from exc


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


def _resolve_standards_domains() -> list[str]:
    """
    Return the list of standards domain names to fetch, sourced from
    ecosystem-standards/index.yaml. Falls back to a hardcoded canonical
    list if the index cannot be fetched or parsed.

    Only entries whose `file` begins with `standards/` are included —
    `ecosystem.yaml` and `definitions-of-done.yaml` are not rule catalogs
    and are fetched separately where needed.

    Never raises. Emits a single warning per process if any domain in the
    fallback list is missing from the live index (drift), and a single
    warning if any domain in the live index is missing from the fallback
    (evaluator is out of date vs the standards repo).
    """
    index = _fetch_yaml(_ECOSYSTEM_STANDARDS_INDEX_URL)
    raw_files = index.get("files") if isinstance(index, dict) else None
    if not isinstance(raw_files, list) or not raw_files:
        log.warning(
            "conformance: index.yaml fetch returned no files — "
            "using hardcoded fallback domain list"
        )
        return list(_FALLBACK_STANDARDS_DOMAINS)

    live_domains: list[str] = []
    for entry in raw_files:
        if not isinstance(entry, dict):
            continue
        file_path = str(entry.get("file") or "")
        if not file_path.startswith("standards/"):
            continue
        domain = str(entry.get("domain") or "").strip()
        if domain:
            live_domains.append(domain)

    if not live_domains:
        log.warning(
            "conformance: index.yaml had no standards/ entries — "
            "using hardcoded fallback domain list"
        )
        return list(_FALLBACK_STANDARDS_DOMAINS)

    live_set = set(live_domains)
    fallback_set = set(_FALLBACK_STANDARDS_DOMAINS)
    missing_from_live = fallback_set - live_set
    missing_from_fallback = live_set - fallback_set
    if missing_from_live:
        log.warning(
            "conformance: domains in fallback list are absent from live "
            "index.yaml — standards repo may have removed: %s",
            sorted(missing_from_live),
        )
    if missing_from_fallback:
        log.warning(
            "conformance: live index.yaml has domains not in evaluator "
            "fallback — evaluator fallback is stale, add these: %s",
            sorted(missing_from_fallback),
        )

    return live_domains


def _fetch_catalog_schema() -> dict:
    """Fetch structured schema data from index.yaml.

    Returns a dict with three keys:
      - traits: {trait_name: {"exempts": [...], "downgrades": [...],
                 "description": "..."}}
                Sourced from index.yaml schema.traits.
      - repo_types: set of valid repo type names.
                    Sourced from index.yaml schema.repo_types (keys).
      - statuses: set of valid rule status values.
                  Sourced from index.yaml statuses (keys).

    Never raises. Returns partial data on fetch or parse failure — the
    caller must handle missing keys. _fetch_yaml already logs a warning
    on transport failure.
    """
    index = _fetch_yaml(_ECOSYSTEM_STANDARDS_INDEX_URL)
    schema = (index.get("schema") or {}) if isinstance(index, dict) else {}

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
    repo_types: set[str] = set()
    if isinstance(raw_repo_types, dict):
        repo_types = {str(k) for k in raw_repo_types}

    raw_statuses = index.get("statuses") or {} if isinstance(index, dict) else {}
    statuses: set[str] = set()
    if isinstance(raw_statuses, dict):
        statuses = {str(k) for k in raw_statuses}

    return {
        "traits": traits,
        "repo_types": repo_types,
        "statuses": statuses,
    }


def _fetch_full_rule_catalog() -> dict[str, dict]:
    """Fetch every checkable rule's metadata from every standards file.

    Returns {rule_id: {"applies_to": list[str] | None, "modifies": list[str],
                       "status": str, "dimension": str,
                       "check_mode": "deterministic" | "llm"}}
    covering the entire catalog.

    `applies_to` is None when the rule omits the field entirely (v4.0.0
    semantics: the rule is not a repo-source scan — see ADR-004). An
    explicit empty list `[]` is also treated as None for dispatch
    purposes, though the catalog does not currently contain any such
    rules.

    `check_mode` is derived from the DETERMINISTIC CHECK. / LLM CHECK.
    marker on each rule's check_notes. Used by EVAL-007 to avoid
    flagging LLM-routed rules as "unimplemented" just because they
    have no deterministic CHECK_ID constant.

    Used by PR 3's dispatch to derive type-based scope from the live
    catalog rather than a hardcoded table. Used by PR 4 for modifier
    resolution.

    Never raises. Returns {} on full-catalog fetch failure.
    """
    domains = _resolve_standards_domains()
    catalog: dict[str, dict] = {}
    for domain in domains:
        url = f"{_STANDARDS_BASE_URL}/{domain}.yaml"
        data = _fetch_yaml(url)
        for rule in data.get("standards", []) or []:
            if not rule.get("checkable"):
                continue
            rule_id = str(rule.get("id") or "").strip()
            if not rule_id:
                continue
            raw_applies = rule.get("applies_to")
            applies_to: list[str] | None
            if raw_applies is None:
                applies_to = None  # ADR-004: non-repo-scan rule
            elif isinstance(raw_applies, list):
                applies_to = [str(x) for x in raw_applies]
            else:
                applies_to = None
            if applies_to == []:
                applies_to = None
            raw_modifies = rule.get("modifies") or []
            modifies = (
                [str(x) for x in raw_modifies if isinstance(x, str)]
                if isinstance(raw_modifies, list)
                else []
            )
            check_notes = str(rule.get("check_notes") or "").strip()
            catalog[rule_id] = {
                "applies_to": applies_to,
                "modifies": modifies,
                "status": str(rule.get("status") or "").strip(),
                "dimension": str(rule.get("dimension") or "").strip(),
                "check_mode": classify_check_mode(rule_id, check_notes),
            }
    return catalog


def _fetch_standards_for_service(
    service: dict, evaluator_cfg: EvaluatorConfig | None = None
) -> list[dict]:
    """
    Fetch checkable rules from all standards domains, filtered by
    the service's repo type using the applies_to field on each rule.
    Returns a list of rule dicts with id, title, severity, check_notes,
    check_mode. `check_mode` is one of "deterministic" or "llm", derived
    from the DETERMINISTIC CHECK / LLM CHECK marker on the rule's
    check_notes; rules missing the marker default to "deterministic".
    Never raises — returns [] on failure.
    """
    # Prefer new type from evaluator_config, fall back to dod_type for migration period
    repo_type = evaluator_cfg.repo_type if evaluator_cfg is not None else None

    dod_type = service.get("dod_type")
    all_rules = []
    domains = _resolve_standards_domains()

    def _to_rule_dict(rule: dict) -> dict:
        check_notes = (rule.get("check_notes") or "").strip()
        rule_id = str(rule.get("id") or "")
        status = str(rule.get("status") or "").strip()
        if status not in _VALID_RULE_STATUSES:
            raise ValueError(
                f"Rule {rule_id}: invalid status '{status}'. "
                f"Must be one of {sorted(_VALID_RULE_STATUSES)}. "
                f"The catalog is v4.0.0 — 'advisory' and 'idea' are "
                f"no longer valid values."
            )
        return {
            "id": rule_id,
            "title": rule.get("title", ""),
            "status": status,
            "severity": rule.get("severity", "INFO"),
            "check_notes": check_notes,
            "check_mode": classify_check_mode(rule_id, check_notes),
        }

    for domain in domains:
        url = f"{_STANDARDS_BASE_URL}/{domain}.yaml"
        data = _fetch_yaml(url)
        for rule in data.get("standards", []):
            if not rule.get("checkable"):
                continue
            applies_to = rule.get("applies_to", [])
            # "all" applies to every type
            if "all" in applies_to:
                all_rules.append(_to_rule_dict(rule))
                continue
            # Match on new repo type (v3.0.0) or legacy dod_type (migration period)
            if (repo_type and repo_type in applies_to) or (
                dod_type and dod_type in applies_to
            ):
                all_rules.append(_to_rule_dict(rule))
    return all_rules


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


def _download_repo(repo_id: str, tmp_dir: str) -> Path | None:
    """
    Download a repo from GitHub as a zip archive and extract it.
    Returns the extracted repo path or None on failure.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    url = f"https://api.github.com/repos/mini-app-polis/{repo_id}/zipball/main"
    dest = Path(tmp_dir) / repo_id

    try:
        timeout = float(os.environ.get("EVALUATOR_CLONE_TIMEOUT_SECONDS", "60"))
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
            r.raise_for_status()
            content = r.content  # capture before client context closes

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

        log.info("conformance: downloaded %s", repo_id)
        return dest
    except Exception as exc:
        log.warning("conformance: failed to download %s: %s", repo_id, exc)
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
    else:
        prefect_log.warning(
            "conformance: ANTHROPIC_API_KEY not set, skipping LLM assessment for %s",
            repo_id,
        )

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


@flow(name="conformance-check", log_prints=True, on_completion=[_on_completion])
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

                repo_path = _download_repo(repo_name, tmp_dir)
                if repo_path is None:
                    prefect_log.warning(
                        "%s: skipping %s — could not clone", flow_label, repo_id
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
                        rp = _download_repo(rname, tmp_dir)
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
                    continue

                repo_name = mono_record.get("repo") or mono_id
                prefect_log.info("%s: cloning monorepo %s", flow_label, repo_name)
                monorepo_root = _download_repo(repo_name, tmp_dir)
                if monorepo_root is None:
                    prefect_log.warning(
                        "%s: skipping monorepo %s — could not clone",
                        flow_label,
                        mono_id,
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
    # Last statement in the flow, deliberately: everything above has
    # already run and reported, and this only decides whether the run is
    # allowed to be called a success. Raising here marks the run Failed,
    # fires the failure hooks, and stops _on_completion from pinging
    # Healthchecks green for a run that delivered nothing.
    _assert_findings_were_delivered(prefect_log)
