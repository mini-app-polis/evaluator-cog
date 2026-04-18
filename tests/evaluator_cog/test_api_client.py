"""Tests for engine/api_client.py — dedup fetch response shape variants."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import respx

from evaluator_cog.engine.api_client import _get_latest_stored_finding, post_findings

# ---------------------------------------------------------------------------
# _get_latest_stored_finding — response shape variants
# ---------------------------------------------------------------------------


def _make_client(response: object) -> object:
    """Return a SimpleNamespace api_client whose .get returns `response`."""
    return SimpleNamespace(get=MagicMock(return_value=response))


def test_get_latest_data_list_shape() -> None:
    """{'data': [{...}]} response shape returns the first item."""
    client = _make_client(
        {"data": [{"finding": "old finding", "severity": "WARN", "dimension": "x"}]}
    )
    result = _get_latest_stored_finding(
        api_client=client, api_base_url="https://test", repo="my-repo"
    )
    assert result is not None
    assert result["finding"] == "old finding"


def test_get_latest_items_list_shape() -> None:
    """{'items': [{...}]} response shape returns the first item."""
    client = _make_client(
        {"items": [{"finding": "items finding", "severity": "INFO", "dimension": "x"}]}
    )
    result = _get_latest_stored_finding(
        api_client=client, api_base_url="https://test", repo="my-repo"
    )
    assert result is not None
    assert result["finding"] == "items finding"


def test_get_latest_bare_list_shape() -> None:
    """A bare list response returns the first item."""
    client = _make_client(
        [{"finding": "list finding", "severity": "ERROR", "dimension": "x"}]
    )
    result = _get_latest_stored_finding(
        api_client=client, api_base_url="https://test", repo="my-repo"
    )
    assert result is not None
    assert result["finding"] == "list finding"


def test_get_latest_empty_data_list_returns_none() -> None:
    """{'data': []} — empty list — returns None."""
    client = _make_client({"data": []})
    result = _get_latest_stored_finding(
        api_client=client, api_base_url="https://test", repo="my-repo"
    )
    assert result is None


def test_get_latest_returns_none_on_exception() -> None:
    """Any exception in the fetch returns None rather than raising."""
    client = SimpleNamespace(get=MagicMock(side_effect=RuntimeError("network error")))
    result = _get_latest_stored_finding(
        api_client=client, api_base_url="https://test", repo="my-repo"
    )
    assert result is None


@respx.mock
def test_get_latest_httpx_fallback_path() -> None:
    """When api_client has no .get, falls back to httpx GET."""
    client_without_get = SimpleNamespace()

    respx.get(url=re.compile(r"https://fallback-api\.test/v1/evaluations\?.*")).mock(
        return_value=httpx.Response(
            200,
            json=[{"finding": "httpx finding", "severity": "WARN", "dimension": "x"}],
        )
    )

    result = _get_latest_stored_finding(
        api_client=client_without_get,
        api_base_url="https://fallback-api.test",
        repo="my-repo",
    )
    assert result is not None
    assert result["finding"] == "httpx finding"


# ---------------------------------------------------------------------------
# post_findings — empty finding_text skip
# ---------------------------------------------------------------------------


def test_post_findings_skips_empty_finding_text(monkeypatch) -> None:
    """Findings with empty or whitespace-only 'finding' text are skipped."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=lambda *_, **__: None)

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = api
        post_findings(
            findings=[
                {"dimension": "x", "severity": "WARN", "finding": "   "},
                {"dimension": "x", "severity": "WARN", "finding": ""},
                {"dimension": "x", "severity": "INFO", "finding": "real finding"},
            ],
            run_id="run-skip-test",
            repo="test-repo",
            flow_name=None,
            source="conformance_check",
            standards_version="3.0.1",
        )

    assert len(posted) == 1
    assert posted[0]["finding"] == "real finding"


def test_post_findings_normalises_warning_to_warn(monkeypatch) -> None:
    """'WARNING' severity in a finding dict is normalised to 'WARN' in the payload."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=lambda *_, **__: None)

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = api
        post_findings(
            findings=[
                {
                    "dimension": "pipeline_consistency",
                    "severity": "WARNING",
                    "finding": "Something is off.",
                    "suggestion": None,
                }
            ],
            run_id="run-warn-norm",
            repo="test-repo",
            flow_name=None,
            source="conformance_check",
            standards_version="3.0.1",
        )

    assert len(posted) == 1
    assert posted[0]["severity"] == "WARN"


def test_post_findings_skips_duplicate_when_same_run_finding_severity_dimension(
    monkeypatch,
) -> None:
    """Latest stored row matches run_id + finding + severity + dimension — skip POST."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test")

    mock_post = MagicMock(return_value={})

    api = SimpleNamespace(
        post=mock_post,
        get=MagicMock(
            return_value={
                "data": [
                    {
                        "run_id": "run-dedup-1",
                        "dimension": "pipeline_consistency",
                        "severity": "SUCCESS",
                        "finding": "Run completed successfully.",
                    }
                ]
            }
        ),
    )

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = api
        post_findings(
            findings=[
                {
                    "dimension": "pipeline_consistency",
                    "severity": "SUCCESS",
                    "finding": "Run completed successfully.",
                    "suggestion": None,
                }
            ],
            run_id="run-dedup-1",
            repo="test-repo",
            flow_name="process-new-csv-files",
            source="flow_inline",
            standards_version="6.0.0",
        )

    mock_post.assert_not_called()


def test_post_findings_posts_when_same_text_but_different_run_id(monkeypatch) -> None:
    """Identical finding text as latest row but different run_id — POST once."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    mock_post = MagicMock(side_effect=_fake_post)

    api = SimpleNamespace(
        post=mock_post,
        get=MagicMock(
            return_value={
                "data": [
                    {
                        "run_id": "run-previous",
                        "dimension": "pipeline_consistency",
                        "severity": "SUCCESS",
                        "finding": "Run completed successfully.",
                    }
                ]
            }
        ),
    )

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = api
        post_findings(
            findings=[
                {
                    "dimension": "pipeline_consistency",
                    "severity": "SUCCESS",
                    "finding": "Run completed successfully.",
                    "suggestion": None,
                }
            ],
            run_id="run-new",
            repo="test-repo",
            flow_name="process-new-csv-files",
            source="flow_inline",
            standards_version="6.0.0",
        )

    mock_post.assert_called_once()
    assert len(posted) == 1
    assert posted[0]["run_id"] == "run-new"


def test_post_findings_respects_caller_source_with_direct_finding_text_kwarg(
    monkeypatch,
) -> None:
    """direct_finding_text kwarg does not override source — payload uses caller source."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=MagicMock(return_value=None))

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = api
        post_findings(
            findings=[
                {
                    "dimension": "pipeline_consistency",
                    "severity": "INFO",
                    "finding": "Direct body text.",
                    "suggestion": None,
                }
            ],
            run_id="run-src",
            repo="test-repo",
            flow_name=None,
            source="flow_inline",
            standards_version="6.0.0",
            direct_finding_text="ignored for payload; findings carry text",
        )

    assert len(posted) == 1
    assert posted[0]["source"] == "flow_inline"


def test_post_findings_handles_post_exception_gracefully(monkeypatch) -> None:
    """When api_client.post raises, the exception is caught, logged, and execution continues."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test")

    api = SimpleNamespace(
        post=MagicMock(side_effect=RuntimeError("connection refused")),
        get=lambda *_, **__: None,
    )

    with (
        patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m,
        patch("evaluator_cog.engine.api_client.log") as mock_log,
    ):
        m.from_env.return_value = api
        post_findings(
            findings=[
                {
                    "dimension": "structural_conformance",
                    "severity": "ERROR",
                    "finding": "Sentry missing.",
                    "suggestion": None,
                }
            ],
            run_id="run-post-fail",
            repo="test-repo",
            flow_name=None,
            source="conformance_check",
            standards_version="3.0.1",
        )

    api.post.assert_called_once()
    assert any(
        "failed to POST finding" in str(call.args)
        for call in mock_log.warning.call_args_list
    )
