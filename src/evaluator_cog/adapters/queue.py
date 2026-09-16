"""Queue consumer: the evaluator's front door.

This replaced an HTTP adapter, and the difference is what happens to an
accepted job. A 202 put the work in one process's memory behind a lock; a
deploy, an OOM or a restart mid-burst discarded every accepted-but-unstarted
job silently — no findings, no failure, no retry, and each repository's
record sitting at its previous state looking healthy. A message on SQS is
durable, redelivered if this process dies holding it, and dead-lettered if
it cannot be processed at all.

**Nothing is deleted until the work is done.** That single rule is what
makes the above true, and it is why every failure path here ends by
letting the message go back rather than by swallowing anything. A job that
raises, a delivery that landed nowhere, a process killed mid-evaluation —
all of them leave the message on the queue, and SQS redelivers it once the
visibility timeout expires.

**One queue, several message types.** The fleet's other cogs move onto this
queue in step 4, so a type this consumer does not understand is expected
rather than exceptional. It is left for the redrive policy deliberately: a
message nobody can process should end up somewhere a person will look at
it, not be quietly dropped by the first consumer to see it.

**No watchdog, no cron, no sleep workaround.** A long-polling consumer has
continuous outbound traffic, so Railway never considers it idle and this
process will not sleep. That is expected and temporary — the move to Lambda
is what fixes it, by letting the platform do the polling. Anything added
here to work around the cost would be scaffolding that has to come out.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from mini_app_polis import logger as logger_mod
from mini_app_polis.pipeline_status import post_run_finding

from evaluator_cog.flows.conformance import (
    _REPO,
    EvaluationEvent,
    EvaluationResult,
    RunContext,
    _assert_findings_were_delivered,
    _build_conformance_run_id,
    _build_deterministic_run_id,
    _get_standards_version,
    handler,
    run_fleet_sweep,
    run_introspection,
)

log = logger_mod.get_logger()

#: Must match api-kaianolevine-com's evaluation_dispatch. A mismatch is a
#: message this consumer refuses rather than misreads.
#:
#: This queue is evaluator-cog's alone — `infra/` names it from
#: `name_prefix`, one prefix per cog. A shared fleet queue was considered
#: and cannot work: SQS has no selective receive, so a consumer takes
#: whatever it is handed, and an unrecognised type would send another
#: cog's job to *this* cog's dead-letter queue. The type check below is
#: therefore a producer-bug detector, not a router.
MESSAGE_VERSION = 1
TYPE_REPOSITORY = "evaluation.repository"
TYPE_SWEEP = "evaluation.sweep"
TYPE_INTROSPECTION = "evaluation.introspection"

#: Long polling. Short polling bills empty receives and adds latency.
WAIT_TIME_SECONDS = 20

#: One message per receive. The unit of work is one repository, and the
#: queue's visibility timeout is sized for one job — a batch would make the
#: deadline depend on how many happened to arrive together.
MAX_MESSAGES = 1


class UnprocessableMessage(RuntimeError):
    """The message cannot be handled by this consumer, ever.

    Distinct from a job that failed: retrying will not help, but the
    message is still left for the dead-letter queue rather than dropped,
    because something produced it and someone should see what.
    """


@dataclass
class _Shutdown:
    """Set by SIGTERM so the current job finishes before the process exits.

    Railway sends SIGTERM on every deploy. Without this the process dies
    mid-evaluation, and while the message is safe — it was never deleted —
    it waits out the whole visibility timeout before anyone retries it.
    Finishing the job in hand turns a deploy from a six-minute stall into
    nothing at all.
    """

    requested: bool = False

    def install(self) -> None:
        def _handle(signum: int, _frame: Any) -> None:
            self.requested = True
            log.info("consumer: %s received, finishing the current job", signum)

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)


def _event_from(payload: dict[str, Any], *, ctx: RunContext) -> EvaluationEvent:
    """One repository, as the API described it.

    No registry lookup: the event carries what CI already knew, and the
    repository's own evaluator.yaml supplies its type and exemptions.

    The run id is minted here when the caller did not supply one, and that
    placement is the point. It carries the catalog version the findings are
    graded against, and that version is whatever is published *now* — not
    whatever was current when the API accepted the request, which may have
    been a release ago. A fleet pass supplies its own id so every
    repository in the pass shares one.
    """
    repo = str(payload.get("repo") or "").strip()
    if not repo:
        raise UnprocessableMessage("repository message names no repo")

    mode = str(payload.get("mode") or "deterministic")
    if mode not in {"deterministic", "llm"}:
        raise UnprocessableMessage(f"unknown mode {mode!r}")

    run_id = str(payload.get("run_id") or "")
    if not run_id:
        standards_version = _get_standards_version(ctx=ctx)
        run_id = (
            _build_conformance_run_id(standards_version)
            if mode == "llm"
            else _build_deterministic_run_id(standards_version)
        )

    # Services, when the dispatcher grouped them; one synthesised service
    # when it did not.
    #
    # A release-triggered evaluation names a repository and nothing else,
    # and synthesising its single service from the repo id is right: CI
    # knows what it built and no registry lookup can add to that.
    #
    # A fan-out is different in a way that is not cosmetic. A monorepo is
    # ONE event carrying every app in it, because sibling deduplication
    # treats an identical finding on two apps as one issue and cannot know
    # that until every app has been evaluated. Flatten a monorepo into one
    # message per app and monorepo_root, the workspace package.json and
    # monorepo_context all resolve to None, _deduplicate_sibling_findings
    # never fires — it is gated on more than one service — and duplicate
    # findings land looking like a clean run. So the grouping travels in
    # the message rather than being rebuilt from it.
    services = payload.get("services")
    if isinstance(services, list) and services:
        if not all(isinstance(service, dict) for service in services):
            raise UnprocessableMessage("services must be a list of objects")
        resolved = tuple(services)
    else:
        repo_id = str(payload.get("repo_id") or repo)
        resolved = ({"id": repo_id, "repo": repo},)

    monorepo = payload.get("monorepo")
    if monorepo is not None and not isinstance(monorepo, dict):
        raise UnprocessableMessage("monorepo must be an object")

    return EvaluationEvent(
        org=str(payload.get("org") or "mini-app-polis"),
        repo=repo,
        ref=str(payload.get("ref") or "main"),
        services=resolved,
        run_id=run_id,
        mode=mode,
        monorepo=monorepo,
        standards_version=str(payload.get("standards_version") or ""),
    )


def process_message(body: str) -> None:
    """Do one message's work. Raises if the message must be redelivered.

    Shared with the Lambda entrypoint, which differs only in where the body
    comes from and who deletes it afterwards.
    """
    try:
        message = json.loads(body)
    except ValueError as exc:
        raise UnprocessableMessage(f"body is not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise UnprocessableMessage("body is not an object")

    version = message.get("version")
    if version != MESSAGE_VERSION:
        raise UnprocessableMessage(
            f"message version {version!r}, this consumer speaks {MESSAGE_VERSION}"
        )

    kind = message.get("type")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise UnprocessableMessage("message carries no payload object")

    ctx = RunContext.for_run()

    if kind == TYPE_REPOSITORY:
        event = _event_from(payload, ctx=ctx)
        result = handler(event, log=log, ctx=ctx)
        log.info(
            "consumer: %s@%s evaluated=%s not_evaluated=%s",
            event.repo,
            event.ref,
            result.evaluated,
            result.not_evaluated,
        )
        # A run that computed findings and delivered none of them is a
        # systemic fault. Raising here means the message is not deleted, so
        # the work is retried rather than logged and lost — which is the
        # property the HTTP adapter could not have.
        _assert_findings_were_delivered(log, ctx=ctx)
        _report_run(event, result, ctx=ctx)
        return

    if kind == TYPE_SWEEP:
        mode = str(payload.get("mode") or "deterministic")
        if mode not in {"deterministic", "llm"}:
            raise UnprocessableMessage(f"unknown sweep mode {mode!r}")
        result = run_fleet_sweep(
            mode=mode,
            run_id=str(payload.get("run_id") or "") or None,
            log=log,
            ctx=ctx,
        )
        log.info(
            "consumer: sweep %d repos, evaluated=%d not_evaluated=%d",
            result.repos,
            len(result.evaluated),
            len(result.not_evaluated),
        )
        return

    if kind == TYPE_INTROSPECTION:
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            raise UnprocessableMessage("introspection message names no run_id")
        run_introspection(
            run_id=run_id,
            # The fan-out pass to grade. Only XSTACK-008 reads it, and it
            # is optional on purpose: the other five checks grade the
            # registry, the catalog and the stored findings, none of which
            # belong to a pass.
            pass_run_id=str(payload.get("pass_run_id") or ""),
            standards_version=str(payload.get("standards_version") or ""),
            log=log,
            ctx=ctx,
        )
        _assert_findings_were_delivered(log, ctx=ctx)
        _report_introspection(ctx=ctx)
        return

    raise UnprocessableMessage(f"unknown message type {kind!r}")


def _report_run(
    event: EvaluationEvent, result: EvaluationResult, *, ctx: RunContext
) -> None:
    """Say how one repository's evaluation went.

    Here rather than in ``handler``, and the placement is load-bearing:
    ``handler`` is also what ``run_fleet_sweep`` calls for each repository
    it visits, and ``RunReport.send`` is once-per-instance. Reporting from
    inside the handler would spend the sweep's single report on its first
    repository and silence the sweep's own summary — twelve messages where
    there should be one, and the one that mattered missing.

    Called after the delivery assertion, deliberately. A run whose findings
    all failed to post raises there and is reported as a failure instead,
    because two messages for one event is how a channel earns being
    ignored.
    """
    if ctx.report is None:
        return

    ctx.report.ok(len(result.evaluated))
    for service_id in result.not_evaluated:
        ctx.report.issue("not_evaluated", service_id)
    # Delivery failure is an issue like any other, matching the sweep: a
    # run that posted nine of ten batches is WARN for the same reason a
    # run that skipped a service is.
    if ctx.tally.failed:
        ctx.report.issue("delivery_failed", f"{ctx.tally.failed} finding(s)")
    ctx.report.count("flow", event.mode)
    # Not "repo": RunReport.send splats its counters into post_run_finding
    # as keyword arguments, and that function already has a repo parameter
    # of its own. The collision is a TypeError at send time, on a path a
    # green test suite would otherwise never walk.
    ctx.report.count("target", f"{event.repo}@{event.ref}")
    ctx.report.count("offered", ctx.tally.attempted)
    ctx.report.count("posted", ctx.tally.posted)
    ctx.report.count("duplicate", ctx.tally.duplicates)
    # Notable whatever the tally, which is where this parts company with
    # the sweep. A release triggered this run and someone is waiting to
    # hear it happened, so a clean evaluation must still say so. Silence
    # already meant both "clean run" and "the job was destroyed before it
    # started" — the ambiguity that let a stub Lambda eat five
    # evaluations without anyone noticing.
    ctx.report.send(notable=True)


def _report_introspection(*, ctx: RunContext) -> None:
    """Say how the fleet-scoped checks went.

    Notable whatever the tally, matching the per-repository report and for
    the same reason: something asked for this run and is waiting to hear
    it happened. These six checks are the ones nobody would notice had
    stopped — they grade the inventory and the table rather than any
    repository, so no release goes red when they quietly do nothing.
    """
    if ctx.report is None:
        return

    if ctx.tally.failed:
        ctx.report.issue("delivery_failed", f"{ctx.tally.failed} finding(s)")
    ctx.report.count("flow", "introspection")
    ctx.report.count("offered", ctx.tally.attempted)
    ctx.report.count("posted", ctx.tally.posted)
    ctx.report.count("duplicate", ctx.tally.duplicates)
    ctx.report.send(notable=True)


def _report_failure(what: str, exc: BaseException) -> None:
    """Say a job died, in the one place someone is watching."""
    try:
        post_run_finding(
            "conformance-check",
            "ERROR",
            f"{what} failed: {type(exc).__name__}: {exc}",
            repo=_REPO,
            source="queue_consumer",
        )
    except Exception:  # noqa: BLE001 — the notification is not the job
        log.exception("consumer: could not report the failure")


def main() -> None:
    """Long-poll the queue until told to stop."""
    import sentry_sdk
    from dotenv import load_dotenv
    from mini_app_polis.environment import current_environment

    load_dotenv()
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN_EVALUATOR"),
        environment=current_environment().value,
    )

    queue_url = (os.environ.get("EVALUATION_QUEUE_URL") or "").strip()
    if not queue_url:
        # Fail loudly at boot rather than idling forever against nothing.
        # A consumer that starts, polls no queue and reports healthy is the
        # same silence this whole migration exists to remove.
        log.error("consumer: EVALUATION_QUEUE_URL is not set; refusing to start")
        sys.exit(1)

    shutdown = _Shutdown()
    shutdown.install()

    # Named for this caller rather than boto3's conventional
    # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY. The producer holds a
    # send-only key and this holds a receive-only one, and the fleet keeps
    # its secrets in one store — under the conventional names the two
    # collide, and this side fails quietly: receive_message raises, the
    # loop logs and keeps polling, and the process never dies, so nothing
    # restarts and nothing alerts.
    #
    # Unset falls through to boto3's default chain, which is exactly what
    # step 5 needs: on Lambda the execution role supplies these and no key
    # exists at all.
    key_id = (os.environ.get("EVALUATION_QUEUE_CONSUMER_KEY_ID") or "").strip()
    secret = (os.environ.get("EVALUATION_QUEUE_CONSUMER_SECRET") or "").strip()
    credentials = (
        {"aws_access_key_id": key_id, "aws_secret_access_key": secret} if key_id else {}
    )
    log.info(
        "consumer: credentials from %s",
        "EVALUATION_QUEUE_CONSUMER_KEY_ID" if key_id else "the default chain",
    )

    sqs = boto3.client(
        "sqs",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        **credentials,
    )
    log.info("consumer: polling %s", queue_url)

    while not shutdown.requested:
        try:
            received = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=MAX_MESSAGES,
                WaitTimeSeconds=WAIT_TIME_SECONDS,
                MessageAttributeNames=["All"],
                AttributeNames=["ApproximateReceiveCount"],
            )
        except (ClientError, BotoCoreError):
            # A transport or credential problem. Log and keep polling: the
            # restart policy is the backstop, and exiting on the first
            # blip would turn a rate limit into an outage.
            log.exception("consumer: receive failed")
            continue

        for message in received.get("Messages", []):
            receipt = message.get("ReceiptHandle")
            body = message.get("Body") or ""
            attempt = (message.get("Attributes") or {}).get(
                "ApproximateReceiveCount", "?"
            )
            try:
                process_message(body)
            except UnprocessableMessage as exc:
                # Left on the queue on purpose. It will exhaust its
                # receives and land in the dead-letter queue, where a
                # person can see what produced it.
                log.error(
                    "consumer: unprocessable message (attempt %s): %s", attempt, exc
                )
                _report_failure("an unprocessable message", exc)
            except Exception as exc:  # noqa: BLE001 — every failure is a retry
                log.exception("consumer: job failed (attempt %s)", attempt)
                _report_failure("a queued evaluation", exc)
            else:
                # Only now. Deleting before the work is done is the one
                # change that would give back everything this costs.
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)

    log.info("consumer: stopped")


if __name__ == "__main__":
    main()
