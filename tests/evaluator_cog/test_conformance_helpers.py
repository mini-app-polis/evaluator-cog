"""Tests for pure helper functions in flows/conformance.py.

These helpers are independently testable without mocking the full
conformance_check_flow (which requires GitHub API, Prefect runtime, etc.).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from evaluator_cog.flows.conformance import (
    _deduplicate_sibling_findings,
    _fetch_yaml,
    _get_active_repos,
    _get_monorepos,
    _get_standards_version,
    _on_completion,
    _parse_check_exceptions,
    _read_workspace_package_json,
)

# ---------------------------------------------------------------------------
# _on_completion — Healthchecks.io ping
# ---------------------------------------------------------------------------


def test_on_completion_pings_healthchecks_when_url_set(monkeypatch) -> None:
    """When HEALTHCHECKS_URL_EVALUATOR is set, urlopen is called once."""
    monkeypatch.setenv("HEALTHCHECKS_URL_EVALUATOR", "https://hc-ping.com/test-uuid")

    with patch("urllib.request.urlopen") as mock_urlopen:
        _on_completion(None, None, None)

    mock_urlopen.assert_called_once()
    args = mock_urlopen.call_args[0]
    assert "hc-ping.com" in str(args[0])


def test_on_completion_skips_when_url_unset(monkeypatch) -> None:
    """When HEALTHCHECKS_URL_EVALUATOR is absent, urlopen is never called."""
    monkeypatch.delenv("HEALTHCHECKS_URL_EVALUATOR", raising=False)

    with patch("urllib.request.urlopen") as mock_urlopen:
        _on_completion(None, None, None)

    mock_urlopen.assert_not_called()


def test_on_completion_swallows_urlopen_exception(monkeypatch) -> None:
    """Exceptions from urlopen are suppressed — _on_completion never raises."""
    monkeypatch.setenv("HEALTHCHECKS_URL_EVALUATOR", "https://hc-ping.com/test-uuid")

    with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
        _on_completion(None, None, None)


# ---------------------------------------------------------------------------
# _fetch_yaml — HTTP YAML fetch
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_yaml_returns_parsed_dict() -> None:
    """Valid YAML response is parsed and returned."""
    respx.get("https://example.com/data.yaml").mock(
        return_value=httpx.Response(200, text="version: 3.0.1\nstatus: active\n")
    )
    result = _fetch_yaml("https://example.com/data.yaml")
    assert result == {"version": "3.0.1", "status": "active"}


@respx.mock
def test_fetch_yaml_returns_empty_on_http_error() -> None:
    """Non-2xx response returns {} without raising."""
    respx.get("https://example.com/missing.yaml").mock(return_value=httpx.Response(404))
    result = _fetch_yaml("https://example.com/missing.yaml")
    assert result == {}


@respx.mock
def test_fetch_yaml_returns_empty_on_network_error() -> None:
    """Network exception returns {} without raising."""
    respx.get("https://example.com/broken.yaml").mock(
        side_effect=httpx.ConnectError("refused", request=None)
    )
    result = _fetch_yaml("https://example.com/broken.yaml")
    assert result == {}


# ---------------------------------------------------------------------------
# _get_standards_version — live version fetch
# ---------------------------------------------------------------------------


@respx.mock
def test_get_standards_version_returns_version_string() -> None:
    """Valid package.json with version field returns the version string."""
    respx.get(
        "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/package.json"
    ).mock(return_value=httpx.Response(200, text='{"version": "3.0.1"}'))

    version = _get_standards_version()
    assert version == "3.0.1"


@respx.mock
def test_get_standards_version_raises_when_version_absent() -> None:
    """package.json missing version field raises RuntimeError."""
    respx.get(
        "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/package.json"
    ).mock(return_value=httpx.Response(200, text='{"name": "ecosystem-standards"}'))

    with pytest.raises(RuntimeError, match="package.json fetch failed"):
        _get_standards_version()


@respx.mock
def test_get_standards_version_raises_on_http_failure() -> None:
    """HTTP failure raises RuntimeError."""
    respx.get(
        "https://raw.githubusercontent.com/mini-app-polis/ecosystem-standards/main/package.json"
    ).mock(return_value=httpx.Response(503))

    with pytest.raises(RuntimeError, match="package.json fetch failed"):
        _get_standards_version()


# ---------------------------------------------------------------------------
# _get_active_repos — pure dict parsing
# ---------------------------------------------------------------------------


def test_get_active_repos_filters_by_status() -> None:
    """Only services with status='active' are returned."""
    ecosystem = {
        "services": [
            {"id": "a", "status": "active"},
            {"id": "b", "status": "retired"},
            {"id": "c", "status": "active"},
        ]
    }
    result = _get_active_repos(ecosystem)
    assert [s["id"] for s in result] == ["a", "c"]


def test_get_active_repos_empty_ecosystem() -> None:
    """Empty or missing services key returns []."""
    assert _get_active_repos({}) == []
    assert _get_active_repos({"services": []}) == []


# ---------------------------------------------------------------------------
# _get_monorepos — pure dict parsing
# ---------------------------------------------------------------------------


def test_get_monorepos_returns_id_keyed_dict() -> None:
    """Monorepo records are keyed by their id field."""
    ecosystem = {
        "monorepos": [
            {"id": "deejaytools-com", "repo": "deejaytools-com", "apps": []},
            {"id": "other-mono", "repo": "other-mono", "apps": []},
        ]
    }
    result = _get_monorepos(ecosystem)
    assert set(result.keys()) == {"deejaytools-com", "other-mono"}
    assert result["deejaytools-com"]["repo"] == "deejaytools-com"


def test_get_monorepos_skips_entries_without_id() -> None:
    """Entries missing id are excluded from the result."""
    ecosystem = {
        "monorepos": [
            {"repo": "no-id-repo"},
            {"id": "valid-mono", "repo": "valid-mono"},
        ]
    }
    result = _get_monorepos(ecosystem)
    assert list(result.keys()) == ["valid-mono"]


# ---------------------------------------------------------------------------
# _read_workspace_package_json — filesystem helper
# ---------------------------------------------------------------------------


def test_read_workspace_package_json_returns_lowercased_content(
    tmp_path: Path,
) -> None:
    """Returns the lowercased content of package.json when present."""
    (tmp_path / "package.json").write_text('{"name": "MyMonorepo"}')
    result = _read_workspace_package_json(tmp_path)
    assert result == '{"name": "mymonorepo"}'


def test_read_workspace_package_json_returns_empty_when_absent(
    tmp_path: Path,
) -> None:
    """Returns empty string when package.json does not exist."""
    result = _read_workspace_package_json(tmp_path)
    assert result == ""


# ---------------------------------------------------------------------------
# _parse_check_exceptions — pure parsing
# ---------------------------------------------------------------------------


def test_parse_check_exceptions_plain_string_format() -> None:
    """Legacy plain string format is parsed correctly."""
    ids, reasons = _parse_check_exceptions(["CD-015", "PIPE-008"])
    assert ids == ["CD-015", "PIPE-008"]
    assert reasons == {}


def test_parse_check_exceptions_structured_format() -> None:
    """New structured {rule, reason} format is parsed correctly."""
    raw = [
        {"rule": "CD-015", "reason": "Multi-flow structure"},
        {"rule": "PIPE-008", "reason": "String literal only"},
    ]
    ids, reasons = _parse_check_exceptions(raw)
    assert ids == ["CD-015", "PIPE-008"]
    assert reasons["CD-015"] == "Multi-flow structure"
    assert reasons["PIPE-008"] == "String literal only"


def test_parse_check_exceptions_mixed_formats() -> None:
    """Legacy strings and structured dicts can coexist in the same list."""
    raw = [
        "CD-015",
        {"rule": "PIPE-008", "reason": "String literal only"},
    ]
    ids, reasons = _parse_check_exceptions(raw)
    assert "CD-015" in ids
    assert "PIPE-008" in ids
    assert "PIPE-008" in reasons
    assert "CD-015" not in reasons


def test_parse_check_exceptions_strips_inline_comments() -> None:
    """Legacy strings with # comments have the comment stripped."""
    ids, _ = _parse_check_exceptions(["CD-015  # no longer needed"])
    assert ids == ["CD-015"]


def test_parse_check_exceptions_skips_empty_rule_ids() -> None:
    """Structured entries with empty rule field are skipped."""
    raw = [{"rule": "", "reason": "should be ignored"}]
    ids, reasons = _parse_check_exceptions(raw)
    assert ids == []
    assert reasons == {}


def test_parse_check_exceptions_empty_input() -> None:
    """Empty list and None both return empty results."""
    assert _parse_check_exceptions([]) == ([], {})
    assert _parse_check_exceptions(None) == ([], {})


# ---------------------------------------------------------------------------
# _deduplicate_sibling_findings — pure logic
# ---------------------------------------------------------------------------


def test_deduplicate_sibling_findings_collapses_identical_findings() -> None:
    """Identical rule_id+finding across siblings is collapsed into the primary."""
    findings_by_service = {
        "svc-a": [
            {
                "rule_id": "XSTACK-001",
                "finding": "Shared lib missing.",
                "severity": "WARN",
            }
        ],
        "svc-b": [
            {
                "rule_id": "XSTACK-001",
                "finding": "Shared lib missing.",
                "severity": "WARN",
            }
        ],
    }
    result = _deduplicate_sibling_findings(findings_by_service)
    assert result["svc-b"] == []
    assert "also affects svc-b" in result["svc-a"][0]["finding"]


def test_deduplicate_sibling_findings_keeps_distinct_findings() -> None:
    """Non-identical findings are kept on each sibling."""
    findings_by_service = {
        "svc-a": [
            {"rule_id": "DOC-001", "finding": "README missing.", "severity": "ERROR"}
        ],
        "svc-b": [
            {"rule_id": "CD-010", "finding": "Sentry missing.", "severity": "ERROR"}
        ],
    }
    result = _deduplicate_sibling_findings(findings_by_service)
    assert len(result["svc-a"]) == 1
    assert len(result["svc-b"]) == 1


def test_deduplicate_sibling_findings_single_service_unchanged() -> None:
    """With only one service, no deduplication occurs."""
    findings_by_service = {
        "svc-a": [
            {"rule_id": "DOC-001", "finding": "README missing.", "severity": "ERROR"}
        ]
    }
    result = _deduplicate_sibling_findings(findings_by_service)
    assert result == findings_by_service
