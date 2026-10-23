"""LLM client, prompt builders, and response parsing for evaluator-cog."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Collection
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    import httpx


class _FileGroup(TypedDict):
    """One evidence group: which files, and how much of each to include."""

    patterns: list[str]
    per_file_cap: int
    test_group_cap: int | None


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _gather_evidence_files(repo_path: Path, *, total_budget_chars: int = 40000) -> str:
    """Collect curated repo file contents for LLM rule assessment evidence."""
    if repo_path is None or total_budget_chars <= 0:
        return ""

    def _is_hidden_or_ignored(path: Path) -> bool:
        parts = path.relative_to(repo_path).parts
        for part in parts:
            if part in {"node_modules", ".venv", "__pycache__"}:
                return True
            if part.startswith(".") and part != ".github":
                return True
        return False

    def _read_with_cap(path: Path, cap: int, *, uv_lock_limit: bool = False) -> str:
        try:
            if uv_lock_limit:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    raw = "".join(line for _, line in zip(range(200), fh, strict=False))
            else:
                raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

        if len(raw) <= cap:
            return raw
        remaining = len(raw) - cap
        return f"{raw[:cap]}\n...(truncated, {remaining} more chars)\n"

    def _eligible_files(patterns: list[str]) -> list[Path]:
        out: list[Path] = []
        for pattern in patterns:
            for p in repo_path.glob(pattern):
                if not p.is_file():
                    continue
                if p.name == "pnpm-lock.yaml":
                    continue
                if _is_hidden_or_ignored(p):
                    continue
                out.append(p)
        return sorted(set(out), key=lambda p: str(p.relative_to(repo_path)))

    groups: list[_FileGroup] = [
        {
            "patterns": [
                "pyproject.toml",
                "package.json",
                "requirements.txt",
                "uv.lock",
            ],
            "per_file_cap": 6000,
            "test_group_cap": None,
        },
        {
            "patterns": [
                "src/**/main.py",
                "src/**/app.py",
                "src/**/__main__.py",
                "src/index.ts",
                "src/index.tsx",
                "src/main.ts",
                "apps/*/src/main.py",
                "apps/*/src/index.ts",
            ],
            "per_file_cap": 5000,
            "test_group_cap": None,
        },
        {
            "patterns": [
                "src/**/flow.py",
                "src/**/flows/*.py",
                "src/**/flows.py",
                "src/**/deploy*.py",
                "**/*deployment*.py",
                "**/*schedule*.py",
            ],
            "per_file_cap": 4000,
            "test_group_cap": None,
        },
        {
            "patterns": [
                "**/auth.py",
                "**/logger.py",
                "**/observability.py",
                "**/sentry*.py",
            ],
            "per_file_cap": 4000,
            "test_group_cap": None,
        },
        {
            "patterns": ["tests/**/*.py", "tests/**/*.ts", "tests/**/*.test.ts"],
            "per_file_cap": 3000,
            "test_group_cap": 12000,
        },
        {
            "patterns": [
                ".github/workflows/*.yml",
                "railway.toml",
                "railway.json",
                "nixpacks.toml",
            ],
            "per_file_cap": 2000,
            "test_group_cap": None,
        },
    ]

    header = (
        "REPO FILE CONTENTS (curated evidence — read this for rule assessment):\n\n"
    )
    remaining = total_budget_chars
    if len(header) > remaining:
        return ""
    remaining -= len(header)
    sections: list[str] = [header]
    omitted_due_budget: list[str] = []

    for group in groups:
        test_group_remaining = group["test_group_cap"]
        for path in _eligible_files(group["patterns"]):
            rel_path = str(path.relative_to(repo_path))
            cap = group["per_file_cap"]
            if test_group_remaining is not None:
                if test_group_remaining <= 0:
                    omitted_due_budget.append(rel_path)
                    continue
                cap = min(cap, test_group_remaining)

            content = _read_with_cap(path, cap, uv_lock_limit=(path.name == "uv.lock"))
            if not content:
                continue

            chunk = f"=== {rel_path} ===\n{content}\n\n"
            if len(chunk) <= remaining:
                sections.append(chunk)
                remaining -= len(chunk)
                if test_group_remaining is not None:
                    test_group_remaining -= min(cap, len(content))
                continue

            if remaining >= 500:
                header_only = f"=== {rel_path} ===\n"
                trailer = "\n\n"
                if len(header_only) + len(trailer) >= remaining:
                    omitted_due_budget.append(rel_path)
                    continue
                max_content_len = remaining - len(header_only) - len(trailer)
                truncated = content[:max_content_len]
                chunk_partial = f"{header_only}{truncated}{trailer}"
                sections.append(chunk_partial)
                remaining -= len(chunk_partial)
                if test_group_remaining is not None:
                    test_group_remaining -= min(cap, len(truncated))
            else:
                omitted_due_budget.append(rel_path)

    included_file_count = sum(1 for s in sections if s.startswith("=== "))
    if included_file_count == 0 and not omitted_due_budget:
        return ""

    text = "".join(sections)
    if not omitted_due_budget:
        return text

    footer_intro = f"(Files matching evidence patterns but not included due to {total_budget_chars}-char budget:\n"
    footer_items = "".join(f"  {p}\n" for p in sorted(set(omitted_due_budget)))
    footer = f"{footer_intro}{footer_items})\n"
    if len(text) + len(footer) <= total_budget_chars:
        return text + footer

    body_budget = max(total_budget_chars - len(footer), 0)
    text = text[:body_budget]
    remaining_for_footer = total_budget_chars - len(text)
    return text + footer[:remaining_for_footer]


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
    if "violation_id" not in item:
        item["violation_id"] = rule_id or None
    return item


#: Attempts per Messages API call, including the first.
_ANTHROPIC_ATTEMPTS = 3
#: Throttled (429), overloaded (529) and server-side failures. A 4xx other
#: than 429 is a malformed request or a bad key, and is raised at once.
_ANTHROPIC_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})
_ANTHROPIC_BACKOFF_SECONDS = 2.0
_ANTHROPIC_BACKOFF_CAP_SECONDS = 30.0


def _anthropic_retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """The server's retry-after when it sent one, else doubling backoff; capped."""
    if response is not None:
        raw = response.headers.get("retry-after", "").strip()
        if raw:
            with suppress(ValueError):
                return min(float(raw), _ANTHROPIC_BACKOFF_CAP_SECONDS)
    return min(
        _ANTHROPIC_BACKOFF_SECONDS * (2**attempt), _ANTHROPIC_BACKOFF_CAP_SECONDS
    )


class LLMResponseError(RuntimeError):
    """The model answered, but not with something that can be graded.

    A truncated reply or one that is not a findings payload. Raised rather
    than read as "no findings", because an empty list is what a clean repo
    produces — treating an unreadable answer the same way reports a pass
    that no assessment actually made.
    """


class LLMTruncatedError(LLMResponseError):
    """The reply stopped at ``max_tokens``.

    Distinct from an unreadable reply because it has a remedy: ask about
    fewer rules at a time, so each answer is shorter. A larger ceiling is not
    one — the call is not streamed and has a fixed timeout, so a longer
    answer turns a truncation into a timeout.
    """


def _anthropic_messages_create(
    *,
    api_key: str,
    model: str,
    max_tokens: int,
    user_prompt: str,
) -> str:
    """Send a single-turn message to the Anthropic Messages API and return the text response.

    Makes a synchronous HTTP POST to /v1/messages with the given model and prompt.
    Raises httpx.HTTPStatusError on non-2xx responses, and LLMResponseError
    when the reply stopped at ``max_tokens`` — a cut-off JSON payload
    cannot be graded, however much of it arrived.
    """
    import os

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
    _llm_timeout = float(os.environ.get("EVALUATOR_LLM_TIMEOUT_SECONDS", "120"))
    # Throttling (429), overload (529) and 5xx are retried with backoff, as
    # are connection failures. Without this one overloaded response failed
    # the repository's whole evaluation, and the queue re-ran the job from
    # the top minutes later.
    data: dict[str, Any] = {}
    for attempt in range(_ANTHROPIC_ATTEMPTS):
        last = attempt == _ANTHROPIC_ATTEMPTS - 1
        response: httpx.Response | None = None
        try:
            with httpx.Client(timeout=_llm_timeout) as client:
                response = client.post(url, headers=headers, json=body)
        except httpx.ReadTimeout:
            # The model was slow, not unreachable. Another attempt costs a
            # full timeout again inside a job with a fixed deadline.
            raise
        except httpx.TransportError:
            if last:
                raise
        else:
            if last or response.status_code not in _ANTHROPIC_RETRYABLE_STATUS:
                response.raise_for_status()
                data = response.json()
                break
        time.sleep(_anthropic_retry_delay(response, attempt))
    if data.get("stop_reason") == "max_tokens":
        raise LLMTruncatedError(
            f"response truncated at max_tokens={max_tokens} "
            f"(model {data.get('model') or model})"
        )
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

    Raises LLMResponseError when the text is not a findings payload — not
    JSON, or JSON of the wrong shape. It used to return an empty list,
    which the flow posts as "passed all LLM checks": an unreadable answer
    reported as a clean one.
    """
    raw = text.strip()
    m = _JSON_FENCE.search(raw)
    if m:
        raw = m.group(1).strip()
    try:
        parsed_top = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMResponseError(
            f"response is not JSON ({exc.msg}); starts {raw[:120]!r}"
        ) from exc

    if isinstance(parsed_top, dict) and isinstance(parsed_top.get("findings"), list):
        parsed = parsed_top["findings"]
    elif isinstance(parsed_top, list):
        parsed = parsed_top
    else:
        raise LLMResponseError(
            "response is JSON but not a findings payload: expected "
            '{"findings": [...]} or a list, got '
            f"{type(parsed_top).__name__}"
        )

    validated: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            validated.append(item)
    return [_normalize_finding(item) for item in validated], False


def _resolved_rule_ids(
    checked_rule_ids: set[str] | None, deterministic_findings: list[dict]
) -> set[str]:
    """Rules the deterministic engine has already settled, one way or the other."""
    resolved = (checked_rule_ids or set()) | {
        str(f.get("rule_id") or "")
        for f in deterministic_findings
        if f.get("rule_id") != "CHECKER"
    }
    return resolved


def _select_soft_rules(
    standards_rules: list[dict],
    all_checked: set[str],
    check_exceptions: list[str] | None,
    all_skipped_ids: frozenset[str] | None,
) -> list[dict]:
    """The rules the LLM is asked to assess, in catalog order."""
    # Also exclude rules that are auto-excepted for this repo type/traits or
    # explicitly excepted via check_exceptions — the LLM should only see rules
    # that are genuinely in scope and not already resolved.
    all_excepted = (all_skipped_ids or frozenset()) | set(check_exceptions or [])
    # Exclude rules the catalog has routed to the deterministic engine. Those
    # belong to engine/deterministic.py regardless of whether the check
    # function has been implemented yet — forwarding them to the LLM would
    # invite inconsistent judgements on rules that have a single, canonical
    # deterministic interpretation. Rules without a routing marker (legacy
    # pre-audit rules) are classified as deterministic by default, which
    # preserves existing behaviour for the rules the engine already runs.
    return [
        r
        for r in standards_rules
        if r["id"] not in all_checked
        and r["id"] not in all_excepted
        and r.get("check_mode", "deterministic") == "llm"
    ]


def soft_rule_ids(
    *,
    standards_rules: list[dict],
    deterministic_findings: list[dict],
    checked_rule_ids: set[str] | None = None,
    check_exceptions: list[str] | None = None,
    all_skipped_ids: frozenset[str] | None = None,
) -> list[str]:
    """Ids of the rules :func:`build_conformance_prompt` asks the LLM about."""
    all_checked = _resolved_rule_ids(checked_rule_ids, deterministic_findings)
    return [
        r["id"]
        for r in _select_soft_rules(
            standards_rules, all_checked, check_exceptions, all_skipped_ids
        )
    ]


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
    all_skipped_ids: frozenset[str] | None = None,
    repo_path: Path | None = None,
    only_rule_ids: Collection[str] | None = None,
) -> str:
    """Build the LLM prompt for soft-rule conformance assessment.

    ``only_rule_ids`` narrows the rules to assess to those ids — one part of
    an assessment split because a single answer would not fit.
    """
    evaluator_yaml_content = ""
    if repo_path is not None:
        evaluator_yaml_path = repo_path / "evaluator.yaml"
        if evaluator_yaml_path.exists():
            with suppress(Exception):
                evaluator_yaml_content = evaluator_yaml_path.read_text().strip()

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
    all_checked = _resolved_rule_ids(checked_rule_ids, deterministic_findings)
    soft_rules = _select_soft_rules(
        standards_rules, all_checked, check_exceptions, all_skipped_ids
    )
    if only_rule_ids is not None:
        # One part of a split assessment: the rest are asked about in
        # another call (see flows/conformance.py).
        wanted = set(only_rule_ids)
        soft_rules = [r for r in soft_rules if r["id"] in wanted]
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

    evidence_block = ""
    if repo_path is not None:
        evidence_block = _gather_evidence_files(repo_path, total_budget_chars=40000)

    if evaluator_yaml_content:
        evaluator_yaml_block = f"""## Repo Evaluation Configuration (evaluator.yaml)

This repo has a formal evaluation configuration that has been reviewed and accepted:

{evaluator_yaml_content}

Rules listed under `exemptions` are formally excepted for this repo with documented reasons — do not raise findings for these rule IDs under any circumstances.

Rules listed under `deferrals` are known failures that are intentionally deprioritized — do not raise findings for these rule IDs. They are already tracked.

Rules that the repo's `traits` exempt, as the standards catalog defines them, are already left out of the rules to assess below — do not raise findings for them.
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
{inventory_block}
{evidence_block}
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
Evidence discipline:
- The REPO FILE CONTENTS block above contains the primary evidence for
  rule assessment. If a rule's check_notes reference a file or pattern,
  look for that evidence in the contents block before emitting a finding.
- NEVER claim a dependency, import, function call, configuration setting,
  or SDK initialization is "absent", "missing", or "not present" unless
  you have looked at the relevant file in the contents block and confirmed
  its absence from that file's content. Referring to the file inventory
  alone (filenames only) is NOT sufficient evidence of absence — you must
  see the file's contents and confirm the symbol is not there.
- If the relevant file is listed in the file inventory but its contents
  are NOT in the contents block (either not matched by the evidence
  patterns, or omitted due to the budget footer), treat the rule as
  UNASSESSABLE and emit no finding. Do not guess.
- "Unassessable" means: emit no finding. Do not emit a hedged finding, a
  "flagged for review" finding, or a finding phrased as "cannot confirm X
  from the inventory alone." The correct response to unassessable is
  silence.
Never flag something as missing just because it was not mentioned
in the deterministic findings — absence of a finding means passing
Only flag rules where you have genuine positive signal of a problem

WHAT TO EMIT:

Emit findings ONLY for rules where you have genuine positive
signal of a problem. Severity is WARN or ERROR for real problems.

DO NOT emit findings in any of these cases:
  - A soft rule appears clean or passing.
  - You cannot assess a rule from the provided file inventory,
    README, and deterministic findings (e.g. rules that require
    git commit history, rules that require test body contents you
    cannot see).
  - A rule does not apply to this repo's shape or dod_type.
  - You want to note that a rule was considered, or confirm
    a rule's exemption is documented, or summarise overall health.

The dashboard already surfaces a SUCCESS marker per repo when no
findings are emitted. Do not emit your own "all clean" summary —
that produces duplicate acknowledgements and dashboard noise.

If every soft rule is clean or unassessable, emit an empty
findings array: {{"findings":[]}}. That is the correct response.

Respond with ONLY valid JSON (no markdown) in this exact shape:
{{"findings":[{{"rule_id":"...","dimension":"structural_conformance","severity":"ERROR|WARN","finding":"...","suggestion":"..."}}]}}

Rules:
- severity must be WARN or ERROR (uppercase). INFO is not used for
  soft-rule findings — the emission conditions above forbid the
  cases where INFO would have applied. CRITICAL and SUCCESS are
  reserved for pipeline-run emission paths and must not be used
  for rule findings.
- Reference the rule ID from the standards list above.
- Keep findings specific and actionable.
- If every soft rule is clean or unassessable, return {{"findings":[]}}.
"""
