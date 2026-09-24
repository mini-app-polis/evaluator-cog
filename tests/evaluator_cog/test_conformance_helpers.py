"""Tests for pure helper functions in flows/conformance.py.

These helpers are independently testable without mocking a whole sweep
(which requires the GitHub API, the standards catalog, and the registry).
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from evaluator_cog.flows.conformance import (
    RunContext,
    _fetch_yaml,
    _get_standards_version,
    _parse_check_exceptions,
    _ping_healthcheck,
    _resolve_language,
)

#: Where the evaluator reads the catalog. Always production — see the
#: constant's own note in conformance.py.
_CATALOG_URL = "https://api.kaianolevine.com/v1/standards/catalog"


def _catalog_response(catalog: dict) -> httpx.Response:
    """A catalog envelope, non-empty unless a test says otherwise."""
    body: dict = {
        "rules": [{"id": "PY-001", "checkable": True, "check_mode": "deterministic"}]
    }
    body.update(catalog)
    return httpx.Response(200, json={"data": body, "meta": {}})


# ---------------------------------------------------------------------------
# _ping_healthcheck — Healthchecks.io ping
# ---------------------------------------------------------------------------


def test_ping_healthcheck_pings_healthchecks_when_url_set(monkeypatch) -> None:
    """When HEALTHCHECKS_URL_EVALUATOR is set, urlopen is called once."""
    monkeypatch.setenv("HEALTHCHECKS_URL_EVALUATOR", "https://hc-ping.com/test-uuid")

    with patch("urllib.request.urlopen") as mock_urlopen:
        _ping_healthcheck()

    mock_urlopen.assert_called_once()
    args = mock_urlopen.call_args[0]
    assert "hc-ping.com" in str(args[0])


def test_ping_healthcheck_skips_when_url_unset(monkeypatch) -> None:
    """When HEALTHCHECKS_URL_EVALUATOR is absent, urlopen is never called."""
    monkeypatch.delenv("HEALTHCHECKS_URL_EVALUATOR", raising=False)

    with patch("urllib.request.urlopen") as mock_urlopen:
        _ping_healthcheck()

    mock_urlopen.assert_not_called()


def test_ping_healthcheck_swallows_urlopen_exception(monkeypatch) -> None:
    """Exceptions from urlopen are suppressed — _ping_healthcheck never raises."""
    monkeypatch.setenv("HEALTHCHECKS_URL_EVALUATOR", "https://hc-ping.com/test-uuid")

    with patch(
        "urllib.request.urlopen", side_effect=OSError("timeout")
    ) as mock_urlopen:
        # Must not raise — _ping_healthcheck uses suppress(Exception) internally
        _ping_healthcheck()

    # Confirm urlopen was actually attempted (and thus its exception was swallowed,
    # rather than _ping_healthcheck early-returning for some other reason).
    mock_urlopen.assert_called_once()


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
    """The version comes from the catalog itself, not a separate fetch."""
    respx.get(_CATALOG_URL).mock(return_value=_catalog_response({"version": "3.0.1"}))

    assert _get_standards_version(ctx=RunContext()) == "3.0.1"


@respx.mock
def test_get_standards_version_raises_when_version_absent() -> None:
    """A catalog with no version cannot pin a finding to anything."""
    respx.get(_CATALOG_URL).mock(return_value=_catalog_response({}))

    with pytest.raises(RuntimeError, match="carries no version"):
        _get_standards_version(ctx=RunContext())


@respx.mock
def test_get_standards_version_raises_on_http_failure() -> None:
    """Every transport failure surfaces as one error naming the address."""
    respx.get(_CATALOG_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(RuntimeError, match="Cannot read the standards catalog"):
        _get_standards_version(ctx=RunContext())


@respx.mock
def test_empty_catalog_raises_rather_than_evaluating_nothing() -> None:
    """Zero rules is indistinguishable from a clean fleet. It must fail.

    The functions this replaced returned partial data on a fetch error so a
    run could limp on. With one source that trade is wrong: no rules means
    no findings means a green run that graded nothing.
    """
    respx.get(_CATALOG_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"version": "9.0.0", "rules": []}}
        )
    )

    with pytest.raises(RuntimeError, match="returned no rules"):
        _get_standards_version(ctx=RunContext())


# ---------------------------------------------------------------------------
# _get_active_repos — pure dict parsing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _parse_check_exceptions — pure parsing
# ---------------------------------------------------------------------------


def test_resolve_language_prefers_the_declared_language(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}")
    assert _resolve_language({"language": "python"}, tmp_path) == "python"


def test_resolve_language_maps_astro_to_typescript(tmp_path) -> None:
    assert _resolve_language({"language": "astro"}, tmp_path) == "typescript"


def test_resolve_language_detects_typescript_from_package_json(tmp_path) -> None:
    """A release-triggered event carries no language."""
    (tmp_path / "package.json").write_text("{}")
    assert _resolve_language({"id": "deejaytools-api"}, tmp_path) == "typescript"


def test_resolve_language_detects_python_from_pyproject(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "package.json").write_text("{}")
    assert _resolve_language({"id": "a-cog"}, tmp_path) == "python"


def test_resolve_language_detects_a_terraform_root(tmp_path) -> None:
    """mini-app-polis/infra: .tf at the root, no Python or Node manifest."""
    (tmp_path / "versions.tf").write_text("terraform {}\n")
    assert _resolve_language({"id": "infra"}, tmp_path) == "hcl"


def test_resolve_language_prefers_a_manifest_to_terraform(tmp_path) -> None:
    """A repository with code keeps its language even beside a .tf file."""
    (tmp_path / "versions.tf").write_text("terraform {}\n")
    (tmp_path / "pyproject.toml").write_text("")
    assert _resolve_language({"id": "a-cog"}, tmp_path) == "python"


def test_resolve_language_defaults_to_python(tmp_path) -> None:
    assert _resolve_language({"id": "unknown"}, tmp_path) == "python"


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
