"""The Lambda entrypoint. The same work, reached by a different door.

``adapters.queue`` polls and deletes; here the platform does both. What is
shared is :func:`~evaluator_cog.adapters.queue.process_message`, which was
written for exactly this — "shared with the Lambda entrypoint, which
differs only in where the body comes from and who deletes it afterwards".

**The delete rule survives the move, and it is the whole point.** The
container consumer deletes a message only after the work finishes. Here
nothing is deleted by this code at all: the event source mapping deletes
what the handler does not report back, so the equivalent of "do not
delete" is naming the message in ``batchItemFailures``. Get that backwards
and a failed job is silently discarded, which is the property the queue
was introduced to remove.

``ReportBatchItemFailures`` is already configured on the mapping
(declared in mini-app-polis/infra), so this response
shape is expected rather than optional. Raising instead would fail the
whole batch — identical behaviour at ``batch_size = 1``, and wrong the
moment that is tuned, because one bad record would redeliver every record
beside it.
"""

from __future__ import annotations

import json
from typing import Any

from mini_app_polis import load_secrets, timing
from mini_app_polis import logger as logger_mod
from mini_app_polis.api.contract import EvaluationIntrospectionMessage
from mini_app_polis.lambda_worker import (
    RunOutOfTime,
    UnprocessableMessage,
    all_failed,
    deadline,
    process_records,
)
from mini_app_polis.sentry import init_sentry

from evaluator_cog.adapters.queue import _report_failure, process_message

log = logger_mod.get_logger()

# At import, not per invocation. A Lambda container is reused across
# invocations, so this runs once per cold start — initialising on every
# call would pay the setup repeatedly and register duplicate integrations.
# An empty DSN in production logs an ERROR rather than passing silently
# (CD-038); the service tag tells this cog's events apart in the shared project.
init_sentry("evaluator-cog")

#: How long before the function's timeout a run is stopped, so that it can
#: still send its report and the handler can still return. The report is
#: one POST with the API client's own timeout; this leaves room for it.
DEADLINE_MARGIN_SECONDS = 30

#: The longest single external request the run makes through the shared
#: library: how much earlier than the stop the shared run deadline is set.
#: The library checks that deadline between attempts, never during one, so
#: an attempt that starts just before it can run this long; set this much
#: earlier, it still ends before the stop. That request is a Drive or Sheets
#: call, whose timeout is the one googleapiclient's ``build()`` gives the
#: library's clients when no socket default timeout is set.
#: evaluator-cog makes no Google request through the library today, so
#: the shared deadline has nothing to act on yet; it is set the same way
#: as in every cog so a Drive or Sheets call added later is covered.
#:
#: The value is googleapiclient's ``DEFAULT_HTTP_TIMEOUT_SEC``, held here
#: rather than imported: the Lambda deploy strips the Google client stack
#: from the zip, so importing it fails the worker at start. A test pins the
#: two together, so a change upstream fails CI rather than drifting.
REQUEST_HEADROOM_SECONDS: float = 60.0


def _mode_label(body: str) -> str:
    """What the message asks for, for the timing line. Never raises.

    ``introspection`` for a fleet pass; otherwise the evaluation mode
    (``deterministic`` or ``llm``), which is what decides how long it waits.
    """
    try:
        message = json.loads(body)
        if message.get("type") == EvaluationIntrospectionMessage.message_type():
            return "introspection"
        return str(message["payload"].get("mode") or "deterministic")
    except Exception:  # noqa: BLE001 — a label must not fail the run
        return "unreadable"


def _attempt(record: dict[str, Any]) -> str:
    """Which receive of this message this is, or "?" when unknown."""
    return str((record.get("attributes") or {}).get("ApproximateReceiveCount", "?"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Do each record's work, and name the ones that must come back.

    Never raises an ordinary failure. An exception escaping here fails the
    whole batch, and at ``batch_size = 1`` that looks identical to reporting
    the one record — right up until the batch size changes, at which point a
    single bad record starts dragging its neighbours back onto the queue.
    :func:`~mini_app_polis.lambda_worker.process_records` reports per
    record, which is correct at every size.

    ``context`` is read for one thing only: how long is left. The run is
    stopped a margin before the function's timeout so that it fails the
    ordinary way — Lambda kills a timed-out invocation outright, with no
    report sent and nothing said. That is not the handler trimming its work
    to fit; the timeout Terraform sets is still the budget.
    """
    # Settings first: a warm container would otherwise run on what it read
    # at cold start, and a credential rotated in Doppler would keep failing
    # until the next one. Without its settings nothing can run, so every
    # record goes back.
    try:
        load_secrets(refresh=True)
    except Exception:  # noqa: BLE001 — nothing can run without its settings
        log.exception("worker: could not refresh settings from SSM")
        return all_failed(event)

    def handle(record: dict[str, Any]) -> None:
        body = record.get("body") or ""
        attempt = _attempt(record)

        # One timing line per record: how much of the run was waiting,
        # and on what (mini_app_polis.timing).
        with timing.invocation(
            cog="evaluator", mode=_mode_label(body), attempt=attempt
        ) as timed:
            try:
                with deadline(
                    context,
                    margin_seconds=DEADLINE_MARGIN_SECONDS,
                    request_headroom_seconds=REQUEST_HEADROOM_SECONDS,
                ):
                    process_message(body)
            except UnprocessableMessage:
                # Reported once, by on_unprocessable below.
                timed.label(outcome="unprocessable")
                raise
            except (Exception, RunOutOfTime) as exc:
                # Every failure is a retry, and is said in the one place
                # someone is watching. Not SystemExit or KeyboardInterrupt:
                # those are the runtime stopping this process, not a job
                # failing, and they go straight through.
                timed.label(outcome="failed")
                log.exception("worker: job failed (attempt %s)", attempt)
                _report_failure("a queued evaluation", exc)
                raise
            timed.label(outcome="ok")

    def on_unprocessable(exc: UnprocessableMessage, record: dict[str, Any]) -> None:
        # A shape this consumer does not handle — a producer bug, since this
        # queue is evaluator-cog's alone. Named for redelivery so it exhausts
        # its receives and lands in the dead-letter queue, where a person can
        # see what produced it. Reported once, on the first receive: every
        # later receive fails the same way, and the dead-letter queue has an
        # alarm of its own; five reports of one bad message is noise
        # (PIPE-021).
        if _attempt(record) in ("1", "?"):
            _report_failure("an unprocessable message", exc)

    result = process_records(event, handle, on_unprocessable=on_unprocessable)
    failures = result["batchItemFailures"]
    if failures:
        records = event.get("Records", []) if isinstance(event, dict) else []
        log.warning(
            "worker: %d of %d record(s) returned to the queue",
            len(failures),
            len(records),
        )

    # Anything absent from this list is deleted by the mapping. An empty
    # list therefore means "all of it is done" — which is true only because
    # process_records names every record whose handle raised.
    return result
