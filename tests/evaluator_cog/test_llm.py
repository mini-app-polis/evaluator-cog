"""Tests for engine/llm.py — normalize, parse, and HTTP client."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from evaluator_cog.engine.llm import (
    _anthropic_messages_create,
    _normalize_finding,
    _parse_findings_from_claude,
    build_conformance_prompt,
)

# ---------------------------------------------------------------------------
# _normalize_finding
# ---------------------------------------------------------------------------


def test_normalize_finding_passthrough_when_finding_present() -> None:
    """Finding key already present — no mutation."""
    item = {"finding": "All good.", "severity": "INFO"}
    result = _normalize_finding(item)
    assert result["finding"] == "All good."


def test_normalize_finding_uses_message_fallback() -> None:
    """'message' key is promoted to 'finding' when 'finding' is absent."""
    item = {"message": "Something went wrong.", "severity": "WARN"}
    result = _normalize_finding(item)
    assert result["finding"] == "Something went wrong."


def test_normalize_finding_uses_description_fallback() -> None:
    """'description' key is promoted to 'finding' when 'finding' and 'message' are absent."""
    item = {"description": "Missing docstrings.", "severity": "WARN"}
    result = _normalize_finding(item)
    assert result["finding"] == "Missing docstrings."


def test_normalize_finding_uses_detail_fallback() -> None:
    """'detail' key is promoted to 'finding'."""
    item = {"detail": "README is missing.", "severity": "ERROR"}
    result = _normalize_finding(item)
    assert result["finding"] == "README is missing."


def test_normalize_finding_uses_text_fallback() -> None:
    """'text' key is promoted to 'finding'."""
    item = {"text": "Sentry not initialised.", "severity": "ERROR"}
    result = _normalize_finding(item)
    assert result["finding"] == "Sentry not initialised."


def test_normalize_finding_default_when_no_key_matches() -> None:
    """Returns sentinel text when no recognisable key is present."""
    item = {"severity": "INFO", "unknown_key": "whatever"}
    result = _normalize_finding(item)
    assert result["finding"] == "No finding text returned by evaluator."


def test_normalize_finding_prefers_finding_over_message() -> None:
    """'finding' takes priority over 'message' when both present."""
    item = {"finding": "Real finding.", "message": "Should be ignored."}
    result = _normalize_finding(item)
    assert result["finding"] == "Real finding."


# ---------------------------------------------------------------------------
# _parse_findings_from_claude
# ---------------------------------------------------------------------------


def test_parse_findings_plain_json() -> None:
    raw = json.dumps(
        {"findings": [{"severity": "INFO", "finding": "ok", "dimension": "x"}]}
    )
    findings, _ = _parse_findings_from_claude(raw)
    assert len(findings) == 1
    assert findings[0]["finding"] == "ok"


def test_parse_findings_strips_json_fence() -> None:
    raw = (
        '```json\n{"findings": [{"severity": "WARN", "finding": "bad", '
        '"dimension": "x"}]}\n```'
    )
    findings, _ = _parse_findings_from_claude(raw)
    assert len(findings) == 1
    assert findings[0]["severity"] == "WARN"


def test_parse_findings_empty_list() -> None:
    raw = json.dumps({"findings": []})
    findings, _ = _parse_findings_from_claude(raw)
    assert findings == []


def test_parse_findings_invalid_json_returns_empty() -> None:
    findings, _ = _parse_findings_from_claude("this is not json")
    assert findings == []


def test_parse_findings_top_level_list() -> None:
    """Claude may return a bare list rather than a wrapped object."""
    raw = json.dumps([{"severity": "ERROR", "finding": "crash", "dimension": "x"}])
    findings, _ = _parse_findings_from_claude(raw)
    assert len(findings) == 1
    assert findings[0]["finding"] == "crash"


def test_parse_findings_skips_non_dict_items() -> None:
    raw = json.dumps(
        {
            "findings": [
                {"finding": "ok", "severity": "INFO", "dimension": "x"},
                "not a dict",
                42,
            ]
        }
    )
    findings, _ = _parse_findings_from_claude(raw)
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# _anthropic_messages_create (respx-mocked HTTP)
# ---------------------------------------------------------------------------


@respx.mock
def test_anthropic_messages_create_sends_correct_request() -> None:
    """Verify the request shape sent to the Anthropic API."""
    response_body = {
        "content": [{"type": "text", "text": '{"findings": []}'}],
    }
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=response_body)
    )

    result = _anthropic_messages_create(
        api_key="test-key",
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        user_prompt="evaluate this",
    )

    assert route.called
    request = route.calls[0].request
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-4-20250514"
    assert body["max_tokens"] == 1000
    assert body["messages"] == [{"role": "user", "content": "evaluate this"}]
    assert request.headers["x-api-key"] == "test-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert result == '{"findings": []}'


@respx.mock
def test_anthropic_messages_create_concatenates_multiple_text_blocks() -> None:
    """Multiple text content blocks are concatenated into one string."""
    response_body = {
        "content": [
            {"type": "text", "text": "part one "},
            {"type": "text", "text": "part two"},
        ],
    }
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=response_body)
    )

    result = _anthropic_messages_create(
        api_key="k",
        model="claude-sonnet-4-20250514",
        max_tokens=100,
        user_prompt="x",
    )
    assert result == "part one part two"


@respx.mock
def test_anthropic_messages_create_raises_on_4xx() -> None:
    """HTTP 4xx responses raise httpx.HTTPStatusError."""
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        _anthropic_messages_create(
            api_key="bad-key",
            model="claude-sonnet-4-20250514",
            max_tokens=100,
            user_prompt="x",
        )


# ---------------------------------------------------------------------------
# _parse_findings_from_claude — edge cases
# ---------------------------------------------------------------------------


def test_parse_findings_valid_json_wrong_type_returns_empty() -> None:
    """Valid JSON that is neither dict nor list (e.g. a number) returns []."""
    findings, _ = _parse_findings_from_claude("42")
    assert findings == []


def test_parse_findings_json_string_returns_empty() -> None:
    """A bare JSON string is not a valid findings payload."""
    findings, _ = _parse_findings_from_claude('"just a string"')
    assert findings == []


def test_normalize_finding_empty_string_values_use_sentinel() -> None:
    """All recognisable keys present but empty — falls through to sentinel text."""
    item = {
        "finding": "",
        "message": "",
        "description": "",
        "detail": "",
        "text": "",
        "severity": "WARN",
    }
    result = _normalize_finding(item)
    assert result["finding"] == "No finding text returned by evaluator."


# ---------------------------------------------------------------------------
# build_conformance_prompt — branch coverage
# ---------------------------------------------------------------------------


def test_build_conformance_prompt_includes_monorepo_context(tmp_path: Path) -> None:
    """Monorepo context block appears in the prompt when monorepo_context is provided."""
    (tmp_path / "README.md").write_text("# Test repo\n")

    monorepo_ctx = {
        "monorepo_id": "deejaytools-com",
        "package_manager": "pnpm",
        "workspace_deps": ["kaiano-ts-utils"],
        "sibling_apps": [
            {"service_id": "deejaytools-com-api", "path": "apps/api"},
            {"service_id": "deejaytools-com-app", "path": "apps/app"},
        ],
    }

    prompt = build_conformance_prompt(
        repo_id="deejaytools-com-app",
        service_type="worker",
        language="typescript",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=[],
        monorepo_context=monorepo_ctx,
        repo_path=tmp_path,
    )

    assert "deejaytools-com" in prompt
    assert "pnpm" in prompt
    assert "kaiano-ts-utils" in prompt
    assert "XSTACK-001" in prompt
    assert "MONO-001" in prompt
    assert "deejaytools-com-api" in prompt


def test_build_conformance_prompt_truncates_long_readme(tmp_path: Path) -> None:
    """README files over 4000 chars are truncated and marked as such."""
    long_readme = "# Title\n" + ("x" * 4100)
    (tmp_path / "README.md").write_text(long_readme)

    prompt = build_conformance_prompt(
        repo_id="test-repo",
        service_type="worker",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=[],
        repo_path=tmp_path,
    )

    assert "(truncated)" in prompt
    assert "README.md CONTENT (first 4000 chars" in prompt


# ---------------------------------------------------------------------------
# evaluate_pipeline_run — no API key early return
# ---------------------------------------------------------------------------


def test_evaluate_pipeline_run_no_anthropic_key_does_not_post(monkeypatch) -> None:
    """Without ANTHROPIC_API_KEY, non-direct-finding calls return without posting."""
    from evaluator_cog.flows.pipeline_eval import evaluate_pipeline_run

    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    posted: list = []
    api = SimpleNamespace(post=MagicMock(side_effect=lambda *a, **_: posted.append(a)))

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r-no-key",
            repo="deejay-cog",
            sets_imported=1,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=10,
            failed_set_labels=[],
            api_ingest_success=True,
            sets_attempted=1,
            direct_finding_text=None,
        )

    assert posted == []


# ---------------------------------------------------------------------------
# _extract_flow_run_event_fields — nested resource payload
# ---------------------------------------------------------------------------


def test_extract_flow_run_event_fields_nested_resource() -> None:
    """Prefect Cloud nested resource format is unwrapped correctly."""
    from evaluator_cog.flows.pipeline_eval import _extract_flow_run_event_fields

    payload = {
        "resource": {
            "flow_run_id": "nested-run-id",
            "flow_name": "process-transcript",
            "state_name": "Failed",
            "state_type": "FAILED",
            "start_time": "2026-04-01T10:00:00Z",
            "end_time": "2026-04-01T10:01:00Z",
        }
    }

    fields = _extract_flow_run_event_fields(payload)

    assert fields["flow_run_id"] == "nested-run-id"
    assert fields["flow_name"] == "process-transcript"
    assert fields["state_type"] == "FAILED"


def test_extract_flow_run_event_fields_flat_payload() -> None:
    """Flat (non-nested) payload is parsed directly."""
    from evaluator_cog.flows.pipeline_eval import _extract_flow_run_event_fields

    payload = {
        "flow_run_id": "flat-run-id",
        "flow_name": "update-dj-set-collection",
        "state_name": "Completed",
        "state_type": "COMPLETED",
        "start_time": "2026-04-01T10:00:00Z",
        "end_time": "2026-04-01T10:01:00Z",
    }

    fields = _extract_flow_run_event_fields(payload)

    assert fields["flow_run_id"] == "flat-run-id"
    assert fields["state_type"] == "COMPLETED"


# ---------------------------------------------------------------------------
# _flow_name_to_repo — unknown flow returns "unknown"
# ---------------------------------------------------------------------------


def test_flow_name_to_repo_unknown_returns_unknown() -> None:
    """Unknown flow names now return 'unknown' instead of 'deejay-cog'."""
    from evaluator_cog.flows.pipeline_eval import _flow_name_to_repo

    assert _flow_name_to_repo("some-brand-new-flow") == "unknown"
    assert _flow_name_to_repo("") == "unknown"
    assert _flow_name_to_repo("watcher-flow") == "unknown"


def test_flow_name_to_repo_known_flows_unchanged() -> None:
    """Known flow names still map to their correct repos."""
    from evaluator_cog.flows.pipeline_eval import _flow_name_to_repo

    assert _flow_name_to_repo("process-transcript") == "notes-ingest-cog"
    assert _flow_name_to_repo("conformance-check") == "evaluator-cog"
    assert _flow_name_to_repo("pipeline-eval") == "evaluator-cog"
    assert _flow_name_to_repo("process-new-csv-files") == "deejay-cog"
    assert _flow_name_to_repo("ingest-live-history") == "deejay-cog"
    assert _flow_name_to_repo("generate-summaries") == "deejay-cog"
    assert _flow_name_to_repo("update-dj-set-collection") == "deejay-cog"
    assert _flow_name_to_repo("retag-music") == "deejay-cog"


def test_parse_findings_findings_value_not_list_returns_empty() -> None:
    """When 'findings' key exists but value is not a list, returns []."""
    raw = '{"findings": "this should be a list not a string"}'
    findings, _ = _parse_findings_from_claude(raw)
    assert findings == []


def test_build_conformance_prompt_with_check_exceptions_and_reasons() -> None:
    """check_exceptions with reason strings appear formatted in the prompt."""
    prompt = build_conformance_prompt(
        repo_id="test-repo",
        service_type="worker",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=[],
        check_exceptions=["CD-015", "PIPE-008"],
        exception_reasons={
            "CD-015": "Multi-flow structure",
            "PIPE-008": "String literal only",
        },
        repo_path=None,
    )
    assert "CD-015" in prompt
    assert "Multi-flow structure" in prompt
    assert "PIPE-008" in prompt
    assert "String literal only" in prompt


def test_build_conformance_prompt_with_no_repo_path_omits_inventory() -> None:
    """When repo_path is None, no file inventory or README block appears."""
    prompt = build_conformance_prompt(
        repo_id="test-repo",
        service_type="worker",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=[],
        repo_path=None,
    )
    assert "REPO FILE INVENTORY" not in prompt
    assert "README.md CONTENT" not in prompt


def test_build_conformance_prompt_inventory_truncated_beyond_60_files(
    tmp_path: Path,
) -> None:
    """Repos with more than 60 files get a truncation marker in the inventory."""
    for i in range(65):
        (tmp_path / f"file_{i:03d}.py").write_text("")

    prompt = build_conformance_prompt(
        repo_id="test-repo",
        service_type="worker",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=[],
        repo_path=tmp_path,
    )
    assert "more files" in prompt


# ---------------------------------------------------------------------------
# build_conformance_prompt — DETERMINISTIC/LLM routing filter
# ---------------------------------------------------------------------------


def _rule(
    rule_id: str,
    *,
    title: str = "",
    severity: str = "WARN",
    check_notes: str = "",
    check_mode: str | None = "llm",
) -> dict:
    """Construct a minimal standards_rules dict for prompt-filter tests."""
    d = {
        "id": rule_id,
        "title": title or f"{rule_id} title",
        "severity": severity,
        "check_notes": check_notes or f"How to check {rule_id}.",
    }
    if check_mode is not None:
        d["check_mode"] = check_mode
    return d


def test_llm_marked_rule_reaches_the_prompt() -> None:
    """A rule with check_mode='llm' appears in the RULES TO ASSESS block."""
    rules = [_rule("PIPE-013", check_mode="llm")]
    prompt = build_conformance_prompt(
        repo_id="r",
        service_type="worker",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=rules,
    )
    # Isolate the soft-rules listing — the block between 'RULES TO ASSESS:'
    # and the 'WHAT YOU ARE AND ARE NOT RESPONSIBLE FOR' divider. This is
    # what Claude is asked to judge.
    assess_section = prompt.split("RULES TO ASSESS:", 1)[1]
    listing_block, _, _ = assess_section.partition("WHAT YOU ARE AND ARE NOT")
    assert "PIPE-013" in listing_block
    assert "none — all checkable rules covered" not in listing_block


def test_deterministic_marked_rule_is_filtered_out_even_when_not_checked() -> None:
    """
    A rule the catalog has marked DETERMINISTIC should NOT reach the LLM
    prompt's soft-rule assessment list, even if no deterministic finding
    exists for it (i.e. the engine has not yet implemented the check).
    This is the leak the routing change exists to close.
    """
    rules = [_rule("PIPE-002", check_mode="deterministic")]
    prompt = build_conformance_prompt(
        repo_id="r",
        service_type="worker",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        # checked_rule_ids deliberately empty — simulates the engine not
        # having run or not having a check for this rule yet.
        checked_rule_ids=set(),
        standards_rules=rules,
    )
    assess_section = prompt.split("RULES TO ASSESS:", 1)[1]
    listing_block, _, _ = assess_section.partition("WHAT YOU ARE AND ARE NOT")
    assert "PIPE-002" not in listing_block


def test_rule_without_check_mode_defaults_to_deterministic_filter() -> None:
    """
    A rule dict missing the check_mode field is treated as deterministic
    for filtering purposes. This matches the classifier's default and
    guards against regressions if the fetch layer ever forgets to
    attach check_mode.
    """
    rules = [_rule("LEGACY-001", check_mode=None)]
    prompt = build_conformance_prompt(
        repo_id="r",
        service_type="worker",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=rules,
    )
    assess_section = prompt.split("RULES TO ASSESS:", 1)[1]
    listing_block, _, _ = assess_section.partition("WHAT YOU ARE AND ARE NOT")
    assert "LEGACY-001" not in listing_block


def test_mixed_rules_partition_correctly_between_context_and_assessment() -> None:
    """
    In a mixed bag of rules, only LLM-marked ones land in the soft-rule
    assessment block; both kinds remain visible in the context list.
    """
    rules = [
        _rule("DOC-005", check_mode="deterministic"),
        _rule("PIPE-013", check_mode="llm"),
        _rule("XSTACK-005", check_mode="llm"),
        _rule("API-001", check_mode="deterministic"),
    ]
    prompt = build_conformance_prompt(
        repo_id="r",
        service_type="worker",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=rules,
    )

    # All four rules appear in the context block ("STANDARDS RULES FOR
    # THIS SERVICE TYPE") — that block is informational, not the filter.
    context_block, _, assess_section = prompt.partition("RULES TO ASSESS:")
    for rid in ("DOC-005", "PIPE-013", "XSTACK-005", "API-001"):
        assert rid in context_block

    # Only LLM-marked rules reach the assessment listing.
    listing_block, _, _ = assess_section.partition("WHAT YOU ARE AND ARE NOT")
    assert "PIPE-013" in listing_block
    assert "XSTACK-005" in listing_block
    assert "DOC-005" not in listing_block
    assert "API-001" not in listing_block


def test_llm_rule_already_covered_by_deterministic_finding_is_filtered() -> None:
    """
    An LLM-marked rule that nevertheless received a deterministic finding
    (e.g. during a split rollout where the old deterministic check still
    fires) should not be re-assessed by the LLM. Existing "already checked"
    semantics take precedence over the routing marker.
    """
    rules = [_rule("PIPE-013", check_mode="llm")]
    prompt = build_conformance_prompt(
        repo_id="r",
        service_type="worker",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[
            {
                "rule_id": "PIPE-013",
                "severity": "WARN",
                "dimension": "structural_conformance",
                "finding": "something",
            }
        ],
        standards_rules=rules,
    )
    # Isolate the soft-rules listing block — the section between
    # 'RULES TO ASSESS:' and the trailing 'WHAT YOU ARE AND ARE NOT RESPONSIBLE FOR'
    # divider. PIPE-013 may legitimately appear elsewhere in the prompt
    # (the deterministic-findings summary, the resolved list, etc.);
    # what matters is that it's not in the list Claude is asked to judge.
    assess_section = prompt.split("RULES TO ASSESS:", 1)[1]
    listing_block, _, _ = assess_section.partition("WHAT YOU ARE AND ARE NOT")
    assert "PIPE-013" not in listing_block
    # And the block should explicitly say there is nothing to assess.
    assert "none — all checkable rules covered" in listing_block


def test_excepted_llm_rule_is_filtered() -> None:
    """An LLM-marked rule in check_exceptions should not reach the prompt."""
    rules = [_rule("PIPE-013", check_mode="llm")]
    prompt = build_conformance_prompt(
        repo_id="r",
        service_type="worker",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=rules,
        check_exceptions=["PIPE-013"],
        exception_reasons={"PIPE-013": "does not apply here"},
    )
    assess_section = prompt.split("RULES TO ASSESS:", 1)[1]
    listing_block, _, _ = assess_section.partition("WHAT YOU ARE AND ARE NOT")
    assert "PIPE-013" not in listing_block
