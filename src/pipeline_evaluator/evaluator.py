"""Best-effort Claude evaluation of pipeline runs; posts findings to deejay-marvel-api."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

from common_python_utils import logger as logger_mod

log = logger_mod.get_logger()

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _normalize_finding(item: dict) -> dict:
    """
    Normalize a finding dict from Claude.
    Claude sometimes uses alternative key names instead of
    "finding". Always ensure the "finding" key is present.
    """
    if not item.get("finding"):
        for alt in ("message", "description", "detail", "text"):
            if item.get(alt):
                item["finding"] = item[alt]
                break
    if not item.get("finding"):
        item["finding"] = "No finding text returned by evaluator."
    return item


def _anthropic_messages_create(
    *,
    api_key: str,
    model: str,
    max_tokens: int,
    user_prompt: str,
) -> str:
    import httpx

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    with httpx.Client(timeout=120.0) as client:
        r = client.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
    blocks = data.get("content") or []
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(str(b.get("text", "")))
    return "".join(parts).strip()


def _parse_findings_from_claude(text: str) -> tuple[list[dict[str, Any]], bool]:
    raw = text.strip()
    m = _JSON_FENCE.search(raw)
    if m:
        raw = m.group(1).strip()
    try:
        parsed_top = json.loads(raw)
    except json.JSONDecodeError:
        return [], False

    if isinstance(parsed_top, dict) and "findings" in parsed_top:
        inner = parsed_top["findings"]
        parsed = inner if isinstance(inner, list) else []
    elif isinstance(parsed_top, list):
        parsed = parsed_top
    else:
        return [], False

    validated: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            validated.append(item)
    return [_normalize_finding(item) for item in validated], False


def _parse_findings_json(text: str) -> list[dict[str, Any]]:
    """Backward-compatible alias; returns findings list only."""
    findings, _ = _parse_findings_from_claude(text)
    return findings


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


def _build_prompt_csv(
    *,
    run_id: str,
    standards_version: str,
    sets_imported: int,
    sets_failed: int,
    sets_skipped: int,
    total_tracks: int,
    failed_set_labels: list[str],
    api_ingest_success: bool,
    sets_attempted: int,
    unrecognized_filename_skips: int,
    duplicate_csv_count: int,
) -> str:
    failed_labels = ", ".join(failed_set_labels) if failed_set_labels else "(none)"
    return f"""You are evaluating a DJ set CSV processing pipeline run against engineering standards v{standards_version}.

CSV PROCESSING evaluation context:
- GitHub Actions run_id: {run_id}
- sets_attempted: CSV files encountered for processing ({sets_attempted})
- sets_imported: successfully processed CSVs (uploaded as Google Sheet, moved to archive) ({sets_imported})
- sets_failed: CSVs renamed with FAILED_ prefix ({sets_failed})
- sets_skipped: non-CSV files moved out of the source folder ({sets_skipped})
- unrecognized_filename_skips: files skipped due to filename format ({unrecognized_filename_skips})
- possible_duplicate_csv: CSVs renamed as possible_duplicate_ and not uploaded ({duplicate_csv_count})
- total_tracks: total track rows across successfully processed sets ({total_tracks})
- failed_set_labels: {failed_labels}
- api_ingest_success: all API ingest attempts succeeded, or none were required ({api_ingest_success})

Respond with ONLY valid JSON (no markdown) in this exact shape:
{{"findings":[{{"dimension":"pipeline_consistency","severity":"INFO|WARN|ERROR","finding":"...","suggestion":""}}]}}

Rules:
- severity must be INFO, WARN, or ERROR (uppercase).
- dimension should be pipeline_consistency unless a different dimension is clearly justified.
- Cover gaps between counts (e.g. attempted vs imported vs failed vs duplicates).
- If api_ingest_success is false, include at least one WARN or ERROR about API ingest.
"""


def _build_prompt_collection(
    *,
    run_id: str,
    standards_version: str,
    folders_processed: int,
    tabs_written: int,
    total_sets: int,
    json_snapshot_written: bool,
    folder_names: list[str],
) -> str:
    current_year = datetime.now().year
    formatted_folder_names = ", ".join(folder_names) if folder_names else "(none)"
    return f"""You are evaluating a DJ set COLLECTION UPDATE pipeline run against engineering standards v{standards_version}.

COLLECTION_UPDATE evaluation context:
- This run rebuilt the master DJ set collection spreadsheet and JSON snapshot.
- No CSV processing happened in this run.
- GitHub Actions run_id: {run_id}
- folders_processed: {folders_processed}
- tabs_written: {tabs_written}
- total_sets: {total_sets}
- json_snapshot_written: {json_snapshot_written}
- folder_names: {formatted_folder_names}
- current_year: {current_year}

Evaluate collection update conformance using these rules:
- If tabs_written == 0 and folders_processed > 0: emit WARN "No tabs written despite N folders processed"
- If json_snapshot_written is False: emit ERROR "JSON snapshot write failed"
- If folder_names does not include current_year: emit WARN "Current year folder missing"
- If total_sets == 0 and folders_processed > 0: emit WARN "No sets found across any folder"
- Otherwise: emit INFO confirming counts

Respond with ONLY valid JSON (no markdown) in this exact shape:
{{"findings":[{{"dimension":"pipeline_consistency","severity":"INFO|WARN|ERROR","finding":"...","suggestion":""}}]}}

Rules:
- severity must be INFO, WARN, or ERROR (uppercase).
"""


def evaluate_pipeline_run(
    *,
    run_id: str,
    repo: str,
    sets_imported: int,
    sets_failed: int,
    sets_skipped: int,
    total_tracks: int,
    failed_set_labels: list[str],
    api_ingest_success: bool,
    sets_attempted: int = 0,
    collection_update: bool = False,
    unrecognized_filename_skips: int = 0,
    duplicate_csv_count: int = 0,
    direct_finding_text: str | None = None,
    direct_severity: str | None = None,
    folders_processed: int = 0,
    tabs_written: int = 0,
    total_sets: int = 0,
    json_snapshot_written: bool = False,
    folder_names: list[str] | None = None,
    flow_name: str | None = None,
    source: str = "flow_inline",
) -> None:
    """
    Call Claude, then POST each finding to KAIANO_API_BASE_URL /v1/evaluations.
    Never raises — logs and returns on any failure.
    """
    if not os.environ.get("KAIANO_API_BASE_URL"):
        return

    api_base_url = os.environ.get("KAIANO_API_BASE_URL", "")
    standards_version = os.environ.get("STANDARDS_VERSION", "6.0")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    findings: list[dict[str, Any]] = []

    # Direct findings are used for failure/crash paths where a deterministic
    # signal is better than trying to infer context with Claude.
    if direct_finding_text:
        sev = str(direct_severity or "WARN").upper()
        if sev == "WARNING":
            sev = "WARN"
        if sev not in {"INFO", "WARN", "ERROR"}:
            sev = "WARN"
        findings = [
            {
                "dimension": "pipeline_consistency",
                "severity": sev,
                "finding": direct_finding_text.strip(),
                "suggestion": None,
            }
        ]
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return
        try:
            if collection_update:
                user_prompt = _build_prompt_collection(
                    run_id=run_id,
                    standards_version=standards_version,
                    folders_processed=folders_processed,
                    tabs_written=tabs_written,
                    total_sets=total_sets,
                    json_snapshot_written=json_snapshot_written,
                    folder_names=folder_names or [],
                )
            else:
                user_prompt = _build_prompt_csv(
                    run_id=run_id,
                    standards_version=standards_version,
                    sets_imported=sets_imported,
                    sets_failed=sets_failed,
                    sets_skipped=sets_skipped,
                    total_tracks=total_tracks,
                    failed_set_labels=failed_set_labels,
                    api_ingest_success=api_ingest_success,
                    sets_attempted=sets_attempted,
                    unrecognized_filename_skips=unrecognized_filename_skips,
                    duplicate_csv_count=duplicate_csv_count,
                )

            claude_text = _anthropic_messages_create(
                api_key=os.environ["ANTHROPIC_API_KEY"],
                model=model,
                max_tokens=4096,
                user_prompt=user_prompt,
            )
            log.debug("Claude raw response: %s", claude_text[:500])
            findings, _ = _parse_findings_from_claude(claude_text)
        except Exception:
            log.exception("pipeline evaluation: Claude request or parse failed")
            return

    err_ct = warn_ct = info_ct = 0

    try:
        from common_python_utils.api import (
            CommonPythonApiClient,  # type: ignore[attr-defined]
        )
    except Exception:
        log.exception("pipeline evaluation: Kaiano API client not available")
        return

    api_client = CommonPythonApiClient.from_env()
    findings_posted = 0
    evaluator_failed = False

    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "INFO").upper()
        if sev == "WARNING":
            sev = "WARN"
        if sev == "ERROR":
            err_ct += 1
        elif sev == "WARN":
            warn_ct += 1
        else:
            sev = "INFO"
            info_ct += 1

        finding_text = (f.get("finding") or "").strip()
        if not finding_text:
            log.warning("Skipping finding with empty finding text")
            continue

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
        }
        latest = _get_latest_stored_finding(
            api_client=api_client,
            api_base_url=api_base_url,
            repo=repo,
        )
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


def build_csv_evaluation_prompt(
    *,
    run_id: str,
    standards_version: str,
    sets_imported: int,
    sets_failed: int,
    sets_skipped: int,
    total_tracks: int,
    failed_set_labels: list[str],
    api_ingest_success: bool,
    sets_attempted: int,
    unrecognized_filename_skips: int = 0,
    duplicate_csv_count: int = 0,
) -> str:
    """Exposed for tests (same body as internal CSV prompt)."""
    return _build_prompt_csv(
        run_id=run_id,
        standards_version=standards_version,
        sets_imported=sets_imported,
        sets_failed=sets_failed,
        sets_skipped=sets_skipped,
        total_tracks=total_tracks,
        failed_set_labels=failed_set_labels,
        api_ingest_success=api_ingest_success,
        sets_attempted=sets_attempted,
        unrecognized_filename_skips=unrecognized_filename_skips,
        duplicate_csv_count=duplicate_csv_count,
    )


def build_collection_evaluation_prompt(
    *,
    run_id: str,
    standards_version: str,
    folders_processed: int,
    tabs_written: int,
    total_sets: int,
    json_snapshot_written: bool,
    folder_names: list[str],
) -> str:
    """Exposed for tests (same body as internal collection prompt)."""
    return _build_prompt_collection(
        run_id=run_id,
        standards_version=standards_version,
        folders_processed=folders_processed,
        tabs_written=tabs_written,
        total_sets=total_sets,
        json_snapshot_written=json_snapshot_written,
        folder_names=folder_names,
    )
