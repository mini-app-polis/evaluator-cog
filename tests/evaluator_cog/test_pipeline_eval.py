"""Unit tests for pipeline_eval helpers."""

from types import SimpleNamespace
from unittest.mock import patch

from evaluator_cog.flows.pipeline_eval import (
    _flow_name_to_repo,
    _state_to_severity,
    evaluate_pipeline_run,
)


def test_flow_name_to_repo_known_flows() -> None:
    """Known flow names map to the correct repo."""
    assert _flow_name_to_repo("process-transcript") == "notes-ingest-cog"
    assert _flow_name_to_repo("update-dj-set-collection") == "deejay-cog"
    assert _flow_name_to_repo("generate-summaries") == "deejay-cog"
    assert _flow_name_to_repo("process-set") == "deejay-cog"


def test_flow_name_to_repo_unknown_returns_unknown() -> None:
    """Unknown flow names return 'unknown' rather than being silently misattributed."""
    assert _flow_name_to_repo("some-unknown-flow") == "unknown"
    assert _flow_name_to_repo("") == "unknown"


def test_evaluate_pipeline_run_no_kaiano_url_returns_early(monkeypatch) -> None:
    """evaluate_pipeline_run returns immediately when KAIANO_API_BASE_URL is unset."""
    monkeypatch.delenv("KAIANO_API_BASE_URL", raising=False)

    posted: list = []

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = SimpleNamespace(
            post=lambda *a, **_: posted.append(a),
            get=lambda *_, **__: None,
        )
        evaluate_pipeline_run(
            run_id="r-no-url",
            repo="deejay-cog",
            sets_imported=0,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=0,
            failed_set_labels=[],
            api_ingest_success=True,
        )

    assert posted == []


def test_evaluate_pipeline_run_direct_severity_warning_normalised(
    monkeypatch,
) -> None:
    """direct_severity='WARNING' is normalised to 'WARN' in the posted payload."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=lambda *_, **__: None)

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r-warn-norm",
            repo="deejay-cog",
            sets_imported=0,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=0,
            failed_set_labels=[],
            api_ingest_success=True,
            direct_finding_text="Something happened.",
            direct_severity="WARNING",
        )

    assert len(posted) == 1
    assert posted[0]["severity"] == "WARN"


def test_evaluate_pipeline_run_direct_severity_critical_passes_through(
    monkeypatch,
) -> None:
    """direct_severity='CRITICAL' is accepted and posted as CRITICAL."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=lambda *_, **__: None)

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r-critical-sev",
            repo="deejay-cog",
            sets_imported=0,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=0,
            failed_set_labels=[],
            api_ingest_success=True,
            direct_finding_text="Something happened.",
            direct_severity="CRITICAL",
        )

    assert len(posted) == 1
    assert posted[0]["severity"] == "CRITICAL"


def test_evaluate_pipeline_run_direct_severity_invalid_defaults_to_warn(
    monkeypatch,
) -> None:
    """An unrecognised direct_severity string falls back to 'WARN'."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=lambda *_, **__: None)

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m:
        m.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r-invalid-sev",
            repo="deejay-cog",
            sets_imported=0,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=0,
            failed_set_labels=[],
            api_ingest_success=True,
            direct_finding_text="Something happened.",
            direct_severity="NOT_A_CANONICAL_SEVERITY",
        )

    assert len(posted) == 1
    assert posted[0]["severity"] == "WARN"


def test_evaluate_pipeline_run_claude_exception_returns_without_posting(
    monkeypatch,
) -> None:
    """When the Claude call raises, evaluate_pipeline_run logs and returns without posting."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    posted: list = []
    api = SimpleNamespace(
        post=lambda *a, **_: posted.append(a),
        get=lambda *_, **__: None,
    )

    with (
        patch(
            "evaluator_cog.engine.llm._anthropic_messages_create",
            side_effect=RuntimeError("API timeout"),
        ),
        patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as m,
    ):
        m.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r-claude-fail",
            repo="deejay-cog",
            sets_imported=1,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=10,
            failed_set_labels=[],
            api_ingest_success=True,
            sets_attempted=1,
        )

    assert posted == []


def test_state_to_severity_completed_returns_info() -> None:
    """CRASHED/FAILED/CANCELLED map to CRITICAL/ERROR/WARN; other states map to INFO."""
    assert _state_to_severity("CRASHED") == "CRITICAL"
    assert _state_to_severity("FAILED") == "ERROR"
    assert _state_to_severity("CANCELLED") == "WARN"
    assert _state_to_severity("COMPLETED") == "INFO"
    assert _state_to_severity("RUNNING") == "INFO"
    assert _state_to_severity("SCHEDULED") == "INFO"
    assert _state_to_severity("") == "INFO"
