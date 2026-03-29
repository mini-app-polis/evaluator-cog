"""Structural conformance checking flow.

Fetches the live ecosystem inventory from ecosystem-standards, clones each
active repo, runs deterministic rule checks, calls LLM for soft-rule
assessment, and posts all findings to api-kaianolevine-com with
source='conformance_check'.

Triggered by Prefect schedule (daily) or manually via Prefect Cloud.
"""

from __future__ import annotations

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
    """Fetch current standards version from live index.yaml."""
    data = _fetch_yaml(_INDEX_YAML_URL)
    return str(data.get("version") or os.environ.get("STANDARDS_VERSION", "1.2.5"))


def _get_active_repos(ecosystem: dict) -> list[dict]:
    """Return all active services from ecosystem.yaml."""
    services = ecosystem.get("services", [])
    return [s for s in services if s.get("status") == "active"]


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
    run_id: str = "conformance",
) -> list[dict[str, Any]]:
    """
    Run deterministic + LLM conformance checks against a cloned repo.
    Posts findings to api-kaianolevine-com. Never raises.
    """
    prefect_log = get_run_logger()

    # Deterministic checks
    try:
        deterministic_findings = run_all_checks(repo_path)
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
                standards_version=standards_version,
                deterministic_findings=deterministic_findings,
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
        prefect_log.info("conformance: no findings for %s", repo_id)
        return []

    post_findings(
        findings=all_findings,
        run_id=run_id,
        repo=repo_id,
        flow_name="conformance",
        source="conformance_check",
        standards_version=standards_version,
    )

    return all_findings


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

    run_id = f"conformance-{standards_version}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        for service in active_repos:
            repo_id = service.get("id", "")
            if not repo_id:
                continue

            # Libraries without a host are not deployed services — skip cloning
            # but still check if they have a GitHub repo
            prefect_log.info("conformance: processing %s", repo_id)

            repo_path = _download_repo(repo_id, tmp_dir)
            if repo_path is None:
                prefect_log.warning(
                    "conformance: skipping %s — could not clone", repo_id
                )
                continue

            try:
                findings = run_conformance_check(
                    repo_id=repo_id,
                    repo_path=repo_path,
                    standards_version=standards_version,
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
