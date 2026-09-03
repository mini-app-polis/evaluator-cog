"""API client helpers for posting evaluation findings."""

from __future__ import annotations

from typing import Any

from mini_app_polis import logger as logger_mod
from mini_app_polis.api import KaianoApiClient as CommonPythonApiClient

log = logger_mod.get_logger()


def _get_latest_stored_finding(
    *,
    api_client: Any,
    repo: str,
) -> dict[str, Any] | None:
    """
    Best-effort fetch of the most recent stored finding for this repo.
    Returns None on any failure.

    Reads go through the machine-named client and nothing else. This
    function used to carry a fallback that built a bare ``httpx.Client``
    against ``KAIANO_API_BASE_URL`` whenever ``api_client`` had no
    ``.get`` attribute. That branch presented no credential at all, so
    the read was unattributable — the exact failure CD-019 exists to
    catch, sitting beside the correct call. Post-Keystone it could not
    have succeeded either: the API rejects an unauthenticated read. It
    is removed rather than repaired; there is only one way for this cog
    to reach the API, and a second one that silently drops the caller's
    identity is worse than an exception.
    """
    try:
        response = api_client.get(f"/v1/evaluations?repo={repo}&limit=1")

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
    err_ct = warn_ct = info_ct = 0

    api_client = CommonPythonApiClient.from_env("evaluator-cog")
    findings_posted = 0
    evaluator_failed = False

    # Fetch once before the loop — avoids one GET per finding.
    # Dedup key: (run_id, finding_text, severity, dimension). Collisions on all
    # four fields are treated as duplicate posts (e.g. a retry); different
    # run_id means a new run regardless of identical text. This matters
    # because client helpers may emit identical default text (e.g. "Run
    # completed successfully.") across many runs.
    latest = _get_latest_stored_finding(api_client=api_client, repo=repo)

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
        # Note: findings may carry `status`, `deferred`, and `downgraded`
        # keys set by the v4.0.0 dispatch machinery in run_all_checks. The
        # api-kaianolevine-com /v1/evaluations endpoint does not accept
        # these fields (extra=forbid), so we do not forward them. The
        # dispatch metadata is preserved on the in-process finding dicts
        # for logging and for the LLM prompt, but not persisted server-side.
        payload = {
            "run_id": run_id,
            "repo": repo,
            "flow_name": flow_name,
            "dimension": f.get("dimension") or "pipeline_consistency",
            "severity": sev,
            "finding": finding_text,
            "suggestion": f.get("suggestion") or None,
            "standards_version": standards_version,
            "source": source,
            "violation_id": violation_id,
        }
        if latest and (
            str(latest.get("run_id") or "").strip() == str(run_id).strip()
            and str(latest.get("finding") or "").strip() == finding_text
            and str(latest.get("severity") or "").upper() == sev
            and str(latest.get("dimension") or "").strip() == str(payload["dimension"])
        ):
            log.info(
                "⏭️ Skipping duplicate finding for run_id=%s: %s",
                run_id,
                finding_text[:60],
            )
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
