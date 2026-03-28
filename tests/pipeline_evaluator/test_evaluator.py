import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pipeline_evaluator.evaluator as pe
from pipeline_evaluator.evaluator import (
    build_collection_evaluation_prompt,
    build_csv_evaluation_prompt,
    evaluate_pipeline_run,
)


def test_collection_update_prompt_is_collection_specific() -> None:
    prompt = build_collection_evaluation_prompt(
        run_id="23312071243",
        standards_version="6.0",
        folders_processed=3,
        tabs_written=2,
        total_sets=12,
        json_snapshot_written=True,
        folder_names=["2024", "2023", "Summary"],
    )
    assert "COLLECTION_UPDATE evaluation context" in prompt
    assert "No CSV processing happened in this run" in prompt
    assert "pipeline_consistency" in prompt
    assert "23312071243" in prompt
    assert "folders_processed: 3" in prompt
    assert "tabs_written: 2" in prompt
    assert "total_sets: 12" in prompt
    assert "json_snapshot_written: True" in prompt
    assert "folder_names: 2024, 2023, Summary" in prompt


def test_collection_prompt_warns_on_zero_tabs() -> None:
    prompt = build_collection_evaluation_prompt(
        run_id="r-zero-tabs",
        standards_version="6.0",
        folders_processed=3,
        tabs_written=0,
        total_sets=9,
        json_snapshot_written=True,
        folder_names=["2024", "2023"],
    )
    assert "If tabs_written == 0 and folders_processed > 0: emit WARN" in prompt
    assert "No tabs written despite N folders processed" in prompt


def test_collection_prompt_errors_on_missing_snapshot() -> None:
    prompt = build_collection_evaluation_prompt(
        run_id="r-snapshot-fail",
        standards_version="6.0",
        folders_processed=3,
        tabs_written=2,
        total_sets=9,
        json_snapshot_written=False,
        folder_names=["2024", "2023"],
    )
    assert "If json_snapshot_written is False: emit ERROR" in prompt
    assert "JSON snapshot write failed" in prompt


def test_collection_prompt_warns_on_missing_current_year() -> None:
    prompt = build_collection_evaluation_prompt(
        run_id="r-missing-year",
        standards_version="6.0",
        folders_processed=2,
        tabs_written=2,
        total_sets=5,
        json_snapshot_written=True,
        folder_names=["2022", "2023"],
    )
    assert "If folder_names does not include current_year: emit WARN" in prompt
    assert "Current year folder missing" in prompt
    assert f"current_year: {datetime.now().year}" in prompt


def test_csv_processing_prompt_is_csv_specific() -> None:
    prompt = build_csv_evaluation_prompt(
        run_id="run-abc",
        standards_version="6.0",
        sets_imported=2,
        sets_failed=1,
        sets_skipped=3,
        total_tracks=120,
        failed_set_labels=["2024-01-01 Bad", "2024-02-02 Worse"],
        api_ingest_success=False,
        sets_attempted=5,
        unrecognized_filename_skips=2,
        duplicate_csv_count=4,
    )
    assert "CSV PROCESSING evaluation context" in prompt
    assert "run-abc" in prompt
    assert "sets_imported: successfully processed CSVs" in prompt and "(2)" in prompt
    assert "sets_failed: CSVs renamed with FAILED_ prefix (1)" in prompt
    assert "sets_skipped: non-CSV files moved out of the source folder (3)" in prompt
    assert "total_tracks: total track rows" in prompt and "(120)" in prompt
    assert "2024-01-01 Bad" in prompt
    assert "api_ingest_success: all API ingest attempts succeeded" in prompt
    assert (
        "unrecognized_filename_skips: files skipped due to filename format (2)"
        in prompt
    )
    assert (
        "possible_duplicate_csv: CSVs renamed as possible_duplicate_ and not uploaded (4)"
        in prompt
    )
    assert "COLLECTION_UPDATE evaluation context" not in prompt


def test_evaluate_pipeline_run_uses_csv_prompt_when_collection_update_false(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")
    prompts: list[str] = []

    def _capture(**kw: object) -> str:
        prompts.append(str(kw["user_prompt"]))
        return '{"findings":[]}'

    api = SimpleNamespace(post=MagicMock(return_value={}))

    with (
        patch.object(pe, "_anthropic_messages_create", side_effect=_capture),
        patch("common_python_utils.api.CommonPythonApiClient") as m_client,
    ):
        m_client.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r1",
            repo="deejay-set-processor-dev",
            sets_imported=1,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=1,
            failed_set_labels=[],
            api_ingest_success=True,
            sets_attempted=1,
            collection_update=False,
        )

    assert prompts and "CSV PROCESSING evaluation context" in prompts[0]


def test_evaluate_pipeline_run_uses_collection_prompt_when_collection_update_true(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")
    prompts: list[str] = []

    def _capture(**kw: object) -> str:
        prompts.append(str(kw["user_prompt"]))
        return '{"findings":[]}'

    api = SimpleNamespace(post=MagicMock(return_value={}))

    with (
        patch.object(pe, "_anthropic_messages_create", side_effect=_capture),
        patch("common_python_utils.api.CommonPythonApiClient") as m_client,
    ):
        m_client.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r2",
            repo="deejay-set-processor-dev",
            sets_imported=0,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=0,
            failed_set_labels=[],
            api_ingest_success=True,
            sets_attempted=0,
            collection_update=True,
            folders_processed=3,
            tabs_written=2,
            total_sets=6,
            json_snapshot_written=True,
            folder_names=["2024", "2023", "Summary"],
        )

    assert prompts and "COLLECTION_UPDATE evaluation context" in prompts[0]


def test_evaluate_pipeline_run_posts_findings(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")

    payload = {
        "findings": [
            {
                "dimension": "pipeline_consistency",
                "severity": "INFO",
                "finding": "ok",
                "suggestion": "",
            }
        ]
    }
    posted: list[tuple[str, dict]] = []

    def _post(path: str, p: dict) -> dict:
        posted.append((path, p))
        return {}

    api = SimpleNamespace(post=_post)

    with (
        patch.object(
            pe,
            "_anthropic_messages_create",
            return_value=json.dumps(payload),
        ),
        patch("common_python_utils.api.CommonPythonApiClient") as m_client,
    ):
        m_client.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r3",
            repo="deejay-set-processor-dev",
            flow_name="test-flow",
            sets_imported=0,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=0,
            failed_set_labels=[],
            api_ingest_success=True,
            sets_attempted=0,
        )

    assert len(posted) == 1
    path, body = posted[0]
    assert path == "/v1/evaluations"
    assert body["repo"] == "deejay-set-processor-dev"
    assert body["severity"] == "INFO"
    assert body["run_id"] == "r3"
    assert body["flow_name"] == "test-flow"
    assert body["finding"] == "ok"
    assert body["source"] == "flow_inline"


def test_evaluate_pipeline_run_flow_name_defaults_to_none(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")

    payload = {
        "findings": [
            {
                "dimension": "pipeline_consistency",
                "severity": "INFO",
                "finding": "ok",
                "suggestion": "",
            }
        ]
    }
    posted: list[tuple[str, dict]] = []

    def _post(path: str, p: dict) -> dict:
        posted.append((path, p))
        return {}

    api = SimpleNamespace(post=_post)

    with (
        patch.object(
            pe,
            "_anthropic_messages_create",
            return_value=json.dumps(payload),
        ),
        patch("common_python_utils.api.CommonPythonApiClient") as m_client,
    ):
        m_client.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r3-default-flow",
            repo="deejay-set-processor-dev",
            sets_imported=0,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=0,
            failed_set_labels=[],
            api_ingest_success=True,
            sets_attempted=0,
        )

    assert len(posted) == 1
    _, body = posted[0]
    assert body["flow_name"] is None


def test_evaluate_pipeline_run_uses_flow_hook_source(monkeypatch) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")

    posted: list[tuple[str, dict]] = []

    def _post(path: str, p: dict) -> dict:
        posted.append((path, p))
        return {}

    api = SimpleNamespace(post=_post)

    with patch("common_python_utils.api.CommonPythonApiClient") as m_client:
        m_client.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r-flow-hook",
            repo="deejay-set-processor-dev",
            sets_imported=0,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=0,
            failed_set_labels=[],
            api_ingest_success=True,
            sets_attempted=0,
            direct_finding_text="Flow process-new-csv-files entered Failed state",
            direct_severity="WARN",
        )

    assert len(posted) == 1
    _, body = posted[0]
    assert body["source"] == "flow_hook"


def test_evaluate_pipeline_run_skips_duplicate_finding(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://x")

    payload = {
        "findings": [
            {
                "dimension": "pipeline_consistency",
                "severity": "WARN",
                "finding": "duplicate finding text",
                "suggestion": "",
            }
        ]
    }

    api = SimpleNamespace(
        get=MagicMock(
            return_value={
                "data": [
                    {
                        "dimension": "pipeline_consistency",
                        "severity": "WARN",
                        "finding": "duplicate finding text",
                    }
                ]
            }
        ),
        post=MagicMock(return_value={}),
    )

    with (
        patch.object(
            pe,
            "_anthropic_messages_create",
            return_value=json.dumps(payload),
        ),
        patch("common_python_utils.api.CommonPythonApiClient") as m_client,
        patch.object(pe.log, "info") as mock_info,
    ):
        m_client.from_env.return_value = api
        evaluate_pipeline_run(
            run_id="r-dup",
            repo="deejay-set-processor-dev",
            sets_imported=0,
            sets_failed=0,
            sets_skipped=0,
            total_tracks=0,
            failed_set_labels=[],
            api_ingest_success=True,
            sets_attempted=0,
        )

    api.post.assert_not_called()
    assert any(
        call.args and "⏭️ Skipping duplicate finding:" in str(call.args[0])
        for call in mock_info.call_args_list
    )
