"""Every Discord message says which evaluator ran, against which catalog.

The library stamps ``(processor=X.Y.Z)`` from the installed distribution's
metadata, and the Lambda deploy strips ``*.dist-info`` — so in production it
stamps nothing. These tests simulate that by making the library's lookup
return ``None``.
"""

from unittest.mock import patch

import mini_app_polis.pipeline_status as ps
import pytest

import evaluator_cog.adapters.queue as q
import evaluator_cog.flows.conformance as conf
from evaluator_cog import __version__


@pytest.fixture
def sent(monkeypatch):
    """Messages as they would be delivered, with no distribution metadata."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.test")
    messages: list[dict] = []

    def _capture(message, **_kwargs) -> bool:
        messages.append(message)
        return True

    with (
        patch.object(ps, "_resolve_processor_version", return_value=None),
        patch.object(ps, "_deliver", _capture),
    ):
        yield messages


def _description(message: dict) -> str:
    return message["embeds"][0]["description"]


def test_stamp_carries_processor_and_standards() -> None:
    assert conf.stamp_versions("done", "4.2.0") == (
        f"done\n(processor={__version__}, standards=4.2.0)"
    )


def test_stamp_omits_an_unknown_standards_version() -> None:
    assert conf.stamp_versions("done") == f"done\n(processor={__version__})"


def test_stamp_leaves_empty_and_stamped_text_alone() -> None:
    assert conf.stamp_versions("") == ""
    assert conf.stamp_versions("x (processor=1.0.0)", "4.2.0") == (
        "x (processor=1.0.0)"
    )


def test_run_report_carries_the_graded_catalog_version(sent) -> None:
    ctx = conf.RunContext.for_run("deterministic-conformance")
    conf._record_standards_version("4.2.0", ctx=ctx)
    assert ctx.report is not None
    ctx.report.ok()
    ctx.report.count("posted", 3)
    ctx.report.send(notable=True)

    lines = _description(sent[0]).splitlines()
    assert lines[0].startswith("Run complete in ")
    # Counters follow the stamp, so the last line is the metadata.
    assert lines[-1] == f"(processor={__version__}, standards=4.2.0) posted=3"
    assert _description(sent[0]).count("(processor=") == 1


def test_handler_records_the_version_it_grades_against() -> None:
    """Resolved in handler, so the report cannot name a different one."""
    ctx = conf.RunContext.for_run()
    event = conf.EvaluationEvent(
        org="mini-app-polis",
        repo="watcher-cog",
        ref="main",
        services=({"id": "watcher-cog", "repo": "watcher-cog"},),
        run_id="deterministic-4.2.0-x",
        mode="deterministic",
        standards_version="4.2.0",
    )
    with (
        patch.object(conf, "_fetch_catalog_schema", side_effect=RuntimeError("stop")),
        pytest.raises(RuntimeError, match="stop"),
    ):
        conf.handler(event, log=None, ctx=ctx)

    assert isinstance(ctx.report, conf.VersionedRunReport)
    assert ctx.report.standards_version == "4.2.0"


def test_a_failed_job_still_says_which_evaluator_failed(sent) -> None:
    q._report_failure("a queued evaluation", RuntimeError("boom"))

    assert _description(sent[0]) == (
        f"a queued evaluation failed: RuntimeError: boom\n(processor={__version__})"
    )
