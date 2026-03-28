"""Structural conformance checking flow.

Clones each repo in the ecosystem inventory, runs deterministic rule checks,
optionally calls LLM for soft rules, and posts findings to
api-kaianolevine-com with source='conformance_check'.

Triggered by Prefect schedule (centralized, not per-repo).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from common_python_utils import logger as logger_mod

from evaluator_cog.engine.api_client import post_findings
from evaluator_cog.engine.deterministic import run_all_checks

log = logger_mod.get_logger()


def run_conformance_check(
    *,
    repo_id: str,
    repo_path: Path,
    standards_version: str | None = None,
    run_id: str = "conformance",
) -> list[dict[str, Any]]:
    """
    Run deterministic conformance checks against a cloned repo.
    Posts findings to api-kaianolevine-com. Never raises.
    """
    _standards_version = standards_version or os.environ.get(
        "STANDARDS_VERSION", "1.2.5"
    )

    try:
        findings = run_all_checks(repo_path)
    except Exception as exc:
        log.exception("conformance: run_all_checks failed for %s: %s", repo_id, exc)
        return []

    if not findings:
        log.info("conformance: no findings for %s", repo_id)
        return []

    log.info("conformance: %d findings for %s", len(findings), repo_id)

    post_findings(
        findings=findings,
        run_id=run_id,
        repo=repo_id,
        flow_name="conformance",
        source="conformance_check",
        standards_version=_standards_version,
    )

    return findings
