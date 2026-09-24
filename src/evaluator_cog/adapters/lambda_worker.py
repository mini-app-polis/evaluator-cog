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
(``function_response_types`` in mini-app-polis/infra's cog-worker module), so this response
shape is expected rather than optional. Raising instead would fail the
whole batch — identical behaviour at ``batch_size = 1``, and wrong the
moment that is tuned, because one bad record would redeliver every record
beside it.
"""

from __future__ import annotations

import os
from typing import Any

import sentry_sdk
from mini_app_polis import logger as logger_mod

from evaluator_cog._deadline import deadline
from evaluator_cog.adapters.queue import (
    UnprocessableMessage,
    _report_failure,
    process_message,
)

log = logger_mod.get_logger()

# At import, not per invocation. A Lambda container is reused across
# invocations, so this runs once per cold start — initialising on every
# call would pay the setup repeatedly and register duplicate integrations.
sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    environment=os.getenv("ENVIRONMENT", "production"),
)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Do each record's work, and name the ones that must come back.

    Never raises. An exception escaping here fails the whole batch, and at
    ``batch_size = 1`` that looks identical to reporting the one record —
    right up until the batch size changes, at which point a single bad
    record starts dragging its neighbours back onto the queue. Reporting
    per record is correct at every size.

    ``context`` is read for one thing only: how long is left. The run is
    stopped a margin before the function's timeout so that it fails the
    ordinary way — Lambda kills a timed-out invocation outright, with no
    report sent and nothing said. That is not the handler trimming its work
    to fit; the timeout Terraform sets is still the budget.
    """
    records = event.get("Records", []) if isinstance(event, dict) else []
    failures: list[dict[str, str]] = []

    for record in records:
        message_id = str(record.get("messageId") or "")
        attempt = (record.get("attributes") or {}).get("ApproximateReceiveCount", "?")

        try:
            with deadline(context):
                process_message(record.get("body") or "")
        except UnprocessableMessage as exc:
            # A shape this consumer does not handle — a producer bug, since
            # this queue is evaluator-cog's alone. Reported back so it
            # exhausts its receives and lands in the dead-letter queue,
            # where a person can see what produced it. Deleting it here
            # would make the bad producer invisible.
            log.error("worker: unprocessable message (attempt %s): %s", attempt, exc)
            # Once, on the first receive. Every later receive fails the same
            # way, and where it ends up — the dead-letter queue — has an
            # alarm of its own; five reports of one bad message is noise
            # (PIPE-021).
            if attempt in ("1", "?"):
                _report_failure("an unprocessable message", exc)
            failures.append({"itemIdentifier": message_id})
        except Exception as exc:  # noqa: BLE001 — every failure is a retry
            log.exception("worker: job failed (attempt %s)", attempt)
            _report_failure("a queued evaluation", exc)
            failures.append({"itemIdentifier": message_id})

    if failures:
        log.warning(
            "worker: %d of %d record(s) returned to the queue",
            len(failures),
            len(records),
        )

    # Anything absent from this list is deleted by the mapping. An empty
    # list therefore means "all of it is done" — which is true only because
    # every failure above appended to it.
    return {"batchItemFailures": failures}
