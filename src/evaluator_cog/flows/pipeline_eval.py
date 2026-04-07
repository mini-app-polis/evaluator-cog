"""Pipeline evaluation flow — post-run AI assessment and Prefect webhook handling."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from mini_app_polis import logger as logger_mod

from evaluator_cog.engine import llm as llm_engine
from evaluator_cog.engine.api_client import post_findings

log = logger_mod.get_logger()

_FLOW_REPO_MAP: dict[str, str] = {
    "conformance-check": "evaluator-cog",
    "pipeline-eval": "evaluator-cog",
    "process-transcript": "notes-ingest-cog",
    "update-dj-set-collection": "deejay-cog",
    "generate-summaries": "deejay-cog",
    "process-set": "deejay-cog",
}


def _flow_name_to_repo(flow_name: str) -> str:
    """Map a Prefect flow name to its ecosystem repo id.

    Falls back to 'deejay-cog' for unknown flows to preserve existing
    behaviour for legacy flows not yet in the map.
    """
    return _FLOW_REPO_MAP.get(flow_name, "deejay-cog")


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
    """Call Claude, then POST each finding to KAIANO_API_BASE_URL /v1/evaluations.

    Never raises — logs and returns on any failure.
    """
    if not os.environ.get("KAIANO_API_BASE_URL"):
        return

    standards_version = os.environ.get("STANDARDS_VERSION", "6.0")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    findings: list[dict[str, Any]] = []

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
                user_prompt = llm_engine._build_prompt_collection(
                    run_id=run_id,
                    standards_version=standards_version,
                    folders_processed=folders_processed,
                    tabs_written=tabs_written,
                    total_sets=total_sets,
                    json_snapshot_written=json_snapshot_written,
                    folder_names=folder_names or [],
                )
            else:
                user_prompt = llm_engine._build_prompt_csv(
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

            claude_text = llm_engine._anthropic_messages_create(
                api_key=os.environ["ANTHROPIC_API_KEY"],
                model=model,
                max_tokens=4096,
                user_prompt=user_prompt,
            )
            log.debug("Claude raw response: %s", claude_text[:500])
            findings, _ = llm_engine._parse_findings_from_claude(claude_text)
        except Exception:
            log.exception("pipeline evaluation: Claude request or parse failed")
            return

    post_findings(
        findings=findings,
        run_id=run_id,
        repo=repo,
        flow_name=flow_name,
        source=source,
        standards_version=standards_version,
        direct_finding_text=direct_finding_text,
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
    """Public wrapper around llm_engine._build_prompt_csv for external use."""
    return llm_engine._build_prompt_csv(
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
    """Public wrapper around llm_engine._build_prompt_collection for external use."""
    return llm_engine._build_prompt_collection(
        run_id=run_id,
        standards_version=standards_version,
        folders_processed=folders_processed,
        tabs_written=tabs_written,
        total_sets=total_sets,
        json_snapshot_written=json_snapshot_written,
        folder_names=folder_names,
    )


def _extract_flow_run_event_fields(payload: dict[str, Any]) -> dict[str, str]:
    """Handle flat webhook payloads and nested event payloads."""
    resource = payload.get("resource")
    if isinstance(resource, dict):
        payload = {**resource, **payload}

    return {
        "flow_run_id": str(
            payload.get("flow_run_id") or payload.get("id") or ""
        ).strip(),
        "flow_name": str(payload.get("flow_name") or "").strip(),
        "state_name": str(payload.get("state_name") or "").strip(),
        "state_type": str(payload.get("state_type") or "").strip().upper(),
        "start_time": str(payload.get("start_time") or "").strip(),
        "end_time": str(payload.get("end_time") or "").strip(),
    }


def _state_to_severity(state_type: str) -> str:
    normalized = (state_type or "").upper()
    if normalized == "CRASHED":
        return "ERROR"
    if normalized == "FAILED":
        return "WARN"
    return "INFO"


def _apply_prefect_flow_run_event(payload: dict[str, Any]) -> None:
    """Map a single webhook payload to evaluate_pipeline_run. May raise."""
    fields = _extract_flow_run_event_fields(payload)
    flow_run_id = fields["flow_run_id"] or "prefect-unknown-run"
    flow_name = fields["flow_name"] or "unknown-flow"
    state_name = fields["state_name"] or "UNKNOWN"
    state_type = fields["state_type"] or "UNKNOWN"
    collection_update = flow_name == "update-dj-set-collection"

    if state_type in {"FAILED", "CRASHED", "CANCELLED"}:
        evaluate_pipeline_run(
            run_id=flow_run_id,
            repo=_flow_name_to_repo(flow_name),
            flow_name=flow_name,
            sets_imported=0,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=0,
            failed_set_labels=[],
            api_ingest_success=True,
            sets_attempted=0,
            collection_update=collection_update,
            direct_finding_text=f"Flow {flow_name} entered {state_name} state",
            direct_severity=_state_to_severity(state_type),
            source="prefect_webhook",
        )
        return

    evaluate_pipeline_run(
        run_id=flow_run_id,
        repo=_flow_name_to_repo(flow_name),
        flow_name=flow_name,
        sets_imported=0,
        sets_failed=0,
        sets_skipped=0,
        total_tracks=0,
        failed_set_labels=[],
        api_ingest_success=True,
        sets_attempted=0,
        collection_update=collection_update,
        source="prefect_webhook",
    )


def handle_prefect_flow_run_event(payload: dict[str, Any]) -> None:
    """Best-effort handler for Prefect flow run state events.

    Never raises; logs and returns on failure.
    """
    try:
        _apply_prefect_flow_run_event(payload)
    except Exception:
        log.exception("evaluation_webhook: failed to handle flow run event")


def handle_prefect_flow_run_events(payloads: Iterable[dict[str, Any]]) -> None:
    """Handle multiple Prefect flow run state event payloads."""
    for payload in payloads:
        handle_prefect_flow_run_event(payload)
