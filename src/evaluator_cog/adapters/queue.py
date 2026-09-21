"""What one queue message means, and what happens when it cannot be done.

The evaluator's front door, minus the door. This module used to open with
a long-polling loop that received messages, ran them and deleted them; the
worker runs on Lambda now, so the platform receives and deletes and what
is left here is the part that was always the point — reading a message,
doing its work, and being clear about failure.

``adapters.lambda_worker`` is the entrypoint that calls into this.

**Nothing is deleted until the work is done.** The rule survives the move
and inverts: this code no longer deletes anything, so "do not delete"
became "report the message back" and lives in the entrypoint. Every
failure path here still ends by raising rather than swallowing, which is
what lets that happen. A job that raises, a delivery that landed nowhere,
a worker killed mid-evaluation — all of them leave the message on the
queue, and SQS redelivers once the visibility timeout expires.

**An unrecognised message type is a producer bug.** This queue is
evaluator-cog's alone — one prefix per cog in ``infra/``. A shared fleet
queue was considered and cannot work, because SQS has no selective
receive: a consumer takes whatever it is handed, so an unrecognised type
would send another cog's job to this cog's dead-letter queue. Refusing it
deliberately beats guessing at it.

**The Railway consumer is gone.** ``main()``, the poll loop and the
SIGTERM handler were deleted with the cutover, along with ``railway.json``.
Rolling back to a container is now a rewrite rather than a restart —
deliberately, because two consumers on one queue is the failure that cost
five evaluations, and keeping a second one runnable is how that happens by
accident.
"""

from __future__ import annotations

import json
from typing import Any

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
    flow_name_for_mode,
    handler,
    run_introspection,
    stamp_versions,
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
TYPE_INTROSPECTION = "evaluation.introspection"


class UnprocessableMessage(RuntimeError):
    """The message cannot be handled by this consumer, ever.

    Distinct from a job that failed: retrying will not help, but the
    message is still left for the dead-letter queue rather than dropped,
    because something produced it and someone should see what.
    """


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

    # Named before the context exists, because the report is created with
    # it. An unrecognised mode is still refused below — this only decides
    # what the run calls itself, and a bad value never gets that far.
    if kind == TYPE_INTROSPECTION:
        flow_name = "introspection"
    else:
        flow_name = flow_name_for_mode(str(payload.get("mode") or "deterministic"))

    ctx = RunContext.for_run(flow_name)

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
    ``handler`` is called once per repository and a fleet pass is N of
    them, so a report built there would be one per repository rather than
    one per job — and ``RunReport.send`` is once-per-instance. Reporting from
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
    # Counted, not named. Both sources of a suppressed finding are
    # ordinary — a redelivered message re-offering a run's whole set, or
    # one repository failing a rule the same way twice, which CD-026 makes
    # routine — so this is a number, not a flag. The rule ids are in the
    # consumer's log if a run is ever worth chasing; putting them here
    # would bury the line that matters under the deduplication working.
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
            # Processor only: the job may have died resolving the catalog,
            # and a version it did not resolve is not stamped as one.
            stamp_versions(f"{what} failed: {type(exc).__name__}: {exc}"),
            repo=_REPO,
            source="queue_consumer",
        )
    except Exception:  # noqa: BLE001 — the notification is not the job
        log.exception("consumer: could not report the failure")
