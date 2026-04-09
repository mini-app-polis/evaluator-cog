"""Tests for engine/llm.py — normalize, parse, and HTTP client."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from evaluator_cog.engine.llm import (
    _anthropic_messages_create,
    _normalize_finding,
    _parse_findings_from_claude,
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
