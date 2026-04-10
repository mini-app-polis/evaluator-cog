"""LLM client, prompt builders, and response parsing for evaluator-cog."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from datetime import datetime
from pathlib import Path
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
    rule_id = item.get("rule_id", "") or ""
    if rule_id and not item["finding"].startswith(f"{rule_id}:"):
        item["finding"] = f"{rule_id}: {item['finding']}"
    return item


def _anthropic_messages_create(
    *,
    api_key: str,
    model: str,
    max_tokens: int,
    user_prompt: str,
) -> str:
    """Send a single-turn message to the Anthropic Messages API and return the text response.

    Makes a synchronous HTTP POST to /v1/messages with the given model and prompt.
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
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
    """Parse a JSON findings payload returned by Claude.

    Accepts raw text that may contain a ```json``` fence.
    Returns a tuple of (findings_list, bool) where bool is always False
    (reserved for a future partial-parse flag).
    Returns ([], False) on any parse error.
    """
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
    """Build the LLM evaluation prompt for a CSV processing pipeline run.

    Returns a prompt string instructing Claude to emit a JSON findings payload
    assessing the run against pipeline_consistency standards.
    """
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
    """Build the LLM evaluation prompt for a DJ set collection update pipeline run.

    Returns a prompt string instructing Claude to emit a JSON findings payload
    assessing the collection rebuild run against pipeline_consistency standards.
    """
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
    checked_rule_ids: set[str] | None = None,
    check_exceptions: list[str] | None = None,
    exception_reasons: dict[str, str] | None = None,
    monorepo_context: dict | None = None,
    repo_path: Path | None = None,
) -> str:
    """Build the LLM prompt for soft-rule conformance assessment."""
    import yaml as _yaml

    evaluator_yaml_content = ""
    repo_type = "pipeline-cog"
    if repo_path is not None:
        evaluator_yaml_path = repo_path / "evaluator.yaml"
        if evaluator_yaml_path.exists():
            with suppress(Exception):
                evaluator_yaml_content = evaluator_yaml_path.read_text().strip()
        if evaluator_yaml_content:
            with suppress(Exception):
                ev = _yaml.safe_load(evaluator_yaml_content) or {}
                repo_type = str(ev.get("type", "pipeline-cog") or "pipeline-cog")

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
    all_checked = (checked_rule_ids or set()) | {
        str(f.get("rule_id") or "")
        for f in deterministic_findings
        if f.get("rule_id") != "CHECKER"
    }
    # EVAL-002 is assessed deterministically via the standards_version field check.
    # Always mark it as checked so the LLM does not re-assess it.
    all_checked.add("EVAL-002")
    soft_rules = [r for r in standards_rules if r["id"] not in all_checked]
    soft_rules_text = (
        "\n".join(
            f"- {r['id']} [{r['severity']}]: {r['title']}\n"
            f"  How to check: {r['check_notes']}"
            for r in soft_rules
        )
        or "(none — all checkable rules covered by deterministic checks)"
    )

    if check_exceptions:
        exc_lines = []
        for rule_id in check_exceptions:
            reason = (exception_reasons or {}).get(rule_id, "")
            exc_lines.append(f"  - {rule_id}: {reason}" if reason else f"  - {rule_id}")
        exc_block = "\n".join(exc_lines)
    else:
        exc_block = "  (none)"

    if monorepo_context:
        workspace_deps = ", ".join(monorepo_context.get("workspace_deps", [])) or "none"
        sibling_ids = (
            ", ".join(
                str(a.get("service_id") or a.get("id") or "")
                for a in monorepo_context.get("sibling_apps", [])
                if (a.get("service_id") or a.get("id")) != repo_id
            )
            or "none"
        )
        monorepo_block = f"""
Monorepo context:
  This service is an app within the '{monorepo_context.get("monorepo_id")}' monorepo.
  Package manager: {monorepo_context.get("package_manager", "pnpm")}
  Workspace-level deps (satisfy XSTACK-001 per MONO-001): {workspace_deps}
  Sibling apps: {sibling_ids}

  IMPORTANT: Do not flag XSTACK-001 for absence of shared library in this app's package.json
  if it is present in the workspace_deps list above — workspace root deps satisfy the
  requirement per MONO-001. Only flag XSTACK-001 if the dep is absent from BOTH the workspace
  root AND this app's own package.json.

  Do not flag CI rules (VER-003, VER-005, VER-006) for absence in this app subdirectory if
  the CI config exists at the monorepo root — root CI satisfies these rules per MONO-002.
"""
    else:
        monorepo_block = ""

    if repo_path is not None:
        try:
            src_files = sorted(
                str(p.relative_to(repo_path))
                for p in repo_path.rglob("*")
                if p.is_file()
                and not any(
                    part.startswith(".")
                    or part == "__pycache__"
                    or part == "node_modules"
                    for part in p.parts
                )
            )
            if len(src_files) > 60:
                src_files = src_files[:60] + [f"... ({len(src_files) - 60} more files)"]
            inventory_block = (
                "REPO FILE INVENTORY (actual files present — do not reference files not listed here):\n"
                + "\n".join(f"  {f}" for f in src_files)
                + "\n"
            )
        except Exception:
            inventory_block = ""
    else:
        inventory_block = ""

    # Inject README content so the LLM can assess documentation rules directly
    # rather than inferring from file inventory alone.
    readme_block = ""
    if repo_path is not None:
        readme_path = repo_path / "README.md"
        if readme_path.exists():
            try:
                readme_text = readme_path.read_text()
                if len(readme_text) <= 4000:
                    readme_block = f"README.md CONTENT:\n{readme_text}\n"
                else:
                    readme_block = (
                        f"README.md CONTENT (first 4000 chars — truncated):\n"
                        f"{readme_text[:4000]}\n...(truncated)\n"
                    )
            except Exception:
                readme_block = ""

    if evaluator_yaml_content:
        evaluator_yaml_block = f"""## Repo Evaluation Configuration (evaluator.yaml)

This repo has a formal evaluation configuration that has been reviewed and accepted:

{evaluator_yaml_content}

Rules listed under `exemptions` are formally excepted for this repo with documented reasons — do not raise findings for these rule IDs under any circumstances.

Rules listed under `deferrals` are known failures that are intentionally deprioritized — do not raise findings for these rule IDs. They are already tracked.

Traits listed modify which rules apply:
- `multi-flow`: CD-015 (prefect.serve() pattern) does not apply — the multi-flow structure makes source scanning unreliable for this check.
- `pipeline-cog-evaluator`: PIPE-011 does not apply — this repo IS the evaluator.
- `logger-primitive`: CD-009 and XSTACK-001 do not apply — this repo defines the shared logger primitive.
- `cloudflare-pages`: VER-003, VER-005, VER-006 do not apply — Cloudflare Pages Git integration handles deployment.

Additionally, rules that are auto-excepted by repo type (`type: {repo_type}`) should not be flagged:
- `shared-library`: TEST-001, TEST-002, TEST-003, TEST-004, TEST-007, CD-002, CD-009, CD-010, PY-006, XSTACK-001, all PIPE rules, CD-007, CD-015
- `static-site`: same as shared-library plus XSTACK-003
- `api-service`: all PIPE rules, TEST-001–004, CD-007, CD-015
- `trigger-cog`: all PIPE rules except CD-007, TEST-001–004, CD-015
- `standards-repo`: all PIPE rules, TEST-001–004, CD-002, CD-009, CD-010, XSTACK-001, PY-006, CD-007, CD-015

"""
    else:
        evaluator_yaml_block = ""

    return f"""You are reviewing a MiniAppPolis ecosystem repo against engineering standards v{standards_version}.

Repo: {repo_id}
Service type: {service_type}
DoD type: {dod_type or "unknown"}
Language: {language}
Check exceptions (do not flag these rule IDs):
{exc_block}
{monorepo_block}
{inventory_block}
{readme_block}
{evaluator_yaml_block}STANDARDS RULES FOR THIS SERVICE TYPE:
The following are the checkable rules that apply to this repo type, with
instructions for how to evaluate them:

{rules_text}

DETERMINISTIC CHECK RESULTS:
These checks have already been run automatically:

{findings_summary}

YOUR TASK:
RULES TO ASSESS:

{soft_rules_text}

WHAT YOU ARE AND ARE NOT RESPONSIBLE FOR:

The following rules were checked deterministically and either passed
or produced findings already listed above. DO NOT assess these rules.
DO NOT produce findings for them. Treat them as resolved:

{chr(10).join(f"  - {rid}" for rid in sorted(all_checked)) or "  (none)"}

You are ONLY responsible for assessing the soft rules listed in
RULES TO ASSESS above — rules the deterministic checker cannot
evaluate. These require qualitative judgment from you.

ABSOLUTE CONSTRAINTS:

Never produce a finding for a rule in the resolved list above,
regardless of how the violation is framed or what rule ID you
assign it. Resolved means resolved.
The check_exceptions list is ABSOLUTE. Never produce a finding
for any excepted rule under any framing — not as a different
rule ID, not as a general observation, not as a suggestion.
Exceptions are deliberate architectural decisions, not oversights.
Never apply general ecosystem knowledge to infer violations.
Only flag things observable directly from the file inventory,
README content provided above, or deterministic findings.
If you cannot observe a violation from the provided context,
do not flag it.
Never produce a finding phrased as "no deterministic result
confirms X" — if you cannot observe something directly, do not
flag it
Never flag something as missing just because it was not mentioned
in the deterministic findings — absence of a finding means passing
Only flag rules where you have genuine positive signal of a problem
- If you are raising a finding for CD-010 (three-layer observability stack
  absent or incomplete), do NOT also raise separate findings for CD-002
  (Sentry absent) or CD-009 (structured logging absent) for the same service.
  CD-010 is the composite rule — its sub-components are implicit. Raising all
  three produces duplicate findings for one root cause.
- If soft_rules is empty, emit exactly one INFO finding confirming
  the repo passed all assessed rules
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
