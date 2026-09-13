# 0004. Evaluation on demand, behind an HTTP handler

Date: 2026-09-13

## Status

Accepted. Supersedes [0001](./ADR-0001-parameterized-single-conformance-flow.md).

## Context

evaluator-cog ran as a single Prefect deployment on a daily cron, and that
deployment evaluated the whole fleet in one flow run. Three problems came
out of that shape rather than out of any bug in it:

- **The trigger is wrong.** A repository's conformance changes when that
  repository changes. Grading all thirteen at 09:00 means a release is
  graded up to a day late, and the twelve repositories that did not change
  are re-graded for nothing.
- **The unit is wrong.** One flow run is the whole fleet, so one repository
  that fails to download degrades a run covering twelve others, and the
  blast radius of any change to the loop is everything.
- **The runtime is a dead end.** The intended destination for this cog is
  a function invoked per request — Lambda-shaped — and a flow that reads a
  registry, iterates it, and carries fleet-wide state cannot be lifted into
  that without being rewritten anyway.

ADR-0001 solved a real constraint (the Prefect Hobby tier caps deployments
at 5) with a parameterized flow. That constraint stops applying the moment
the cog stops being a Prefect deployment.

## Decision

Make `handler(event) -> EvaluationResult` the unit of work: one repository,
everything it needs passed in, nothing looked up. Put transports in front
of it rather than logic around it.

- `POST /invoke` evaluates one repository. api-kaianolevine-com calls it
  when a repository's release job asks for an evaluation. The event is
  built from what CI already knows — repository, ref, org — and the
  repository's own `evaluator.yaml` supplies its type and exemptions. No
  registry read on this path.
- `POST /sweep` evaluates the whole registry, then runs the checks that
  scope to no repository at all (EVAL-003, MONO-003, EVAL-007 — ADR-004 in
  ecosystem-standards). It has no schedule. The two releases that
  invalidate every repository's last result at once — a new standards
  catalog and a new evaluator — are what call it.
- Rules arrive as a compiled catalog from
  `GET /v1/standards/catalog`, not as a checkout of ecosystem-standards.
- `railway.json` starts uvicorn. `src/evaluator_cog/main.py`, the Prefect
  deployment and its cron are deleted.

`run_llm: bool` becomes `mode: Literal["deterministic", "llm"]`, carried on
the event. ADR-0001's two behaviours survive intact; what goes away is the
deployment slot they were sharing.

## Consequences

- A release is evaluated in seconds by the thing that changed, and nothing
  else is evaluated at all. That is the point.
- The CI contract is fire-and-forget: CI posts to the API, reads a 202, and
  exits. The API awaits the evaluator's acknowledgement so a dropped job is
  knowable while someone is still on the line; the evaluation itself runs
  after the caller is gone.
- Prefect no longer knows this cog exists. Its failure hooks, run logger and
  concurrency primitive went with it: failures are reported by the adapter
  through `post_run_finding`, and `threading.Lock` serializes evaluations
  because the catalog, the delivery tally and the run report are module
  state. Both are correct for one container and neither is correct for two —
  the queue-plus-workers shape is what makes this horizontal.
- Several rules under `type: pipeline-cog` assume Prefect is how a cog runs
  (CD-015 most directly). They are exempted in `evaluator.yaml` with the
  reason stated. The standards are what should change; this repo is the
  proof of concept that motivates it.
- The Prefect Cloud deployment must be deleted by hand. Nothing serves it
  after this change, so leaving it in place produces a Late run every day
  that nobody is watching for.
