"""A run that delivers nothing must not report success.

On 2026-09-03 two conformance runs computed roughly 162 findings across
13 repos and delivered none of them: KAIANO_API_BASE_URL was not set on
the service, so every request failed inside httpx before a packet left
the process. Every instrument said the run was healthy —

  - post_findings caught each exception, set a local `evaluator_failed`
    flag, wrote it to one log line and discarded it;
  - the flow logged "EVAL-003: posted 1 findings" and two more like it,
    because that line reported len(findings_handed_over) rather than
    what the API accepted;
  - the flow finished Completed, so no failure hook fired and
    _on_completion pinged Healthchecks.io green.

Three independent signals reporting success during a total delivery
failure. These tests pin the corrected behaviour.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from evaluator_cog.engine import api_client
from evaluator_cog.engine.api_client import PostResult, post_findings
from evaluator_cog.flows import conformance as conf


def _finding(text: str = "a finding") -> dict:
    return {
        "rule_id": "SEC-002",
        "violation_id": "SEC-002",
        "severity": "ERROR",
        "dimension": "security_posture",
        "finding": text,
        "suggestion": "x" * 50,
    }


# --- PostResult ---------------------------------------------------------


def test_total_failure_is_offered_work_that_landed_nowhere() -> None:
    assert PostResult(attempted=5, posted=0, failed=5).total_failure is True
    assert PostResult(attempted=5, posted=1, failed=4).total_failure is False
    # Nothing offered is not a failure — a clean repo posts nothing.
    assert PostResult().total_failure is False


# --- post_findings reports outcomes -------------------------------------


def _client(post_side_effect=None):
    client = MagicMock()
    client.get.return_value = {"data": []}
    if post_side_effect is not None:
        client.post.side_effect = post_side_effect
    return client


def test_post_findings_reports_what_the_api_accepted() -> None:
    client = _client()
    with patch.object(
        api_client.CommonPythonApiClient, "from_env", return_value=client
    ):
        result = post_findings(
            findings=[_finding("one"), _finding("two")],
            run_id="r1",
            repo="evaluator-cog",
            flow_name="f",
            source="conformance_deterministic",
            standards_version="6.4.1",
        )
    assert (result.attempted, result.posted, result.failed) == (2, 2, 0)
    assert result.total_failure is False


def test_post_findings_reports_a_total_failure_without_raising() -> None:
    """Still never raises — the caller must not lose computed findings."""
    client = _client(post_side_effect=RuntimeError("Connection failed"))
    with patch.object(
        api_client.CommonPythonApiClient, "from_env", return_value=client
    ):
        result = post_findings(
            findings=[_finding("one"), _finding("two")],
            run_id="r1",
            repo="evaluator-cog",
            flow_name="f",
            source="conformance_deterministic",
            standards_version="6.4.1",
        )
    assert (result.attempted, result.posted, result.failed) == (2, 0, 2)
    assert result.total_failure is True
    assert "Connection failed" in result.last_error


def test_post_findings_reports_a_partial_failure() -> None:
    client = _client(post_side_effect=[{"ok": True}, RuntimeError("boom")])
    with patch.object(
        api_client.CommonPythonApiClient, "from_env", return_value=client
    ):
        result = post_findings(
            findings=[_finding("one"), _finding("two")],
            run_id="r1",
            repo="evaluator-cog",
            flow_name="f",
            source="conformance_deterministic",
            standards_version="6.4.1",
        )
    assert (result.posted, result.failed) == (1, 1)
    # A partial failure is not total — one landing proves the route works.
    assert result.total_failure is False


# --- the flow refuses to call a delivery failure a success --------------


def test_delivery_assertion_raises_when_nothing_landed() -> None:
    ctx = conf.RunContext.for_run()
    ctx.tally.merge(
        PostResult(attempted=162, posted=0, failed=162, errors=["Connection failed"])
    )
    with pytest.raises(conf.FindingDeliveryError) as excinfo:
        conf._assert_findings_were_delivered(MagicMock(), ctx=ctx)
    message = str(excinfo.value)
    assert "162" in message
    # The message must name the two variables an operator should check.
    assert "KAIANO_API_BASE_URL" in message
    assert "EVALUATOR_COG_API_KEY" in message


def test_delivery_assertion_is_silent_on_a_partial_failure() -> None:
    """One landing proves the route works; the rest are warned about.

    The assertion that matters is total_failure: nine findings reached
    the API, so the route is up and the run must not be failed. The log
    is a stand-in here — the per-emitter warning is asserted in
    test_post_tracked_logs_what_landed_not_what_was_handed_over.
    """
    ctx = conf.RunContext.for_run()
    ctx.tally.merge(PostResult(attempted=10, posted=9, failed=1))
    prefect_log = MagicMock()

    conf._assert_findings_were_delivered(prefect_log, ctx=ctx)

    # "Silent" is the claim in the name, so assert it: this function says
    # nothing on a partial failure. The per-emitter warning is raised by
    # _post_tracked, not here.
    assert not prefect_log.warning.called
    assert not prefect_log.error.called
    assert ctx.tally.total_failure is False
    assert ctx.tally.posted == 9
    assert ctx.tally.attempted == 10


def test_delivery_assertion_is_silent_when_nothing_was_offered() -> None:
    """A wholly conformant fleet posts nothing and that is not a failure.

    Nothing attempted is not the same as nothing delivered, and only the
    second is a failure. This pins that distinction: an empty tally
    leaves total_failure False and the run succeeds.
    """
    ctx = conf.RunContext.for_run()
    prefect_log = MagicMock()

    conf._assert_findings_were_delivered(prefect_log, ctx=ctx)

    assert not prefect_log.warning.called
    assert not prefect_log.error.called
    assert ctx.tally.attempted == 0
    assert ctx.tally.total_failure is False


def test_post_tracked_logs_what_landed_not_what_was_handed_over() -> None:
    """The regression test for the false 'posted N findings' log line."""
    ctx = conf.RunContext.for_run()
    prefect_log = MagicMock()
    with patch.object(
        conf,
        "post_findings",
        return_value=PostResult(attempted=3, posted=0, failed=3, errors=["nope"]),
    ):
        conf._post_tracked("EVAL-003", prefect_log, ctx=ctx, findings=[1, 2, 3])

    # It must NOT have claimed a successful post.
    assert not prefect_log.info.called, (
        "logged a success line for findings that never reached the API"
    )
    assert prefect_log.warning.called
    assert ctx.tally.total_failure is True


def test_post_tracked_accumulates_across_emitters() -> None:
    ctx = conf.RunContext.for_run()
    prefect_log = MagicMock()
    for res in (
        PostResult(attempted=2, posted=2),
        PostResult(attempted=3, posted=0, failed=3, errors=["x"]),
    ):
        with patch.object(conf, "post_findings", return_value=res):
            conf._post_tracked("repo", prefect_log, ctx=ctx, findings=[])
    assert ctx.tally.attempted == 5
    assert ctx.tally.posted == 2
    assert ctx.tally.failed == 3
    # Two landed, so this is partial, not total — the run still passes.
    assert ctx.tally.total_failure is False


def test_two_evaluations_in_one_process_keep_separate_tallies() -> None:
    """The condition for removing the lock: concurrency without cross-talk.

    Two runs post a different number of findings and flag a different
    repo, interleaved on a barrier so neither finishes before the other
    has started. Each reads back only its own counters.

    Against the module globals this could not have passed. The tally, the
    report and the flagged set were one object shared by every run in the
    process, and the reset that started a run wiped whatever a run
    already in flight had accumulated.
    """
    counts = {"alpha": 2, "beta": 5}
    both_posted = threading.Barrier(2)
    contexts: dict[str, conf.RunContext] = {}

    def fake_post(**kwargs):
        n = counts[kwargs["repo"]]
        return PostResult(attempted=n, posted=n)

    def run(name: str) -> None:
        ctx = conf.RunContext.for_run()
        contexts[name] = ctx
        conf._post_tracked(name, MagicMock(), ctx=ctx, findings=[], repo=name)
        both_posted.wait(timeout=5)
        conf._report_issue("repo_download_failed", f"{name}-cog", ctx=ctx)

    with patch.object(conf, "post_findings", side_effect=fake_post):
        threads = [threading.Thread(target=run, args=(n,)) for n in counts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), "a run never finished"

    assert contexts["alpha"].tally.posted == 2
    assert contexts["beta"].tally.posted == 5
    assert contexts["alpha"].flagged == {"alpha-cog"}
    assert contexts["beta"].flagged == {"beta-cog"}
    assert contexts["alpha"].report is not contexts["beta"].report


def test_every_post_site_in_the_flow_reports_into_the_run_log() -> None:
    """No emitter may go quiet in the window an operator watches.

    `_post_tracked` falls back to the shared-library logger when no run
    logger is passed. That logger reaches the service's stdout but not
    the Prefect run view, so a call site that omits it disappears from
    the log while the ones around it keep reporting — which is exactly
    what happened: eleven standalone repos posted silently while the two
    monorepo apps and every introspection rule reported normally.

    Asserted against the source rather than by running the flow, because
    the failure is a missing argument at a call site and a behavioural
    test would need to reach all eight of them.

    The run context needs no equivalent test: it is keyword-only and has
    no default, so a call site that forgets it is a TypeError rather than
    a silent write into another run's accounting.
    """
    import ast
    import inspect

    from evaluator_cog.flows import conformance as conf

    tree = ast.parse(inspect.getsource(conf))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "_post_tracked":
            continue
        # (label, prefect_log) are the two positional parameters.
        if len(node.args) < 2:
            missing.append(node.lineno)

    assert not missing, (
        f"_post_tracked called without a run logger at line(s) {missing} — "
        "those findings would post without saying so in the Prefect run log"
    )


def test_suppressed_duplicates_are_reported_in_the_run_view() -> None:
    """A repo whose only finding is dropped must not log nothing.

    deejay-cog and evaluator-cog each computed exactly one finding in the
    6.9.1 run. Both were dropped as duplicates, so posted was 0 and
    failed was 0, and _post_tracked logged neither — the repos produced
    no line at all and read as though they had never been processed. The
    finding was real, current, and silently discarded.
    """
    result = conf.PostResult()
    result.duplicates = 1
    result.duplicate_details = ["CD-021 for deejay-cog matched the stored row"]

    prefect_log = MagicMock()
    with patch.object(conf, "post_findings", return_value=result):
        conf._post_tracked(
            "deejay-cog",
            prefect_log,
            ctx=conf.RunContext.for_run(),
            findings=[{}],
            repo="deejay-cog",
        )

    assert prefect_log.warning.called, "a suppressed finding logged nothing"
    message = " ".join(str(c) for c in prefect_log.warning.call_args[0])
    assert "duplicate" in message.lower()
    assert "deejay-cog" in " ".join(
        str(a) for a in prefect_log.warning.call_args[0]
    ) or "deejay-cog" in str(prefect_log.warning.call_args)


# --- coverage, as opposed to delivery ------------------------------------
#
# ctx.tally answers "did the findings reach the API". These pin the other
# question: was every declared repo actually looked at, and looked at
# completely. A run can be perfect on the first and wrong on the second.


def test_repo_whose_checks_raise_is_named_in_the_run_report() -> None:
    """A repo evaluated with zero deterministic checks is not a clean repo."""
    ctx = conf.RunContext.for_run()
    conf._report_issue(
        "deterministic_checks_failed",
        "watcher-cog",
        RuntimeError("catalog fetch"),
        ctx=ctx,
    )

    assert ctx.report is not None
    assert ctx.report.severity == "WARN"
    text = ctx.report.text()
    assert "deterministic_checks_failed" in text
    assert "watcher-cog" in text
    assert "RuntimeError" in text


def test_missing_api_key_counts_without_escalating() -> None:
    """One configuration fact must not turn every LLM run WARN."""
    ctx = conf.RunContext.for_run()
    for repo in ("a-cog", "b-cog", "c-cog"):
        conf._report_note("llm_skipped_no_api_key", repo, ctx=ctx)

    assert ctx.report is not None
    assert ctx.report.severity == "SUCCESS"
    assert "llm_skipped_no_api_key=3" in ctx.report.text()


def test_helpers_are_noops_on_a_context_with_no_report() -> None:
    """A context with no report to send writes nowhere rather than raising.

    Tests and one-off scripts build one of these. Nothing is going to
    read the report, so nothing should be written into it.
    """
    ctx = conf.RunContext()
    conf._report_issue("repo_download_failed", "x-cog", RuntimeError("boom"), ctx=ctx)
    conf._report_note("llm_skipped_no_api_key", "x-cog", ctx=ctx)
    assert ctx.report is None
    assert ctx.flagged == set()


def test_a_repo_flagged_twice_is_still_one_repo_missing() -> None:
    """The clean count is derived, not incremented, so it cannot drift.

    A repo whose deterministic checks raise and whose LLM pass then fails
    is one repo that did not come through whole, not two.
    """
    ctx = conf.RunContext.for_run()
    conf._report_issue(
        "deterministic_checks_failed", "watcher-cog", RuntimeError("a"), ctx=ctx
    )
    conf._report_issue(
        "llm_assessment_failed", "watcher-cog", RuntimeError("b"), ctx=ctx
    )

    active = [{"id": "watcher-cog"}, {"id": "deejay-cog"}, {"id": "retag-cog"}]
    clean = sum(1 for s in active if s.get("id") and s["id"] not in ctx.flagged)
    assert clean == 2


def test_a_note_does_not_cost_a_repo_its_clean_count() -> None:
    """A skipped LLM pass is not a repo that went unevaluated."""
    ctx = conf.RunContext.for_run()
    conf._report_note("llm_skipped_no_api_key", "deejay-cog", ctx=ctx)

    assert "deejay-cog" not in ctx.flagged


def test_a_fresh_context_starts_a_fresh_coverage_report() -> None:
    """A run's coverage must not inherit the previous run's issues."""
    ctx = conf.RunContext.for_run()
    conf._report_issue("repo_download_failed", "x-cog", RuntimeError("boom"), ctx=ctx)
    assert ctx.report is not None
    assert ctx.report.severity == "WARN"

    fresh = conf.RunContext.for_run()
    assert fresh.report is not None
    assert fresh.report.severity == "SUCCESS"
    # The headline now carries how long the run took, so this asserts the
    # shape rather than the exact string — the duration is real elapsed
    # time and cannot be pinned here.
    # First line: the last one is the version stamp.
    headline = fresh.report.text().splitlines()[0]
    assert headline.endswith("— nothing to do.")
    assert headline.startswith("Run complete in ")


def test_coverage_issue_makes_a_fully_delivered_run_warn() -> None:
    """The gap this closes: delivery was perfect, coverage was not.

    Before, a run that skipped a repo entirely still reported
    "162 offered, 162 posted, 0 failed" as SUCCESS, because the tally is
    honest about delivery and silent about coverage.
    """
    ctx = conf.RunContext.for_run()
    assert ctx.report is not None
    ctx.report.count("offered", 162)
    ctx.report.count("posted", 162)
    assert ctx.report.severity == "SUCCESS"

    conf._report_issue(
        "repo_download_failed", "deejay-cog", RuntimeError("404"), ctx=ctx
    )
    assert ctx.report.severity == "WARN"
