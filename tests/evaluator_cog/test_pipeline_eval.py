"""Unit tests for pipeline_eval helpers."""

from evaluator_cog.flows.pipeline_eval import _flow_name_to_repo


def test_flow_name_to_repo_known_flows() -> None:
    """Known flow names map to the correct repo."""
    assert _flow_name_to_repo("process-transcript") == "notes-ingest-cog"
    assert _flow_name_to_repo("update-dj-set-collection") == "deejay-cog"
    assert _flow_name_to_repo("generate-summaries") == "deejay-cog"
    assert _flow_name_to_repo("process-set") == "deejay-cog"


def test_flow_name_to_repo_unknown_falls_back() -> None:
    """Unknown flow names fall back to deejay-cog."""
    assert _flow_name_to_repo("some-unknown-flow") == "deejay-cog"
    assert _flow_name_to_repo("") == "deejay-cog"
