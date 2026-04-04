"""LLM client, prompt builders, and response parsing for evaluator-cog."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

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


def build_conformance_prompt(
    *,
    repo_id: str,
    service_type: str,
    dod_type: str | None = None,
    language: str,
    standards_version: str,
    deterministic_findings: list[dict],
    standards_rules: list[dict],
    check_exceptions: list[str] | None = None,
    exception_reasons: dict[str, str] | None = None,
) -> str:
    """Build the LLM prompt for soft-rule conformance assessment."""
    findings_summary = (
        "\n".join(
            f"- [{f.get('severity', 'INFO')}] {f.get('rule_id', '?')}: {f.get('finding', '')}"
            for f in deterministic_findings
        )
        or "(none)"
    )

    rules_text = (
        "\n".join(
            f"- {r['id']} [{r['severity']}]: {r['title']}\n  How to check: {r['check_notes']}"
            for r in standards_rules
            if r.get("check_notes")
        )
        or "(none)"
    )

    if check_exceptions:
        exc_lines = []
        for rule_id in check_exceptions:
            reason = (exception_reasons or {}).get(rule_id, "")
            exc_lines.append(f"  - {rule_id}: {reason}" if reason else f"  - {rule_id}")
        exc_block = "\n".join(exc_lines)
    else:
        exc_block = "  (none)"

    return f"""You are reviewing a MiniAppPolis ecosystem repo against engineering standards v{standards_version}.

Repo: {repo_id}
Service type: {service_type}
DoD type: {dod_type or "unknown"}
Language: {language}
Check exceptions (do not flag these rule IDs):
{exc_block}

STANDARDS RULES FOR THIS SERVICE TYPE:
The following are the checkable rules that apply to this repo type, with
instructions for how to evaluate them:

{rules_text}

DETERMINISTIC CHECK RESULTS:
These checks have already been run automatically:

{findings_summary}

YOUR TASK:
Evaluate ONLY the soft rules that deterministic checks cannot fully assess.
These are qualitative rules requiring interpretation:

DOC-006: missing docstrings on public functions/classes
DOC-008: dead or commented-out code blocks
PIPE-006: dual logger pattern in Prefect flows
PRIN-002: per-item error handling in pipeline loops
DOC-013: README 'Running locally' section completeness

STRICT CONSTRAINTS — failure to follow these will corrupt the report:

NEVER report a finding about something you cannot directly observe in
the deterministic results. If the deterministic checks did not flag
PY-006 (missing dependency), CD-002 (missing sentry-sdk), VER-003
(missing releaserc), or any other checkable rule, assume those things
ARE present and correct. Do not second-guess the deterministic checker.
NEVER report findings for rule IDs listed in check_exceptions.
NEVER report findings for rules already present in the deterministic
results above — they are already captured.
ONLY report findings for the five soft rules listed above, and only
when you have genuine signal from the deterministic findings or from
the service context provided.
RESPONSE COUNT RULE: Emit the MINIMUM number of findings needed.

If you have genuine signal for a soft rule: emit ONE finding for it.
If you have NO signal for a soft rule: do NOT emit anything for it.
Never emit an INFO finding just to note the absence of a problem.
When all soft rules are clean: emit exactly ONE INFO summarising
overall repo health. Do not emit one finding per rule.

Respond with ONLY valid JSON (no markdown) in this exact shape:
{{"findings":[{{"rule_id":"...","dimension":"structural_conformance","severity":"INFO|WARN|ERROR","finding":"...","suggestion":"..."}}]}}

Rules:
- severity must be INFO, WARN, or ERROR (uppercase).
- Reference the rule ID from the standards list above.
- Keep findings specific and actionable.
- If all soft rules appear clean, emit a single INFO finding summarising repo health.
"""
