"""Tests for engine/llm.py — normalize, parse, and HTTP client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from evaluator_cog.engine.llm import (
    LLMResponseError,
    _anthropic_messages_create,
    _gather_evidence_files,
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


def test_parse_findings_invalid_json_raises() -> None:
    """Not JSON is an unreadable answer, never an empty one."""
    with pytest.raises(LLMResponseError, match="not JSON"):
        _parse_findings_from_claude("this is not json")


def test_parse_findings_truncated_json_raises() -> None:
    """A reply cut off mid-payload must not read as a clean pass."""
    with pytest.raises(LLMResponseError):
        _parse_findings_from_claude('{"findings": [{"severity": "WARN", "fin')


def test_parse_findings_non_list_findings_raises() -> None:
    with pytest.raises(LLMResponseError, match="not a findings payload"):
        _parse_findings_from_claude(json.dumps({"findings": "none"}))


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


@respx.mock
def test_anthropic_messages_create_raises_when_truncated() -> None:
    """stop_reason max_tokens means the payload was cut off."""
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"findings": [{"sev'}],
                "stop_reason": "max_tokens",
            },
        )
    )
    with pytest.raises(LLMResponseError, match="truncated at max_tokens=100"):
        _anthropic_messages_create(
            api_key="k", model="m", max_tokens=100, user_prompt="x"
        )


# ---------------------------------------------------------------------------
# _parse_findings_from_claude — edge cases
# ---------------------------------------------------------------------------


def test_parse_findings_valid_json_wrong_type_raises() -> None:
    """Valid JSON that is neither dict nor list (e.g. a number) raises."""
    with pytest.raises(LLMResponseError):
        _parse_findings_from_claude("42")


def test_parse_findings_json_string_raises() -> None:
    """A bare JSON string is not a valid findings payload."""
    with pytest.raises(LLMResponseError):
        _parse_findings_from_claude('"just a string"')


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


# ---------------------------------------------------------------------------
# _extract_flow_run_event_fields — nested resource payload
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _flow_name_to_repo — unknown flow returns "unknown"
# ---------------------------------------------------------------------------


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


def test_gather_evidence_includes_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\ndependencies=['sentry-sdk>=2.0']\n"
    )
    evidence = _gather_evidence_files(tmp_path)
    assert "=== pyproject.toml ===" in evidence
    assert "sentry-sdk" in evidence


def test_gather_evidence_respects_budget(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("p" * 20000)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("m" * 20000)
    (tmp_path / "src" / "app.py").write_text("a" * 20000)
    (tmp_path / "src" / "__main__.py").write_text("b" * 20000)
    (tmp_path / "src" / "logger.py").write_text("l" * 20000)
    (tmp_path / "src" / "observability.py").write_text("o" * 20000)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_big.py").write_text("t" * 20000)
    (tmp_path / "tests" / "test_big_two.py").write_text("u" * 20000)
    (tmp_path / "tests" / "test_big_three.py").write_text("v" * 20000)
    (tmp_path / "tests" / "test_big_four.py").write_text("w" * 20000)

    evidence = _gather_evidence_files(tmp_path, total_budget_chars=30000)
    assert len(evidence) <= 30000
    assert (
        "Files matching evidence patterns but not included due to 30000-char budget"
        in evidence
    )


def test_gather_evidence_skips_node_modules(tmp_path: Path) -> None:
    node_pkg = tmp_path / "node_modules" / "some-pkg"
    node_pkg.mkdir(parents=True)
    (node_pkg / "package.json").write_text('{"name":"skip-me"}')
    evidence = _gather_evidence_files(tmp_path)
    assert "node_modules/some-pkg/package.json" not in evidence


def test_gather_evidence_truncates_large_files(tmp_path: Path) -> None:
    payload = "x" * 15000
    (tmp_path / "pyproject.toml").write_text(payload)
    evidence = _gather_evidence_files(tmp_path)
    assert "=== pyproject.toml ===" in evidence
    assert payload[:6000] in evidence
    assert "...(truncated, 9000 more chars)" in evidence


def test_gather_evidence_includes_singular_flow_py(tmp_path: Path) -> None:
    flow_dir = tmp_path / "src" / "somecog"
    flow_dir.mkdir(parents=True)
    (flow_dir / "flow.py").write_text("FLOW_MARKER = 'single-flow-cog'\n")

    evidence = _gather_evidence_files(tmp_path)

    assert "=== src/somecog/flow.py ===" in evidence
    assert "single-flow-cog" in evidence


def test_build_conformance_prompt_includes_evidence_block(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    prompt = build_conformance_prompt(
        repo_id="demo",
        service_type="api-service",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=[],
        repo_path=tmp_path,
    )
    assert "REPO FILE CONTENTS" in prompt
    assert "=== pyproject.toml ===" in prompt
    assert "name='demo'" in prompt


def test_build_conformance_prompt_no_repo_path() -> None:
    prompt = build_conformance_prompt(
        repo_id="demo",
        service_type="api-service",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=[],
        repo_path=None,
    )
    assert (
        "REPO FILE CONTENTS (curated evidence — read this for rule assessment):"
        not in prompt
    )


def test_api_kaianolevine_com_style_repo_does_not_trigger_false_prin_005(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'api-kaianolevine-com'\n"
        "dependencies = ['sentry-sdk>=2.0', 'common-python-utils>=1.0']\n"
    )
    app_dir = tmp_path / "src" / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "main.py").write_text(
        "import sentry_sdk\n"
        "from mini_app_polis.logger import get_logger\n"
        "sentry_sdk.init(dsn='x')\n"
        "logger = get_logger(__name__)\n"
    )
    prompt = build_conformance_prompt(
        repo_id="api-kaianolevine-com",
        service_type="api-service",
        language="python",
        standards_version="3.0.1",
        deterministic_findings=[],
        standards_rules=[
            {
                "id": "PRIN-005",
                "severity": "WARN",
                "title": "Use sentry and common logging primitives",
                "check_notes": "Check pyproject and main app initialization.",
                "check_mode": "llm",
            }
        ],
        repo_path=tmp_path,
    )
    assert "=== pyproject.toml ===" in prompt
    assert "sentry-sdk" in prompt
    assert "common-python-utils" in prompt
    assert "=== src/app/main.py ===" in prompt
    assert "sentry_sdk.init" in prompt
    assert "mini_app_polis.logger" in prompt


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


# ---------------------------------------------------------------------------
# _anthropic_messages_create — retry
# ---------------------------------------------------------------------------

_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_OK = httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})


def _call() -> str:
    return _anthropic_messages_create(
        api_key="k", model="m", max_tokens=10, user_prompt="x"
    )


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr("evaluator_cog.engine.llm.time.sleep", delays.append)
    return delays


@respx.mock
def test_anthropic_retries_overload_then_succeeds(slept: list[float]) -> None:
    """A 529 is Anthropic being busy, not the request being wrong."""
    route = respx.post(_MESSAGES_URL).mock(
        side_effect=[httpx.Response(529, json={}), httpx.Response(429, json={}), _OK]
    )
    assert _call() == "ok"
    assert route.call_count == 3
    assert slept == [2.0, 4.0]


@respx.mock
def test_anthropic_honours_retry_after(slept: list[float]) -> None:
    respx.post(_MESSAGES_URL).mock(
        side_effect=[httpx.Response(429, headers={"retry-after": "7"}), _OK]
    )
    assert _call() == "ok"
    assert slept == [7.0]


@respx.mock
def test_anthropic_gives_up_after_three_attempts(slept: list[float]) -> None:
    route = respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(503, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        _call()
    assert route.call_count == 3
    assert len(slept) == 2


@respx.mock
def test_anthropic_does_not_retry_a_bad_request(slept: list[float]) -> None:
    route = respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(400, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        _call()
    assert route.call_count == 1
    assert slept == []


@respx.mock
def test_anthropic_retries_a_connection_error(slept: list[float]) -> None:
    route = respx.post(_MESSAGES_URL).mock(
        side_effect=[httpx.ConnectError("refused"), _OK]
    )
    assert _call() == "ok"
    assert route.call_count == 2


@respx.mock
def test_anthropic_does_not_retry_a_read_timeout(slept: list[float]) -> None:
    """A slow model costs a full timeout per attempt; the job's deadline is fixed."""
    route = respx.post(_MESSAGES_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        _call()
    assert route.call_count == 1
