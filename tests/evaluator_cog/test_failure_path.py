"""Failure-path tests: errors in one item must not abort a multi-item run."""

from unittest.mock import MagicMock, patch

import evaluator_cog.flows.pipeline_eval as pe
from evaluator_cog.flows.pipeline_eval import handle_prefect_flow_run_events


def _completed_payload(flow_run_id: str) -> dict:
    return {
        "flow_run_id": flow_run_id,
        "flow_name": "process-new-csv-files",
        "state_name": "Completed",
        "state_type": "COMPLETED",
        "start_time": "2026-03-19T10:00:00Z",
        "end_time": "2026-03-19T10:01:00Z",
    }


def test_evaluate_pipeline_run_failure_in_loop_logs_and_continues() -> None:
    """First evaluation raises; second runs — loop does not abort after the error."""
    payloads = [_completed_payload("run-first"), _completed_payload("run-second")]

    mock_eval = MagicMock(side_effect=[RuntimeError("boom"), None])

    with (
        patch.object(pe, "evaluate_pipeline_run", mock_eval),
        patch.object(pe.log, "exception") as mock_log_exception,
    ):
        handle_prefect_flow_run_events(payloads)

    assert mock_eval.call_count == 2
    assert mock_eval.call_args_list[0].kwargs["run_id"] == "run-first"
    assert mock_eval.call_args_list[1].kwargs["run_id"] == "run-second"
    mock_log_exception.assert_called_once()
