"""Structural conformance checking flow.

Fetches the live ecosystem inventory from ecosystem-standards, clones each
active repo, runs deterministic rule checks, calls LLM for soft-rule
assessment, and posts all findings to api-kaianolevine-com with
source='conformance_check'.

Triggered by Prefect schedule (daily) or manually via Prefect Cloud.
"""

from __future__ import annotations

import datetime
import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import httpx
import yaml
from mini_app_polis import logger as logger_mod
from prefect import flow, get_run_logger

from evaluator_cog.engine.api_client import post_findings
from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.llm import (
    _anthropic_messages_create,
    _parse_findings_from_claude,
    build_conformance_prompt,
)

log = logger_mod.get_logger()

_ECOSYSTEM_YAML_URL = "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/ecosystem.yaml"
_INDEX_YAML_URL = "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/index.yaml"
_STANDARDS_BASE_URL = "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/standards"

_DOMAINS_BY_TYPE = {
    "worker": [
        "python",
        "testing",
        "pipeline",
        "delivery",
        "documentation",
        "principles",
    ],
    "api": ["python", "testing", "api", "delivery", "documentation", "principles"],
    "library": ["python", "testing", "delivery", "documentation"],
    "site": ["frontend", "delivery", "documentation"],
}


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


def _fetch_standards_for_type(service_type: str) -> list[dict]:
    """
    Fetch checkable rules from relevant standards domains for this service type.
    Returns a list of rule dicts with id, title, severity, check_notes.
    Never raises — returns [] on failure.
    """
    domains = _DOMAINS_BY_TYPE.get(service_type, _DOMAINS_BY_TYPE["worker"])
    rules = []
    for domain in domains:
        url = f"{_STANDARDS_BASE_URL}/{domain}.yaml"
        data = _fetch_yaml(url)
        for rule in data.get("standards", []):
            if rule.get("checkable"):
                rules.append(
                    {
                        "id": rule.get("id", ""),
                        "title": rule.get("title", ""),
                        "severity": rule.get("severity", "INFO"),
                        "check_notes": rule.get("check_notes", "").strip(),
                    }
                )
    return rules


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
    language: str = "python",
    cog_subtype: str | None = None,
    check_exceptions: list[str] | None = None,
    standards_rules: list[dict] | None = None,
    run_id: str = "conformance",
) -> list[dict[str, Any]]:
    """
    Run deterministic + LLM conformance checks against a cloned repo.
    Posts findings to api-kaianolevine-com. Never raises.
    """
    prefect_log = get_run_logger()

    # Deterministic checks
    try:
        deterministic_findings = run_all_checks(
            repo_path,
            language=language,
            service_type=service_type,
            cog_subtype=cog_subtype,
            check_exceptions=check_exceptions,
        )
    except Exception as exc:
        log.exception("conformance: run_all_checks failed for %s: %s", repo_id, exc)
        deterministic_findings = []

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
                language=language,
                standards_version=standards_version,
                deterministic_findings=deterministic_findings,
                standards_rules=standards_rules or [],
                check_exceptions=check_exceptions,
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

    if not all_findings:
        all_findings = [
            {
                "rule_id": "STATUS",
                "dimension": "structural_conformance",
                "severity": "INFO",
                "finding": f"{repo_id} passed all conformance checks for standards v{standards_version}.",
                "suggestion": "",
            }
        ]

    post_findings(
        findings=all_findings,
        run_id=run_id,
        repo=repo_id,
        flow_name="conformance",
        source="conformance_check",
        standards_version=standards_version,
    )

    return all_findings


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


@flow(name="conformance-check", log_prints=True)
def conformance_check_flow() -> None:
    """
    Fetch ecosystem inventory, clone each active repo, run conformance checks.
    Runs daily via Prefect schedule. Advisory only — no automated remediation.
    """
    prefect_log = get_run_logger()

    standards_version = _get_standards_version()
    prefect_log.info("conformance: standards version %s", standards_version)

    ecosystem = _fetch_yaml(_ECOSYSTEM_YAML_URL)
    active_repos = _get_active_repos(ecosystem)

    if not active_repos:
        prefect_log.warning("conformance: no active repos found in ecosystem.yaml")
        return

    prefect_log.info("conformance: checking %d active repos", len(active_repos))

    run_id = _build_conformance_run_id(standards_version)

    with tempfile.TemporaryDirectory() as tmp_dir:
        for service in active_repos:
            repo_id = service.get("id", "")
            repo_name = service.get("repo") or repo_id
            if not repo_id:
                continue

            # Libraries without a host are not deployed services — skip cloning
            # but still check if they have a GitHub repo
            prefect_log.info("conformance: processing %s", repo_id)

            repo_path = _download_repo(repo_name, tmp_dir)
            if repo_path is None:
                prefect_log.warning(
                    "conformance: skipping %s — could not clone", repo_id
                )
                continue

            service_type = service.get("type", "worker")
            language = str(service.get("language") or "python")
            cog_subtype = str(service.get("cog_subtype") or "").strip() or None
            raw_exc = service.get("check_exceptions") or []
            check_exceptions = (
                [str(x) for x in raw_exc] if isinstance(raw_exc, list) else []
            )
            standards_rules = _fetch_standards_for_type(service_type)

            try:
                findings = run_conformance_check(
                    repo_id=repo_id,
                    repo_path=repo_path,
                    standards_version=standards_version,
                    service_type=service_type,
                    language=language,
                    cog_subtype=cog_subtype,
                    check_exceptions=check_exceptions,
                    standards_rules=standards_rules,
                    run_id=run_id,
                )
                prefect_log.info(
                    "conformance: %d total findings for %s", len(findings), repo_id
                )
            except Exception as exc:
                prefect_log.warning(
                    "conformance: check failed for %s: %s", repo_id, exc
                )

    prefect_log.info("conformance: complete")
