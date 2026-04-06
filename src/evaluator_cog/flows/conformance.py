"""Conformance checking flow for evaluator-cog.

A single parameterized flow (conformance_check_flow) handles both modes:

run_llm=False (default, daily schedule):
  Runs deterministic rule checks only. No LLM calls. No token cost.
  Posts findings with source='conformance_deterministic'.
  run_id prefix: 'deterministic-{version}-{uuid}'

run_llm=True (triggered manually or via Prefect automation, weekly):
  Runs deterministic pass first to get checked_rule_ids, then calls
  the LLM for soft-rule assessment. Posts LLM findings only.
  Posts findings with source='conformance_check'.
  run_id prefix: 'conformance-{version}-{uuid}'
"""

from __future__ import annotations

import datetime
import io
import os
import shutil
import tempfile
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import yaml
from mini_app_polis import logger as logger_mod
from prefect import flow, get_run_logger
from prefect.concurrency.sync import concurrency

from evaluator_cog.engine.api_client import post_findings
from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.evaluator_config import EvaluatorConfig, load_evaluator_config
from evaluator_cog.engine.llm import (
    _anthropic_messages_create,
    _parse_findings_from_claude,
    build_conformance_prompt,
)

log = logger_mod.get_logger()

_ECOSYSTEM_YAML_URL = "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/ecosystem.yaml"
_INDEX_YAML_URL = "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/index.yaml"
_STANDARDS_BASE_URL = "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/standards"


def _on_completion(flow, flow_run, state) -> None:
    """Ping Healthchecks.io after successful conformance run. Never raises."""
    import urllib.request

    url = os.getenv("HEALTHCHECKS_URL_EVALUATOR", "").strip()
    if not url:
        return
    with suppress(Exception):
        urllib.request.urlopen(url, timeout=10)


def _fetch_yaml(url: str) -> dict:
    """Fetch and parse a YAML file from a URL. Never raises — returns {} on failure."""
    try:
        r = httpx.get(url, timeout=20.0)
        r.raise_for_status()
        return yaml.safe_load(r.text) or {}
    except Exception as exc:
        log.warning("conformance: failed to fetch %s: %s", url, exc)
        return {}


def _get_standards_version() -> str:
    """Fetch current standards version from live index.yaml. Raises on failure."""
    try:
        r = httpx.get(_INDEX_YAML_URL, timeout=20.0)
        r.raise_for_status()
        data = yaml.safe_load(r.text) or {}
        version = data.get("version")
        if not version:
            raise ValueError("version field absent from index.yaml")
        return str(version)
    except Exception as exc:
        log.error(
            "conformance: failed to fetch standards version from index.yaml: %s", exc
        )
        raise RuntimeError(
            f"Cannot determine standards version — index.yaml fetch failed: {exc}"
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


def _fetch_standards_for_service(service: dict, evaluator_cfg: EvaluatorConfig | None = None) -> list[dict]:
    """
    Fetch checkable rules from all standards domains, filtered by
    the service's repo type using the applies_to field on each rule.
    Returns a list of rule dicts with id, title, severity, check_notes.
    Never raises — returns [] on failure.
    """
    # Prefer new type from evaluator_config, fall back to dod_type for migration period
    if evaluator_cfg is not None:
        repo_type = evaluator_cfg.repo_type
    else:
        repo_type = None

    dod_type = service.get("dod_type")
    all_rules = []
    domains = [
        "python",
        "testing",
        "api",
        "pipeline",
        "frontend",
        "delivery",
        "documentation",
        "evaluation",
        "principles",
        "cross-stack",
        "monorepo",
    ]
    for domain in domains:
        url = f"{_STANDARDS_BASE_URL}/{domain}.yaml"
        data = _fetch_yaml(url)
        for rule in data.get("standards", []):
            if not rule.get("checkable"):
                continue
            applies_to = rule.get("applies_to", [])
            # "all" applies to every type
            if "all" in applies_to:
                all_rules.append(
                    {
                        "id": rule.get("id", ""),
                        "title": rule.get("title", ""),
                        "severity": rule.get("severity", "INFO"),
                        "check_notes": rule.get("check_notes", "").strip(),
                    }
                )
                continue
            # Match on new repo type (v3.0.0) or legacy dod_type (migration period)
            if repo_type and repo_type in applies_to:
                all_rules.append(
                    {
                        "id": rule.get("id", ""),
                        "title": rule.get("title", ""),
                        "severity": rule.get("severity", "INFO"),
                        "check_notes": rule.get("check_notes", "").strip(),
                    }
                )
            elif dod_type and dod_type in applies_to:
                all_rules.append(
                    {
                        "id": rule.get("id", ""),
                        "title": rule.get("title", ""),
                        "severity": rule.get("severity", "INFO"),
                        "check_notes": rule.get("check_notes", "").strip(),
                    }
                )
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
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
            r.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
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
) -> list[dict[str, Any]]:
    """
    Run deterministic + LLM conformance checks against a cloned repo.
    Posts findings to api-kaianolevine-com when post=True. Never raises.
    """
    prefect_log = get_run_logger()

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
                "severity": "INFO",
                "finding": f"{repo_id} passed all {'LLM' if post_llm_only else 'conformance'} checks for standards v{standards_version}.",
                "suggestion": "",
            }
        ]

    if post:
        post_findings(
            findings=findings_to_post,
            run_id=run_id,
            repo=repo_id,
            flow_name="conformance",
            source="conformance_check",
            standards_version=standards_version,
        )

    return all_findings


def _run_standalone_conformance(
    service: dict,
    repo_path: Path,
    standards_version: str,
    run_id: str,
    prefect_log: Any,
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
        )
        _ = all_findings
        prefect_log.info("conformance: LLM pass complete for %s (config: %s)", repo_id, evaluator_cfg.source)
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
    )
    # For monorepo apps the evaluator.yaml may live at the app path
    if monorepo_root and not (check_root / "evaluator.yaml").exists():
        evaluator_cfg = load_evaluator_config(
            repo_path,
            fallback_type=service.get("type") or dod_type,
            fallback_exceptions=check_exceptions,
            fallback_exception_reasons=exception_reasons,
        )

    prefect_log.info(
        "deterministic: %s using config from %s", repo_id, evaluator_cfg.source
    )

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
        )
        findings = result.findings
        prefect_log.info("deterministic: %d findings for %s", len(findings), repo_id)
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
                "severity": "INFO",
                "finding": f"{repo_id} passed all deterministic checks for standards v{standards_version}.",
                "suggestion": "",
            }
        ]

    post_findings(
        findings=findings,
        run_id=run_id,
        repo=repo_id,
        flow_name="deterministic-conformance",
        source="conformance_deterministic",
        standards_version=standards_version,
    )


@flow(name="conformance-check", log_prints=True, on_completion=[_on_completion])
def conformance_check_flow(run_llm: bool = False) -> None:
    """
    Clone each active repo and run conformance checks.

    When run_llm=False (default): deterministic checks only, no LLM calls.
    Posts findings with source='conformance_deterministic'. Runs daily.

    When run_llm=True: deterministic pass first (for checked_rule_ids),
    then LLM soft-rule assessment. Posts LLM findings only with
    source='conformance_check'. Triggered manually or via Prefect automation.
    """
    prefect_log = get_run_logger()
    flow_label = "conformance" if run_llm else "deterministic"

    standards_version = _get_standards_version()
    prefect_log.info("%s: standards version %s", flow_label, standards_version)

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

        with tempfile.TemporaryDirectory() as tmp_dir:
            for service in standalone_services:
                repo_id = service.get("id", "")
                repo_name = service.get("repo") or repo_id
                if not repo_id:
                    continue

                prefect_log.info("%s: processing %s", flow_label, repo_id)

                repo_path = _download_repo(repo_name, tmp_dir)
                if repo_path is None:
                    prefect_log.warning(
                        "%s: skipping %s — could not clone", flow_label, repo_id
                    )
                    continue

                if run_llm:
                    _run_standalone_conformance(
                        service, repo_path, standards_version, run_id, prefect_log
                    )
                else:
                    _run_standalone_deterministic(
                        service, repo_path, standards_version, run_id, prefect_log
                    )

            for mono_id, services in monorepo_service_groups.items():
                mono_record = monorepos_registry.get(mono_id)
                if not mono_record:
                    for svc in services:
                        rid = svc.get("id", "")
                        rname = svc.get("repo") or rid
                        if not rid:
                            continue
                        rp = _download_repo(rname, tmp_dir)
                        if rp is None:
                            continue
                        if run_llm:
                            _run_standalone_conformance(
                                svc, rp, standards_version, run_id, prefect_log
                            )
                        else:
                            _run_standalone_deterministic(
                                svc, rp, standards_version, run_id, prefect_log
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
                    monorepo_path = str(service.get("monorepo_path") or "")
                    if not repo_id:
                        continue

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
                    cog_subtype = str(service.get("cog_subtype") or "").strip() or None
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
                            )
                            findings_by_service[repo_id] = result.findings
                        except Exception as exc:
                            prefect_log.warning(
                                "deterministic: check failed for monorepo app %s: %s",
                                repo_id,
                                exc,
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
                                    "severity": "INFO",
                                    "finding": (
                                        f"{service_id} passed all deterministic checks for "
                                        f"standards v{standards_version}."
                                    ),
                                    "suggestion": "",
                                }
                            ]
                        post_findings(
                            findings=findings,
                            run_id=run_id,
                            repo=service_id,
                            flow_name="conformance-check",
                            source="conformance_deterministic",
                            standards_version=standards_version,
                        )

    prefect_log.info("%s: complete", flow_label)
