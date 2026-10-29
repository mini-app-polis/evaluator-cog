"""Every Discord message says which evaluator ran, against which catalog.

The library stamps ``(version=X.Y.Z)`` from the ``__version__`` the
evaluator passes, from its own ``_version.py``, so nothing depends on
installed package metadata, which the Lambda deploy strips. The catalog
version is the evaluator's own: ``(standards=...)`` on a line of its own.
"""

from unittest.mock import patch

import mini_app_polis.pipeline_status as ps
import pytest

import evaluator_cog.adapters.queue as q
import evaluator_cog.flows.conformance as conf
from evaluator_cog import __version__


@pytest.fixture
def sent(monkeypatch):
    """Messages as they would be delivered."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.test")
    messages: list[dict] = []

    def _capture(message, **_kwargs) -> bool:
        messages.append(message)
        return True

    with patch.object(ps, "_deliver", _capture):
        yield messages


def _description(message: dict) -> str:
    return message["embeds"][0]["description"]


def test_stamp_carries_the_standards_version() -> None:
    assert conf.stamp_standards("done", "4.2.0") == "done (standards=4.2.0)"
    assert conf.stamp_standards("done\n\nCounts\n  3 posted", "4.2.0") == (
        "done (standards=4.2.0)\n\nCounts\n  3 posted"
    )


def test_stamp_omits_an_unknown_standards_version() -> None:
    assert conf.stamp_standards("done") == "done"


def test_stamp_leaves_empty_and_stamped_text_alone() -> None:
    assert conf.stamp_standards("", "4.2.0") == ""
    assert conf.stamp_standards("x (standards=1.0.0)", "4.2.0") == (
        "x (standards=1.0.0)"
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
    # The headline carries the metadata: the catalog, then the build.
    assert lines[0].endswith(f"(standards=4.2.0) (version={__version__})")
    assert lines[-2:] == ["Counts", "  3 posted"]
    assert _description(sent[0]).count("(version=") == 1


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
        f"a queued evaluation failed: RuntimeError: boom (version={__version__})"
    )
