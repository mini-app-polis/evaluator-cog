"""Tests for engine/api_client.py — dedup fetch response shape variants."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
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
    result = _get_latest_stored_finding(api_client=client, repo="my-repo")
    assert result is not None
    assert result["finding"] == "old finding"


def test_get_latest_items_list_shape() -> None:
    """{'items': [{...}]} response shape returns the first item."""
    client = _make_client(
        {"items": [{"finding": "items finding", "severity": "INFO", "dimension": "x"}]}
    )
    result = _get_latest_stored_finding(api_client=client, repo="my-repo")
    assert result is not None
    assert result["finding"] == "items finding"


def test_get_latest_bare_list_shape() -> None:
    """A bare list response returns the first item."""
    client = _make_client(
        [{"finding": "list finding", "severity": "ERROR", "dimension": "x"}]
    )
    result = _get_latest_stored_finding(api_client=client, repo="my-repo")
    assert result is not None
    assert result["finding"] == "list finding"


def test_get_latest_empty_data_list_returns_none() -> None:
    """{'data': []} — empty list — returns None."""
    client = _make_client({"data": []})
    result = _get_latest_stored_finding(api_client=client, repo="my-repo")
    assert result is None


def test_get_latest_returns_none_on_exception() -> None:
    """Any exception in the fetch returns None rather than raising."""
    client = SimpleNamespace(get=MagicMock(side_effect=RuntimeError("network error")))
    result = _get_latest_stored_finding(api_client=client, repo="my-repo")
    assert result is None


@respx.mock
def test_get_latest_makes_no_unauthenticated_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client without .get must fail closed, not fall back to bare httpx.

    The removed fallback built an httpx.Client against
    KAIANO_API_BASE_URL and sent no credential, so the read was
    unattributable — CD-019's own violation, in the evaluator. The
    respx mock here registers no routes, so any outbound request raises
    and fails this test; the helper must swallow the AttributeError and
    return None instead of reaching the network another way.
    """
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://fallback-api.test")
    client_without_get = SimpleNamespace()

    result = _get_latest_stored_finding(
        api_client=client_without_get,
        repo="my-repo",
    )
    assert result is None


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
            source="conformance_llm",
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
            source="conformance_llm",
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
            source="conformance_llm",
            standards_version="3.0.1",
        )

    api.post.assert_called_once()
    assert any(
        "failed to POST finding" in str(call.args)
        for call in mock_log.warning.call_args_list
    )


# ---------------------------------------------------------------------------
# post_findings — the API's own idempotency guard (PIPE-002)
# ---------------------------------------------------------------------------
#
# A suppressed write answers 200. Counting that as a delivered finding is
# how a run comes to report findings as posted that were never stored —
# the September failure shape, reached by a new route.


def _deduplicated_envelope(deduplicated: bool) -> dict:
    """What /v1/evaluations answers after the idempotency guard."""
    return {
        "data": {
            "id": "00000000-0000-0000-0000-000000000001",
            "repo": "test-repo",
            "finding": "a finding",
            "deduplicated": deduplicated,
        },
        "meta": {"count": 1, "total": 1, "version": "v1"},
    }


def _one_finding() -> list[dict]:
    return [
        {
            "violation_id": "CD-026",
            "dimension": "cd_readiness",
            "severity": "ERROR",
            "finding": "a finding",
            "suggestion": "a fix",
        }
    ]


def _post_one(monkeypatch, response: dict):
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test")
    api = SimpleNamespace(
        post=MagicMock(return_value=response),
        get=MagicMock(return_value={"data": []}),
    )
    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = api
        return post_findings(
            findings=_one_finding(),
            run_id="deterministic-7.0.0-abc",
            repo="test-repo",
            flow_name="deterministic-conformance",
            source="conformance_deterministic",
            standards_version="7.0.0",
        )


def test_a_server_side_duplicate_is_not_counted_as_posted(monkeypatch) -> None:
    """The row was not stored by this request, so it was not posted."""
    result = _post_one(monkeypatch, _deduplicated_envelope(True))

    assert result.posted == 0
    assert result.duplicates == 1
    assert result.failed == 0
    # Offered and declined is not a delivery failure — the route works.
    assert result.total_failure is False
    assert result.duplicate_details, "a suppressed finding recorded no detail"
    assert "already stored" in result.duplicate_details[0]


def test_a_stored_finding_is_counted_as_posted(monkeypatch) -> None:
    result = _post_one(monkeypatch, _deduplicated_envelope(False))

    assert result.posted == 1
    assert result.duplicates == 0


def test_an_api_without_the_flag_is_read_as_having_stored_the_row(
    monkeypatch,
) -> None:
    """Absent means stored, which is the safe reading.

    An API from before the idempotency guard does not send the field and
    did write the row. Reading a missing flag as "suppressed" would under-
    report every delivered finding against it.
    """
    for response in ({}, {"data": {}}, {"data": None}, None):
        result = _post_one(monkeypatch, response)
        assert result.posted == 1, f"{response!r} was not read as a stored row"
        assert result.duplicates == 0


def test_offered_counts_both_kinds_of_duplicate() -> None:
    """The denominator a log line should use.

    A finding suppressed before it was offered never reached `attempted`;
    one suppressed by the API did. Adding `duplicates` to `attempted` would
    double-count the second kind.
    """
    from evaluator_cog.engine.api_client import PostResult

    # Two stored, one suppressed by the API, one failed: all four offered.
    server_side = PostResult(attempted=4, posted=2, duplicates=1, failed=1)
    assert server_side.offered == 4

    # One suppressed client-side, never offered to the API.
    client_side = PostResult(attempted=1, posted=1, duplicates=1)
    assert client_side.offered == 2
