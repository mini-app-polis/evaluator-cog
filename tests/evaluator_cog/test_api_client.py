"""Tests for engine/api_client.py — dedup fetch response shape variants."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from evaluator_cog.engine.api_client import post_findings

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
