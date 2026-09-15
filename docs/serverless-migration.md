# Make evaluation reliable under burst, and able to idle at zero

**Goal:** give conformance evaluation durable retries and real concurrency, and
let the fleet's services stop holding memory while doing nothing.

**Target: SQS for the queue, Lambda for the workers.** The decision is made and
the reasoning is in "Why Lambda" below — do not relitigate it mid-migration.

**This retires Prefect.** The queue in step 2 replaces what Prefect is actually
being used for, and step 6 is the cleanup. See "What Prefect is doing today"
before starting, because two of its jobs need deliberate replacements rather
than deletion.

## Context

evaluator-cog no longer runs on a schedule. A repository's release asks the API
to evaluate it, the API hands the job to the evaluator, and `handler(event)` in
`flows/conformance.py` does the work — see
[ADR-0004](decisions/ADR-0004-evaluation-on-demand.md). That refactor left the
unit of work in the right shape and two properties still missing: a job cannot
be retried, and two jobs cannot run at once.

**The burst risk is loss, not latency.** Concurrent POSTs to `/invoke` each get
a 202 immediately and then queue as FastAPI `BackgroundTasks` in one process's
memory. A deploy, an OOM or a restart mid-burst silently discards every
accepted-but-unstarted job: no findings, no failure, no retry, and each
repository's record sits at its previous state looking healthy. That is the
same shape as the September incident the delivery assertions exist to catch, in
the one place they cannot see.

**The idle cost is a trigger problem, not a hosting problem.** 97% of the
Railway bill is memory; CPU is $0.24/month. Four cogs hold a container open to
poll Prefect Cloud every ten seconds. Anything that polls — Prefect Cloud, or a
queue — keeps a container awake and billed. Only a runtime where *the platform*
does the polling escapes that.

## What Prefect is doing today

Checked against the source of all four cogs. There is **no DAG**: no `.map()`,
no `.submit()`, no task runners anywhere. It is doing four jobs:

| Job | Replaced by | Deliberate? |
|---|---|---|
| **The queue.** watcher-cog calls `create_flow_run`; each cog's runner loop polls Prefect Cloud for it | Step 2 | direct replacement |
| **Task-level retries.** `retries=` / `retry_delay` — 27 occurrences in transcription-cog alone | `tenacity`, per call site | **yes — see step 4** |
| **Cross-process concurrency limits.** `prefect.concurrency` in transcription-cog and wiki-curator-cog | SQS consumer concurrency | **yes — see step 4** |
| **Run history and run identity** | `pipeline_evaluations` + the website, already built | already duplicated |

The fourth row is worth sitting with: `pipeline_eval` already posts run outcomes
to your own table with `source=flow_inline`, and the website renders them. You
are running two observability stacks and paying for one of them.

## Why Lambda

Three shapes were considered. Two were rejected for reasons worth recording,
because both look attractive until you push on them.

| | Inbound interface | Idles | Wake is durable | Feedback latency |
|---|---|---|---|---|
| Railway push-to-wake | yes | no | **no** | seconds |
| Railway cron-drain | none | no | yes | **up to 5 min** |
| **Lambda + SQS** | none | no | yes | seconds |

**Railway push-to-wake — rejected.** A lost ping — API restart between enqueue
and ping, a cold-start 502, a dropped connection — leaves the job sitting safely
in the queue with nothing ever running it. Durable work that might never
execute, looking healthy throughout.

**Railway cron-drain — rejected.** Simplest option on the table, and it very
nearly won. Two things sank it: Railway skips a scheduled run if the previous
one is still going, so one hung job halts all processing indefinitely unless you
write and maintain a watchdog; and the five-minute floor is a two-order-of-
magnitude regression on a release that is graded in about thirteen seconds.

**Lambda + SQS — chosen.** With an event source mapping, AWS polls the queue and
invokes the function. The polling still happens; it is simply not your container
doing it, so it is not your idle cost.

What this is **not** justified by: money. Idle cost is effectively zero either
way, and the addressable spend is ~$5–7/month out of a $22.71 bill. It is
justified by keeping the properties the refactor achieved while getting retry
semantics that are guaranteed rather than hand-maintained.

The honest remaining cost is a second cloud for a solo operator — two places to
look when something is wrong.

---

## Step 1 — Server-side idempotency (PIPE-002) — **done**

**Repo:** api-kaianolevine-com.

SQS is at-least-once, the release workflow already retries POSTs five times, and
a redelivered message reruns a completed job. All three produce duplicate
findings.

The deferral cited "complexity of constraining TEXT columns". The text is not
indexed; its digest is.

What shipped:

- `fingerprint` on `pipeline_evaluations` — SHA-256 over `(violation_id,
  dimension, severity, finding, suggestion)`, computed in the API
  (`services/evaluation_fingerprint.py`) and backfilled by migration 030 with
  the SQL equivalent. **Not** a generated column: `convert_to` is STABLE, so
  Postgres rejects the natural expression, and the suite runs on SQLite where a
  Postgres-only column could not be tested at all.
- Unique index on `(run_id, repo, fingerprint) WHERE run_id IS NOT NULL`, with
  the existing duplicates cleaned in the same migration. The rule id alone is
  not a key — CD-026 emits one finding per offending job.
- **A suppressed write answers 200 with `deduplicated: true`.** This is not
  cosmetic. `post_findings` counts a 2xx as a delivered finding, so dropping the
  row silently would have runs reporting findings as posted that were never
  stored — the September failure shape reached by a new route. `api_client`
  reads the flag and counts a duplicate instead of a post.

## Step 2 — SQS, with the worker still on Railway

**Repos:** api-kaianolevine-com, evaluator-cog. **The next thing to do.**

Replace the synchronous HTTP hand-off with the queue. The seam already exists:
`services/evaluation_dispatch.py::_dispatch()` is one function whose docstring
says it is meant to become an enqueue.

- **Producer:** the API sends to SQS. Needs `boto3` and SigV4 credentials in the
  Railway service — the one piece of cross-cloud coupling in this plan, and a
  credential to rotate.
- **Consumer:** evaluator-cog long-polls the queue instead of serving HTTP.

`adapters/http.py`, `EVALUATOR_INVOKE_URL`, `EVALUATOR_INVOKE_SECRET` and the
`X-Evaluator-Token` guard all come out, and with them the 502-on-dispatch branch
and the Cloudflare hop between two first-party services.

**The worker will not sleep during this step, and that is expected.** A
long-polling consumer has continuous outbound traffic, so Railway never
considers it idle. Step 5 is what fixes that. Do not add a watchdog, a cron, or
a sleep workaround here — it is temporary scaffolding and it comes out.

Size the queue for the other cogs too, not only evaluations: step 4 uses it. One
queue with a message type, or one queue per cog, is a judgement call — the
existing `DeejayMode` enum suggests a type discriminator is already natural.

**Done when:** killing the worker mid-burst loses no jobs; a job that fails
repeatedly lands in the DLQ; the evaluator accepts no inbound requests.

## Step 3 — Make the handler pure — **done, bar run identity**

**Repos:** evaluator-cog, common-python-utils.

`_EVALUATION_LOCK` existed because the catalog, the delivery tally, the run
report and the flagged set were module globals. Worth recording why the lock was
never sufficient: `/invoke` reset those globals from the *request* thread while a
previously accepted evaluation was still running under the lock, so a second
release arriving mid-evaluation wiped the first one's accounting without ever
contending for it. The lock serialised the work and not the state.

What shipped: a `RunContext` carrying tally, report, flagged set, unresolved
downloads and the per-run catalog, threaded as a required keyword-only argument
so a call site that forgets it is a `TypeError` rather than silent cross-talk.
`_reset_run_tally` and the lock are gone. `prefect` is out of `pyproject.toml`,
taking 70 transitive packages with it.

**Still outstanding:** `mini_app_polis.pipeline_status.get_run_id()` resolves the
Prefect flow run id, then `PREFECT_FLOW_RUN_ID`, then falls back to
`"local-run"` — and with Prefect gone it always falls back, so every run report
the evaluator sends is unattributable. The evaluator already mints a good id
(`deterministic-<version>-<uuid>`, the one findings are filed under); it needs a
way to hand that to `RunReport`. Bundle with the XSTACK-007 floor bumps.

## Step 4 — Convert the polling cogs

**Repos:** watcher-cog first, then deejay-cog, transcription-cog,
wiki-curator-cog.

Same change evaluator-cog already made: replace `prefect.serve()` with a queue
consumer so the process stops asking whether there is work.

**Start with watcher-cog. It is the keystone, not a peer.** It is the producer —
it calls `create_flow_run` to trigger the others. Point it at SQS and the other
three cogs' trigger source moves with it. Convert it last and you are running two
brokers in the meantime. Its own Drive poll becomes a scheduled invocation
rather than a resident process.

deejay-cog is closer to this than evaluator-cog was: `deejay_router(mode)` is
already `handler(event)` and `DeejayMode` is already the message schema.

Two of Prefect's jobs must be **replaced, not dropped**, as each cog converts:

- **Task-level retries.** The queue retries the *job*; Prefect retried a *task
  inside* the job. Re-running an entire transcription because one API call
  flaked is a materially worse trade. Wrap those call sites in `tenacity` as you
  remove `@task(retries=…)`.
- **Concurrency limits.** `prefect.concurrency` is a fleet-wide semaphore. SQS
  gives the same thing through consumer concurrency, but only if you set it.
  Check what each existing limit was protecting before removing it.

**Done when:** no cog polls Prefect Cloud, and `create_flow_run` has no callers.

## Step 5 — Workers move to Lambda

The AWS account, queue, IAM and deploy pipeline are built —
[aws-foundation.md](aws-foundation.md) and `infra/`. Each consumer becomes a
Lambda behind an SQS event source mapping. `handler()` does not change; this is
packaging and deployment.

- **A zip, not a container image.** Measured after dropping `prefect`: 25.8 MB
  zipped against a 50 MB limit, 136 MB unzipped against 250 MB. An earlier draft
  of this document asserted the tree was past the zip limit; it is not. Skipping
  ECR removes a registry, a build-and-push step and an entire class of "which
  image is actually deployed" confusion.
- 100 MB of that 136 MB is `googleapiclient`, pulled in by
  `miniapppolis-common-utils` and never imported by the evaluator. Behind a
  `[google]` extra the package is 8.2 MB zipped. Worth doing for every cog's
  cold start, not just this one.
- GitHub Actions with OIDC to AWS, so CI holds no long-lived keys. Terraform
  owns the function's configuration and CI owns only its code.
- Concurrency limits per queue, replacing the semaphores from step 4. Note a new
  account cannot set reserved concurrency at all until the Lambda concurrency
  quota is raised above 100.
- Timeout above the slowest observed job with headroom: one repository is ~13s, a
  16-repo sweep is ~46s. Currently 300s, which means a poison message takes ~17
  minutes to reach the DLQ — shorten it if failures should escalate faster.

**Done when:** no cog runs a resident process, and the Railway project holds only
api-kaianolevine-com and Postgres.

## Step 6 — Retire Prefect

Once step 4 is done, nothing creates a Prefect flow run, so nothing Prefect
offers has a subject. This is cleanup, not a decision.

- **The webhook path is dead.** `pipeline_eval.handle_prefect_flow_run_event`
  observes Prefect Cloud state changes. Remove it and the Prefect Cloud
  automation, and mark `source=prefect_webhook` legacy in
  `website-astro-software/src/lib/sources.ts` alongside `conformance_check`.
  Existing rows keep the value.
- **common-python-utils:** `serve_resilience.serve_with_retry` has no callers.
  `pipeline_status.get_prefect_logger` and the Prefect branch of `get_run_id`
  become dead paths.
- **Standards:** CD-015 (serve pattern), CD-016 (`serve_with_retry`), PIPE-004
  (concurrency guard), PIPE-006 (dual logger) and PIPE-015 (trigger
  architecture) encode Prefect as an architectural assumption. Five cogs
  carrying the same exemption is the signal to retire or rescope the rules, not
  to write the exemption five times.
- **Docs:** `evaluator-cog/docs/PREFECT_AUTOMATION.md`.
- **The `prefect` dependency** in each converted cog's `pyproject.toml`.
- **The Prefect Cloud account itself** — a cost line and a dependency.

## Constraints that will bite

- The shared release workflow retries the evaluation POST five times with a five
  second delay. Step 1 is what makes that safe.
- **Job retries are not task retries.** See step 4. The single most likely thing
  to be quietly lost in the conversion.
- **deejaytools-com is a monorepo** and is deliberately not wired for
  self-evaluation. `/invoke` carries one service, and ADR-0002 sibling
  deduplication needs every app in the workspace in the same event. The sweep
  builds its event correctly. Do not "fix" this by sending two requests.
- **A fleet pass must keep one `run_id` across all repositories.** The website's
  latest-run filter (`website-astro-software/src/lib/latest-run.ts`) keys on
  `(repo, cluster)` and relies on a sweep's findings belonging to one run graded
  against one catalog version. `EvaluationEvent.run_id` and
  `EvaluationRunRequest.run_id` already accept a caller-supplied id.
- CD-026's canonical job set is `security, test, release, evaluate`.
- Do **not** build a fan-in coordinator for the sweep as part of this. It stays a
  loop until steps 1–4 are done. When it is finally fanned out, EVAL-007 needs
  nothing from the run (move it to the standards-release trigger), EVAL-003
  grades the table rather than the run (make it periodic), and only MONO-003
  needs completion — which it can satisfy by grading the previous run rather than
  racing the current one.

### Settled, and no longer a risk

- **Cloudflare does not challenge AWS egress.** The stub Lambda's probe got 200
  and JSON from `api.kaianolevine.com`, not a challenge page. No WAF rule is
  needed. This was the constraint flagged as most likely to bite.
