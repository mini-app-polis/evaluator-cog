"""The consumer, and the one rule that makes a queue worth having.

A 202 from the old HTTP adapter put the work in one process's memory. A
deploy, an OOM or a restart mid-burst discarded every accepted-but-unstarted
job silently — no findings, no failure, no retry, and each repository's
record sitting at its previous state looking healthy. A message is
different only because it is not deleted until the work is done, so most of
what is pinned here is about when the delete happens.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from evaluator_cog.adapters import queue as q


def _body(kind: str = q.TYPE_REPOSITORY, version: int = q.MESSAGE_VERSION, **payload):
    if kind == q.TYPE_REPOSITORY and not payload:
        payload = {"repo": "watcher-cog", "ref": "v1.2.3", "mode": "deterministic"}
    return json.dumps({"type": kind, "version": version, "payload": payload})


# ── reading a message ────────────────────────────────────────────────────


def test_a_repository_message_becomes_one_evaluation() -> None:
    with (
        patch.object(q, "handler") as handler,
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", return_value="7.0.0"),
    ):
        q.process_message(_body())

    event = handler.call_args.args[0]
    assert (event.repo, event.ref, event.mode) == (
        "watcher-cog",
        "v1.2.3",
        "deterministic",
    )
    assert event.org == "mini-app-polis"
    assert event.services == ({"id": "watcher-cog", "repo": "watcher-cog"},)


def test_an_absent_run_id_is_minted_from_the_catalog_version_now() -> None:
    """Not from the version current when the API accepted the request.

    The run id carries the catalog version the findings are graded against.
    A job can sit on the queue across a standards release, so the version
    is resolved where the grading happens.
    """
    with (
        patch.object(q, "handler") as handler,
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", return_value="7.1.0"),
    ):
        q.process_message(_body())

    assert handler.call_args.args[0].run_id.startswith("deterministic-7.1.0-")


def test_a_supplied_run_id_is_used_unchanged() -> None:
    """A fleet pass keeps one run id across every repository in it."""
    with (
        patch.object(q, "handler") as handler,
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", side_effect=AssertionError),
    ):
        q.process_message(
            _body(repo="deejay-cog", mode="llm", run_id="conformance-7.0.0-fleet")
        )

    assert handler.call_args.args[0].run_id == "conformance-7.0.0-fleet"


def test_a_sweep_message_runs_the_fleet() -> None:
    with patch.object(q, "run_fleet_sweep") as sweep:
        sweep.return_value = MagicMock(repos=3, evaluated=[], not_evaluated=[])
        q.process_message(_body(kind=q.TYPE_SWEEP, mode="llm"))

    assert sweep.call_args.kwargs["mode"] == "llm"
    assert sweep.call_args.kwargs["run_id"] is None


# ── messages this consumer cannot handle ─────────────────────────────────
#
# Left for the redrive policy rather than dropped. Something produced them
# and a person should get to see what.


@pytest.mark.parametrize(
    "body, why",
    [
        ("not json at all", "body is not JSON"),
        (json.dumps([1, 2, 3]), "not an object"),
        (_body(version=99), "message version"),
        (json.dumps({"type": q.TYPE_REPOSITORY, "version": 1}), "no payload"),
        (_body(kind="deejay.transcribe"), "unknown message type"),
        (_body(repo="", mode="deterministic"), "names no repo"),
        (_body(repo="x", mode="guess"), "unknown mode"),
    ],
)
def test_unhandleable_messages_are_refused_not_guessed_at(body: str, why: str) -> None:
    with pytest.raises(q.UnprocessableMessage, match=why):
        q.process_message(body)


def test_a_message_type_from_another_cog_is_expected_not_exceptional() -> None:
    """One queue serves the fleet from step 4 onward.

    This consumer must recognise a shape it does not handle and say so,
    rather than crashing in a way that reads as its own bug.
    """
    with pytest.raises(q.UnprocessableMessage, match="unknown message type"):
        q.process_message(_body(kind="transcription.run", something="else"))


def test_a_run_that_delivered_nothing_raises_so_it_is_retried() -> None:
    """The property the HTTP adapter could not have.

    A run that computed findings and delivered none of them used to be
    logged and lost. Raising here means the message is never deleted, so
    the work comes back.
    """
    from evaluator_cog.flows.conformance import FindingDeliveryError

    with (
        patch.object(q, "handler"),
        patch.object(q, "_get_standards_version", return_value="7.0.0"),
        patch.object(
            q,
            "_assert_findings_were_delivered",
            side_effect=FindingDeliveryError("nothing landed"),
        ),
        pytest.raises(FindingDeliveryError),
    ):
        q.process_message(_body())


# ── the loop: when the delete happens ────────────────────────────────────


class _OneShot:
    """A shutdown flag that lets the loop run exactly one iteration."""

    def __init__(self) -> None:
        self._reads = 0

    def install(self) -> None:
        pass

    @property
    def requested(self) -> bool:
        self._reads += 1
        return self._reads > 1


def _run_one(monkeypatch, *, process_side_effect=None) -> MagicMock:
    monkeypatch.setenv("EVALUATION_QUEUE_URL", "https://sqs.test/q")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    sqs = MagicMock()
    sqs.receive_message.return_value = {
        "Messages": [
            {
                "ReceiptHandle": "rh-1",
                "Body": _body(),
                "Attributes": {"ApproximateReceiveCount": "1"},
            }
        ]
    }
    with (
        patch.object(q.boto3, "client", return_value=sqs),
        patch.object(q, "_Shutdown", _OneShot),
        patch.object(q, "process_message", side_effect=process_side_effect),
        patch.object(q, "_report_failure"),
        patch("sentry_sdk.init"),
    ):
        q.main()
    return sqs


def test_a_finished_job_is_deleted(monkeypatch) -> None:
    sqs = _run_one(monkeypatch)
    sqs.delete_message.assert_called_once()
    assert sqs.delete_message.call_args.kwargs["ReceiptHandle"] == "rh-1"


def test_a_failed_job_is_left_on_the_queue(monkeypatch) -> None:
    """Not deleting is the retry. Everything else follows from it."""
    sqs = _run_one(monkeypatch, process_side_effect=RuntimeError("the repo exploded"))
    sqs.delete_message.assert_not_called()


def test_an_unprocessable_message_is_left_for_the_dead_letter_queue(
    monkeypatch,
) -> None:
    """Dropping it here would hide whatever produced it."""
    sqs = _run_one(
        monkeypatch, process_side_effect=q.UnprocessableMessage("unknown type")
    )
    sqs.delete_message.assert_not_called()


def test_the_consumer_long_polls(monkeypatch) -> None:
    """Short polling bills empty receives and adds latency to every job."""
    sqs = _run_one(monkeypatch)
    assert sqs.receive_message.call_args.kwargs["WaitTimeSeconds"] == 20
    # One job per receive: the visibility timeout is sized for one, and a
    # batch would make the deadline depend on what happened to arrive.
    assert sqs.receive_message.call_args.kwargs["MaxNumberOfMessages"] == 1


def test_it_refuses_to_start_without_a_queue(monkeypatch) -> None:
    """A consumer polling nothing and reporting healthy is the old silence."""
    monkeypatch.delenv("EVALUATION_QUEUE_URL", raising=False)
    with patch("sentry_sdk.init"), pytest.raises(SystemExit):
        q.main()
