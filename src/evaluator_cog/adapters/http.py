"""HTTP adapter: the evaluator's front door.

Two routes, two shapes of the same work. ``/invoke`` translates a POST
into an ``EvaluationEvent`` and calls ``handler`` — one repository, which
is what a release triggers. ``/sweep`` calls ``run_fleet_sweep``, which
loops that same handler over the registry and then runs the checks that
scope to no repository at all. Neither holds evaluation logic of its own;
the point of the split is that the same functions serve this and whatever
runtime comes after, without either knowing about the other.

**The sweep is the occasional path, and it has no schedule.** It exists
for the two releases that invalidate every repository's last result at
once — a new standards catalog, and a new evaluator — and those releases
call it. There is no cron here, and there is nowhere for one to live: this
process is a web server.

**The request is accepted, not awaited.** An evaluation downloads a
repository and runs a hundred-odd checks; holding the caller's connection
open for that would make the API's forward a long-lived request and put
the evaluator's runtime on the caller's critical path. The route returns
202 with a run id and does the work in the background, which is what makes
the fire-and-forget contract honest rather than merely fast.

**One run context per accepted request.** The catalog, the delivery
tally and the run report used to be module state in ``conformance``,
serialised by a process-wide lock. The lock covered the work and not the
state: this route reset those globals from the request thread, so a
second release arriving while the first was still evaluating wiped the
first one's accounting without ever contending for the lock. Each
request now builds its own :class:`RunContext` and hands it to the
background task, so overlapping evaluations share nothing and no lock is
needed to keep them apart.

**No registry lookup.** The event is built from what the caller knows —
repository, ref, org — and the repo's own ``evaluator.yaml`` supplies its
type and exemptions. That is the end state the registry removal is heading
for, and this path already works that way.
"""

from __future__ import annotations

import hmac
import os
from contextlib import suppress
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from mini_app_polis import logger as logger_mod
from mini_app_polis.pipeline_status import post_run_finding
from pydantic import BaseModel, Field

from evaluator_cog.flows.conformance import (
    _REPO,
    EvaluationEvent,
    RunContext,
    _assert_findings_were_delivered,
    _build_conformance_run_id,
    _build_deterministic_run_id,
    _get_standards_version,
    handler,
    run_fleet_sweep,
)

log = logger_mod.get_logger()

#: Header the API presents. Not a Bearer credential: the caller is another
#: first-party service on an internal hop, and CD-019's two credential
#: types are for callers that can present one.
SECRET_HEADER = "X-Evaluator-Token"

app = FastAPI(
    title="evaluator-cog",
    description="Conformance evaluation, one repository per request.",
)


class InvokeRequest(BaseModel):
    """One repository to evaluate."""

    repo: str = Field(
        ..., min_length=1, description="Repository name, without the org."
    )
    ref: str = Field("main", min_length=1, description="Branch or tag to evaluate.")
    org: str = Field("mini-app-polis", min_length=1, description="Owning GitHub org.")
    mode: Literal["deterministic", "llm"] = Field(
        "deterministic",
        description=(
            "Which engine to run. Deterministic is the release-path default: "
            "it costs no tokens and is the one CI waits on nothing for."
        ),
    )
    repo_id: str | None = Field(
        None,
        description=(
            "The id findings are filed under. Defaults to the repository name, "
            "which is the same thing everywhere except a monorepo app."
        ),
    )
    run_id: str | None = Field(
        None,
        description=(
            "Group these findings with an existing run. Omit and one is "
            "minted, which is what a single repository's release wants."
        ),
    )


class SweepRequest(BaseModel):
    """A whole-fleet pass. Nothing to name — the registry says who."""

    mode: Literal["deterministic", "llm"] = Field(
        "deterministic",
        description=(
            "Which engine to run against every repository. Fleet-wide llm "
            "is the expensive one and is never a release default."
        ),
    )
    run_id: str | None = Field(
        None,
        description="Group these findings with an existing run. Usually omitted.",
    )


class SweepAccepted(BaseModel):
    """What the caller gets back. Not a result — the sweep has not run yet."""

    accepted: bool = True
    run_id: str
    mode: str


class InvokeAccepted(BaseModel):
    """What the caller gets back. Not a result — the work has not run yet."""

    accepted: bool = True
    run_id: str
    repo: str
    mode: str


def require_invoke_secret(
    token: str | None = Header(None, alias=SECRET_HEADER),
) -> None:
    """Check the shared secret. Fails closed when none is configured.

    The Prefect webhook honours its secret when set and skips it when not,
    because that route only posts a Discord embed and a credential that can
    quietly disable the last witness of a crash costs more than the noise it
    prevents. This route is the opposite trade: it clones a repository, runs
    code against it and writes findings. An unset secret here is a
    misconfiguration, and answering requests anyway would be the wrong
    reading of it.
    """
    expected = os.environ.get("EVALUATOR_INVOKE_SECRET", "").strip()
    if not expected:
        log.error("invoke: EVALUATOR_INVOKE_SECRET is not set; refusing requests")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "This evaluator has no invoke secret configured.",
            },
        )
    if not token or not hmac.compare_digest(expected, token):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "unauthorized",
                "message": f"Valid {SECRET_HEADER} required",
            },
        )


def _report_failure(what: str, exc: BaseException) -> None:
    """Say a background job died, in the one place someone is watching.

    What ``make_failure_hook`` did while these ran as Prefect flows. Prefect
    reported a failed run because it owned the run; nothing owns this one,
    so the report has to be made here or not at all — and a background task
    that dies silently is the exact shape of the September outage this
    module's assertions exist to prevent.
    """
    with suppress(Exception):  # the notification is not the job
        post_run_finding(
            "conformance-check",
            "ERROR",
            f"{what} failed: {type(exc).__name__}: {exc}",
            repo=_REPO,
            source="http_adapter",
        )


def _evaluate(event: EvaluationEvent, ctx: RunContext) -> None:
    """Run one evaluation to completion. Never raises into the server.

    Mirrors the flow's tail rather than only calling the handler: a run
    that computed findings and delivered none of them is a systemic fault,
    and the whole reason that assertion exists is that it once looked
    exactly like success from every other angle.

    ``ctx`` is the one the route built when it accepted the request, so
    the assertion below reads this evaluation's tally and not whatever a
    later request left behind.
    """
    try:
        result = handler(event, log=log, ctx=ctx)
        log.info(
            "invoke: %s@%s evaluated=%s not_evaluated=%s",
            event.repo,
            event.ref,
            result.evaluated,
            result.not_evaluated,
        )
        _assert_findings_were_delivered(log, ctx=ctx)
    except Exception as exc:
        # The caller is long gone — 202 was returned before this
        # started — so there is nobody to raise to. Sentry, the log
        # and the notification channel are the report.
        log.exception("invoke: evaluation of %s@%s failed", event.repo, event.ref)
        _report_failure(f"evaluation of {event.repo}@{event.ref}", exc)


def _sweep(*, mode: str, run_id: str, ctx: RunContext) -> None:
    """Run one fleet sweep to completion. Never raises into the server."""
    try:
        result = run_fleet_sweep(mode=mode, run_id=run_id, log=log, ctx=ctx)
        log.info(
            "sweep: %d repos, evaluated=%d not_evaluated=%d",
            result.repos,
            len(result.evaluated),
            len(result.not_evaluated),
        )
    except Exception as exc:
        log.exception("sweep: %s failed", run_id)
        _report_failure(f"sweep {run_id}", exc)


@app.post(
    "/invoke",
    status_code=202,
    response_model=InvokeAccepted,
    summary="Evaluate one repository",
    dependencies=[Depends(require_invoke_secret)],
)
def invoke(payload: InvokeRequest, background: BackgroundTasks) -> InvokeAccepted:
    """Accept one repository for evaluation."""
    # One context per request, built before anything reads the catalog.
    # It carries the catalog cache, so this request grades against the
    # release current when it arrived — in a long-lived process that
    # matters more than it did in a flow run, where a catalog fetched
    # once at boot would otherwise be graded against for the life of the
    # container however many releases went out meanwhile.
    ctx = RunContext.for_run()

    standards_version = _get_standards_version(ctx=ctx)
    run_id = payload.run_id or (
        _build_conformance_run_id(standards_version)
        if payload.mode == "llm"
        else _build_deterministic_run_id(standards_version)
    )

    event = EvaluationEvent(
        org=payload.org,
        repo=payload.repo,
        ref=payload.ref,
        services=({"id": payload.repo_id or payload.repo, "repo": payload.repo},),
        run_id=run_id,
        mode=payload.mode,
    )

    log.info(
        "invoke: accepted %s@%s (%s) as %s",
        event.repo,
        event.ref,
        event.mode,
        run_id,
    )
    background.add_task(_evaluate, event, ctx)
    return InvokeAccepted(run_id=run_id, repo=event.repo, mode=event.mode)


@app.post(
    "/sweep",
    status_code=202,
    response_model=SweepAccepted,
    summary="Evaluate every repository in the registry",
    dependencies=[Depends(require_invoke_secret)],
)
def sweep(payload: SweepRequest, background: BackgroundTasks) -> SweepAccepted:
    """Accept a whole-fleet pass.

    Minting the run id costs a catalog fetch, and the sweep will fetch the
    catalog again when it starts. That is deliberate rather than wasteful:
    a sweep accepted while a catalog release is in flight should grade
    against the version it actually runs under, not the one that happened
    to be current when the request arrived.
    """
    ctx = RunContext.for_run()

    standards_version = _get_standards_version(ctx=ctx)
    run_id = payload.run_id or (
        _build_conformance_run_id(standards_version)
        if payload.mode == "llm"
        else _build_deterministic_run_id(standards_version)
    )

    log.info("sweep: accepted (%s) as %s", payload.mode, run_id)
    background.add_task(_sweep, mode=payload.mode, run_id=run_id, ctx=ctx)
    return SweepAccepted(run_id=run_id, mode=payload.mode)


@app.get("/health", summary="Liveness")
def health() -> dict[str, Any]:
    """Liveness only. Deliberately does not reach the catalog or the API.

    A health check that fails when a dependency is down turns one outage
    into a restart loop, and this process has nothing to restart into.
    """
    return {"status": "ok"}


def main() -> None:
    """Run the adapter. Railway start command, once this is the entrypoint."""
    import sentry_sdk
    import uvicorn
    from dotenv import load_dotenv
    from mini_app_polis.environment import current_environment

    load_dotenv()
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN_EVALUATOR"),
        environment=current_environment().value,
    )
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 — the platform terminates and routes
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
