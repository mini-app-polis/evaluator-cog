"""API client helpers for posting evaluation findings."""

from __future__ import annotations

import os
from typing import Any

from mini_app_polis import logger as logger_mod
from mini_app_polis.api import KaianoApiClient as CommonPythonApiClient

log = logger_mod.get_logger()


def _get_latest_stored_finding(
    *,
    api_client: Any,
    api_base_url: str,
    repo: str,
) -> dict[str, Any] | None:
    """
    Best-effort fetch of the most recent stored finding for this repo.
    Returns None on any failure.
    """
    try:
        if hasattr(api_client, "get"):
            response = api_client.get(f"/v1/evaluations?repo={repo}&limit=1")
        else:
            import httpx

            with httpx.Client(timeout=20.0) as client:
                r = client.get(
                    f"{api_base_url.rstrip('/')}/v1/evaluations",
                    params={"repo": repo, "limit": 1},
                )
                r.raise_for_status()
                response = r.json()

        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, list) and data:
                item = data[0]
                return item if isinstance(item, dict) else None
            if isinstance(response.get("items"), list) and response["items"]:
                item = response["items"][0]
                return item if isinstance(item, dict) else None
        if isinstance(response, list) and response:
            item = response[0]
            return item if isinstance(item, dict) else None
    except Exception:
        return None
    return None


def post_findings(
    *,
    findings: list[dict],
    run_id: str,
    repo: str,
    flow_name: str | None,
    source: str,
    standards_version: str,
    direct_finding_text: str | None = None,
) -> None:
    """Post a list of findings to api-kaianolevine-com. Never raises."""
    api_base_url = os.environ.get("KAIANO_API_BASE_URL", "")

    err_ct = warn_ct = info_ct = 0

    api_client = CommonPythonApiClient.from_env()
    findings_posted = 0
    evaluator_failed = False

    # Fetch once before the loop — avoids one GET per finding.
    # Note: this compares against the single most-recent stored finding.
    # Multi-finding batches may still accumulate duplicates if an earlier
    # finding in the batch is not the most recent record for the repo.
    # Known limitation — tracked for future improvement via composite key lookup.
    latest = _get_latest_stored_finding(
        api_client=api_client,
        api_base_url=api_base_url,
        repo=repo,
    )

    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "INFO").upper()
        if sev == "WARNING":
            sev = "WARN"
        if sev in {"CRITICAL", "ERROR"}:
            err_ct += 1
        elif sev == "WARN":
            warn_ct += 1
        elif sev == "SUCCESS":
            info_ct += 1
        else:
            sev = "INFO"
            info_ct += 1

        finding_text = (f.get("finding") or "").strip()
        if not finding_text:
            log.warning("Skipping finding with empty finding text")
            continue

        violation_id = f.get("violation_id") or None
        payload = {
            "run_id": run_id,
            "repo": repo,
            "flow_name": flow_name,
            "dimension": f.get("dimension") or "pipeline_consistency",
            "severity": sev,
            "finding": finding_text,
            "suggestion": f.get("suggestion") or None,
            "standards_version": standards_version,
            "source": "flow_hook" if direct_finding_text else source,
            "violation_id": violation_id,
        }
        if latest and (
            str(latest.get("finding") or "").strip() == finding_text
            and str(latest.get("severity") or "").upper() == sev
            and str(latest.get("dimension") or "").strip() == str(payload["dimension"])
        ):
            log.info("⏭️ Skipping duplicate finding: %s", finding_text[:60])
            continue
        try:
            api_client.post("/v1/evaluations", payload)
            findings_posted += 1
        except Exception as e:
            log.warning("pipeline evaluation: failed to POST finding: %s", e)
            evaluator_failed = True

    log.info(
        "🤖 Evaluation complete: %d errors, %d warnings, %d info findings "
        "(%d posted, evaluator_failed=%s)",
        err_ct,
        warn_ct,
        info_ct,
        findings_posted,
        evaluator_failed,
    )
