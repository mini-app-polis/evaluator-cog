"""The entrypoint, and the one rule it must not get backwards.

The container consumer deletes a message only once the work is done. Here
nothing is deleted by this code: the event source mapping deletes whatever
the handler does not name in ``batchItemFailures``. So the equivalent of
"do not delete" is *reporting* the message, and the failure mode of
getting it wrong is silent — a failed job is deleted, no findings are
posted, and the repository's record sits at its previous state looking
healthy. That is the shape the queue was introduced to remove, reachable
again through one inverted condition.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from evaluator_cog.adapters import lambda_worker as lw
from evaluator_cog.adapters.queue import MESSAGE_VERSION, TYPE_REPOSITORY


def _event(*bodies: str, ids: tuple[str, ...] = ()) -> dict:
    return {
        "Records": [
            {
                "messageId": ids[i] if i < len(ids) else f"m-{i}",
                "body": body,
                "attributes": {"ApproximateReceiveCount": "1"},
            }
            for i, body in enumerate(bodies)
        ]
    }


def _body(**payload) -> str:
    return json.dumps(
        {"type": TYPE_REPOSITORY, "version": MESSAGE_VERSION, "payload": payload}
    )


@pytest.fixture(autouse=True)
def _quiet_reports():
    with patch.object(lw, "_report_failure"):
        yield


def test_a_finished_job_is_not_reported_back() -> None:
    """An empty list is what lets the mapping delete. It means "all of it
    is done", so it must be reachable only when nothing failed."""
    with patch.object(lw, "process_message"):
        result = lw.lambda_handler(_event(_body(repo="watcher-cog")), None)

    assert result == {"batchItemFailures": []}


def test_a_failed_job_is_returned_to_the_queue() -> None:
    """Not deleted, so it is retried and eventually dead-lettered. Leaving
    it out of the list would discard the work silently — no findings, no
    failure, no retry."""
    with patch.object(lw, "process_message", side_effect=RuntimeError("boom")):
        result = lw.lambda_handler(
            _event(_body(repo="watcher-cog"), ids=("m-9",)), None
        )

    assert result == {"batchItemFailures": [{"itemIdentifier": "m-9"}]}


def test_an_unprocessable_message_is_returned_too() -> None:
    """It exhausts its receives and lands in the dead-letter queue, where
    a person can see what produced it. Deleting it here would make a bad
    producer invisible."""
    with patch.object(
        lw, "process_message", side_effect=lw.UnprocessableMessage("unknown type")
    ):
        result = lw.lambda_handler(_event(_body(), ids=("m-3",)), None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m-3"}]}


def test_one_bad_record_does_not_drag_its_neighbours_back() -> None:
    """The reason this reports per record rather than raising.

    At batch_size = 1 the two are indistinguishable. Raise instead, and the
    day someone tunes the batch size, a single bad record starts
    redelivering every record beside it — re-running work that already
    succeeded.
    """
    calls = {"n": 0}

    def _one_fails(body):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the second one")

    with patch.object(lw, "process_message", _one_fails):
        result = lw.lambda_handler(
            _event(_body(), _body(), _body(), ids=("a", "b", "c")), None
        )

    assert result == {"batchItemFailures": [{"itemIdentifier": "b"}]}


@pytest.mark.parametrize(
    "boom", [RuntimeError("boom"), ValueError("bad"), KeyError("missing")]
)
def test_no_ordinary_failure_escapes(boom) -> None:
    """An exception escaping here fails the whole batch, bypassing the
    per-record report entirely — so every way a job can die has to land in
    batchItemFailures instead."""
    with patch.object(lw, "process_message", side_effect=boom):
        result = lw.lambda_handler(_event(_body(), ids=("m-1",)), None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}


def test_a_base_exception_is_allowed_through() -> None:
    """Exception, not BaseException, matching the container consumer.

    SystemExit and KeyboardInterrupt are the runtime telling this process
    to stop, not a job failing. Catching them would report the record as a
    retryable failure and carry on with the next one, which is how a
    shutting-down container quietly eats a batch.
    """
    with (
        patch.object(lw, "process_message", side_effect=SystemExit(1)),
        pytest.raises(SystemExit),
    ):
        lw.lambda_handler(_event(_body()), None)


def test_an_event_with_no_records_is_not_an_error() -> None:
    assert lw.lambda_handler({}, None) == {"batchItemFailures": []}


def test_a_failure_is_reported_to_the_channel() -> None:
    """Same as the container consumer. A job that died and said nothing is
    the silence this whole migration exists to remove."""
    with (
        patch.object(lw, "process_message", side_effect=RuntimeError("boom")),
        patch.object(lw, "_report_failure") as reported,
    ):
        lw.lambda_handler(_event(_body()), None)

    assert reported.called


def test_the_real_message_path_is_reached() -> None:
    """Not just the error handling — process_message is given the record's
    body verbatim, so the Lambda and the container evaluate the same
    message the same way."""
    with patch.object(lw, "process_message") as process:
        lw.lambda_handler(_event(_body(repo="deejay-cog", ref="main")), None)

    body = json.loads(process.call_args.args[0])
    assert body["payload"]["repo"] == "deejay-cog"
    assert body["type"] == TYPE_REPOSITORY


def test_the_handler_signature_matches_what_lambda_calls() -> None:
    """Two positional arguments. A mismatch here is a runtime error on the
    first real invocation, which is an expensive place to find it."""
    import inspect

    params = list(inspect.signature(lw.lambda_handler).parameters)
    assert len(params) == 2
