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
    conf._reset_run_tally()
    conf._RUN_TALLY.merge(
        PostResult(attempted=162, posted=0, failed=162, errors=["Connection failed"])
    )
    with pytest.raises(conf.FindingDeliveryError) as excinfo:
        conf._assert_findings_were_delivered(MagicMock())
    message = str(excinfo.value)
    assert "162" in message
    # The message must name the two variables an operator should check.
    assert "KAIANO_API_BASE_URL" in message
    assert "EVALUATOR_COG_API_KEY" in message
    conf._reset_run_tally()


def test_delivery_assertion_is_silent_on_a_partial_failure() -> None:
    """One landing proves the route works; the rest are warned about.

    The assertion that matters is total_failure: nine findings reached
    the API, so the route is up and the run must not be failed. The log
    is a stand-in here — the per-emitter warning is asserted in
    test_post_tracked_logs_what_landed_not_what_was_handed_over.
    """
    conf._reset_run_tally()
    conf._RUN_TALLY.merge(PostResult(attempted=10, posted=9, failed=1))
    prefect_log = MagicMock()

    conf._assert_findings_were_delivered(prefect_log)

    # "Silent" is the claim in the name, so assert it: this function says
    # nothing on a partial failure. The per-emitter warning is raised by
    # _post_tracked, not here, and is asserted in
    # test_post_tracked_logs_what_landed_not_what_was_handed_over.
    assert not prefect_log.warning.called
    assert not prefect_log.error.called
    assert conf._RUN_TALLY.total_failure is False
    assert conf._RUN_TALLY.posted == 9
    assert conf._RUN_TALLY.attempted == 10
    conf._reset_run_tally()


def test_delivery_assertion_is_silent_when_nothing_was_offered() -> None:
    """A wholly conformant fleet posts nothing and that is not a failure.

    Nothing attempted is not the same as nothing delivered, and only the
    second is a failure. This pins that distinction: an empty tally
    leaves total_failure False and the run succeeds.
    """
    conf._reset_run_tally()
    prefect_log = MagicMock()

    conf._assert_findings_were_delivered(prefect_log)

    # Silent here too: an empty tally is a conformant fleet, not a
    # delivery failure, and nothing should be logged about it.
    assert not prefect_log.warning.called
    assert not prefect_log.error.called
    assert conf._RUN_TALLY.attempted == 0
    assert conf._RUN_TALLY.total_failure is False
    conf._reset_run_tally()


def test_post_tracked_logs_what_landed_not_what_was_handed_over() -> None:
    """The regression test for the false 'posted N findings' log line."""
    conf._reset_run_tally()
    prefect_log = MagicMock()
    with patch.object(
        conf,
        "post_findings",
        return_value=PostResult(attempted=3, posted=0, failed=3, errors=["nope"]),
    ):
        conf._post_tracked("EVAL-003", prefect_log, findings=[1, 2, 3])

    # It must NOT have claimed a successful post.
    assert not prefect_log.info.called, (
        "logged a success line for findings that never reached the API"
    )
    assert prefect_log.warning.called
    assert conf._RUN_TALLY.total_failure is True
    conf._reset_run_tally()


def test_post_tracked_accumulates_across_emitters() -> None:
    conf._reset_run_tally()
    prefect_log = MagicMock()
    for res in (
        PostResult(attempted=2, posted=2),
        PostResult(attempted=3, posted=0, failed=3, errors=["x"]),
    ):
        with patch.object(conf, "post_findings", return_value=res):
            conf._post_tracked("repo", prefect_log, findings=[])
    assert conf._RUN_TALLY.attempted == 5
    assert conf._RUN_TALLY.posted == 2
    assert conf._RUN_TALLY.failed == 3
    # Two landed, so this is partial, not total — the run still passes.
    assert conf._RUN_TALLY.total_failure is False
    conf._reset_run_tally()


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
    from unittest.mock import MagicMock

    import evaluator_cog.flows.conformance as conf

    result = conf.PostResult()
    result.duplicates = 1
    result.duplicate_details = ["CD-021 for deejay-cog matched the stored row"]

    prefect_log = MagicMock()
    with patch.object(conf, "post_findings", return_value=result):
        conf._post_tracked("deejay-cog", prefect_log, findings=[{}], repo="deejay-cog")

    assert prefect_log.warning.called, "a suppressed finding logged nothing"
    message = " ".join(str(c) for c in prefect_log.warning.call_args[0])
    assert "duplicate" in message.lower()
    assert "deejay-cog" in " ".join(
        str(a) for a in prefect_log.warning.call_args[0]
    ) or "deejay-cog" in str(prefect_log.warning.call_args)
