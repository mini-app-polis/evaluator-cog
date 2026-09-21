# Make evaluation reliable under burst, and able to idle at zero

**Goal:** give conformance evaluation durable retries and real concurrency, and
let the fleet's services stop holding memory while doing nothing.

**Target: SQS for the queue, Lambda for the workers.** The decision is made and
the reasoning is in "Why Lambda" below — do not relitigate it mid-migration.

**This retires Prefect.** The queue in step 2 replaces what Prefect is actually
being used for, and the final step is the cleanup. See "What Prefect is doing
today" before starting, because two of its jobs need deliberate replacements
rather than deletion.

**evaluator-cog and deejay-cog are done.** Both run on Lambda behind an SQS
event source mapping, hold no resident process, and have no Prefect.
What each slice left behind is in "After the first slice" and "After the
second slice" at the end — read both before starting the next cog. The
second supersedes parts of the first: the producer, the build and the
Terraform workflow are shared or scripted now, not copied.

**One cog at a time, all the way to Lambda.** The plan originally moved the
whole fleet onto queues and then moved every worker to Lambda in one wave. It no
longer does; see "Why vertical slices" below. Steps 1–3 were fleet-wide
groundwork and are done. Everything after them is per-cog.

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
| **The queue.** watcher-cog calls `create_flow_run`; each cog's runner loop polls Prefect Cloud for it | The per-cog queue | direct replacement |
| **Task-level retries.** `retries=` / `retry_delay` — 27 occurrences in transcription-cog alone | `tenacity`, per call site | **yes — see the per-cog checklist** |
| **Cross-process concurrency limits.** `prefect.concurrency` in transcription-cog and wiki-curator-cog | SQS consumer concurrency | **yes — see the per-cog checklist** |
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

## Why vertical slices

The original plan finished every cog's queue conversion before starting on
Lambda. Two things changed.

**The reason for that ordering was watcher-cog, and watcher-cog is being
retired.** The argument was that watcher is the producer — it calls
`create_flow_run` to trigger the other three — so converting it last meant
running two brokers in the meantime. That is still true of the *trigger*, but
the trigger is not moving to a queue consumer any more. It is being replaced
outright; see "Retiring watcher-cog".

**A horizontal wave defers every Lambda unknown until four cogs are committed to
it.** The packaging, the entrypoint, the cold starts, the concurrency quota —
none of that is exercised by converting a cog to a Railway queue consumer. Doing
one cog end to end means those are answered on the cog whose code you know best,
and a wrong answer costs one cog to unwind rather than four.

What does **not** fit in a slice is the final step. Prefect cannot be retired
until the last cog is off it, so that stays terminal.

**Order:** evaluator-cog first (done), then deejay-cog (done) — its
`deejay_router(mode)` is already `handler(event)` and `DeejayMode` is already
the message schema — then transcription-cog and wiki-curator-cog.

watcher-cog is not in that list because it is replaced rather than converted,
but one piece of it comes first: its trigger stops being
`create_flow_run` and becomes a POST to the API, which is what gives the
three downstream cogs a producer at all. That is a single call site, and it
takes Prefect out of watcher-cog as a side effect. Its Drive polling is
replaced later and blocks nothing.

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

## Step 2 — SQS, with the worker still on Railway — **done**

**Repos:** api-kaianolevine-com, evaluator-cog.

The synchronous HTTP hand-off is gone. `services/evaluation_dispatch.py`
enqueues; `adapters/queue.py` long-polls and does the work. `adapters/http.py`,
`EVALUATOR_INVOKE_URL`, `EVALUATOR_INVOKE_SECRET` and the `X-Evaluator-Token`
guard all came out, along with the 502-on-dispatch branch and the Cloudflare hop
between two first-party services.

**One queue per cog — settled, and not a judgement call.** An earlier draft left
this open ("one queue with a message type, or one queue per cog"). It is not
open: SQS has no selective receive. A consumer takes whatever it is handed, so
on a shared queue evaluator-cog's consumer would receive a `transcription.run`
message, fail to recognise the type, and its redrive policy would put *another
cog's job* in evaluator's dead-letter queue. Every consumer would do that to
every other consumer's work, and which one loses is a race. `infra/` already has
this right — `name_prefix` defaults to `evaluator` and produces
`evaluator-jobs`, one prefix per cog.

The type discriminator stays anyway. An unrecognised type on a cog's *own* queue
means a producer bug, and dead-lettering it deliberately beats guessing.

**The worker does not sleep during this step, and that is expected.** A
long-polling consumer has continuous outbound traffic, so Railway never
considers it idle. Moving to Lambda is what fixes that. Do not add a watchdog, a
cron, or a sleep workaround — it is temporary scaffolding and it comes out.

**Done:** killing the worker mid-burst loses no jobs; a job that fails
repeatedly lands in the DLQ (watched end to end, ~17 minutes for three
receives); the evaluator accepts no inbound requests.

## Step 3 — Make the handler pure — **done**

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

Run identity is closed too. `pipeline_status.get_run_id()` resolved the Prefect
flow run id and, with Prefect gone, always fell back to `"local-run"` — so every
run report the evaluator sent was unattributable. `RunReport.run_id` (common-utils
5.10.0) now takes the id the evaluator already mints, set in both
`run_fleet_sweep` and `handler`. **Both repos need the `>=5.10.0` floor**: with
an older lock the assignment is a silent no-op, which is how it was first missed.

---

## The per-cog slice

Each cog does all of this before the next one starts.

### 0. Build the producer, in api-kaianolevine-com

**Every producer goes through the API.** No cog enqueues for another cog,
and nothing else holds a key. The API is the single sending identity for
the fleet, which is why `create_api_producer` exists (below) and why its
policy is a wildcard over `*-jobs`.

This step is first, and the ordering is not stylistic. Convert a consumer
before something sends to its queue and the cog goes silent: watcher-cog is
still calling `create_flow_run` at a `prefect.serve()` that no longer
listens, nothing errors, and the work simply stops happening. The producer
must exist **before** the cutover, mirroring the rule on the other side.

What to build, per cog:

- A dispatch function in `services/`, alongside `evaluation_dispatch.py`.
  It is the same shape every time: build the message, `asyncio.to_thread`
  the blocking boto3 call, insist on a `MessageId`, and report to the
  errors channel if it did not land.
- Nothing to configure for the queue. `services/job_queue.py` derives it
  from the cog name and the API's environment — see "After the second
  slice".
- The route or webhook that calls it.

**The message envelope is fixed and both sides must agree:**

```json
{"type": "<cog>.<what>", "version": 1, "payload": { ... }}
```

`MESSAGE_VERSION` is checked by the consumer, which refuses a version it
does not speak rather than misreading it — that is what makes a
producer/consumer redeploy safe. The `type` discriminator is a
producer-bug detector, not a router: one queue per cog means an
unrecognised type is something enqueued wrongly, not another cog's
traffic. `MessageAttributes` carries the type as well, so a metric filter
or a console view can read it without parsing the body.

**What is evaluator-specific and does not generalise:** `fleet_registry`,
`dispatch_fleet`, the introspection endpoint and the fan-out. Those exist
because the evaluator's unit of work is "a repository" and the fleet is a
list of them. A transcription job has no equivalent.

**watcher-cog calls the API in the middle step.** Their trigger today is
watcher calling `create_flow_run` with a pinned mode, and the replacement
for that is one HTTP POST — not the whole Drive-push rebuild.

`prefect_trigger.fire(deployment_id, parameters=…)` becomes a POST to the
API, which enqueues onto the named cog's queue. `deployment_id` was
already a per-folder constant and `parameters` was already the payload, so
the static map in watcher's `config.py` is the routing table more or less
as it stands.

Two things fall out, and the second is the reason to do this first:

- The three cogs get a producer without waiting for anything.
- **`prefect_trigger.py` is watcher-cog's only use of Prefect.** It serves
  no deployments — it is a Drive poller with one `get_client()` call.
  Replacing it drops `prefect` from its `pyproject.toml` entirely, ~70
  transitive packages with it — **but only once every cog it triggers has
  an API route.** This section first said the deejay change would do that;
  it does not, because `wcs-notes` and `voice-notes` still fire
  transcription-cog's Prefect deployment. `WatcherConfig` takes exactly one
  of `api_path` or `deployment_id`, and Prefect leaves watcher with
  transcription-cog.

Watcher keeps polling Drive from a resident container after this, and
keeps costing what a resident container costs. That is what "Retiring
watcher-cog" finishes, and it is a separate piece of work with its own
prerequisites — a WAF rule, persistent page-token state, a renewal job —
none of which block a cog conversion.

### 1. Convert the consumer

Replace `prefect.serve()` with a queue consumer so the process stops asking
whether there is work. The message is not deleted until the work is done — that
single rule is what the queue buys.

**Three `RunReport` traps, all of which cost time on the first cog.** They are
in Constraints too; they are here because this is where they bite.

- **Set `report.run_id` explicitly.** `get_run_id()` resolves the Prefect flow
  run id and falls back to `"local-run"` — and with Prefect gone it always
  falls back, so every run report is unattributable. Nothing raises. It was
  found by reading a Discord message that said `run local-run` next to fifteen
  that said otherwise.
- **`send()` is once per instance.** The second call returns
  `DeliveryReport(suppressed=1)`, so whichever caller sends first spends it.
  That makes *placement* load-bearing: a report built inside the per-item
  handler is one per item, and a job that also has its own summary loses it.
  Put the per-job report in the adapter's message branch, not in the handler.
- **Counter keys are keyword arguments.** `send()` ends with
  `**self.counters`, so `count("repo", …)` collides with `post_run_finding`'s
  own `repo` parameter and is a `TypeError` at send time — on a path a green
  test suite never walks.

Two of Prefect's jobs must be **replaced, not dropped**:

- **Task-level retries.** The queue retries the *job*; Prefect retried a *task
  inside* the job. Re-running an entire transcription because one API call
  flaked is a materially worse trade. Wrap those call sites in `tenacity` as you
  remove `@task(retries=…)`.
- **Concurrency limits.** `prefect.concurrency` is a fleet-wide semaphore. SQS
  gives the same thing through consumer concurrency, but only if you set it.
  Check what each existing limit was protecting before removing it.

### 2. Stand up the cog's own infrastructure

Copy `infra/` and set `name_prefix`. Two things to get right:

- **`create_github_oidc_provider = false` for every cog after the first.** There
  is one OIDC provider per account; a second `terraform apply` fails on a
  resource that already exists.
- **`create_api_producer = false` for every cog after the first.** There is
  one API, so there should be one IAM user for it holding one access key.
  Its policy is a wildcard over `*-jobs`, so a new cog's queue is covered
  the moment it exists — no cross-state reference, and nothing to remember
  to widen. Leave it default-true and by the fifth cog the API carries five
  credentials, five Doppler entries and five client configurations all
  saying the same thing.
- **There is no stub worker and no flag that disables the consumer.** Both
  existed, and both are gone. evaluator-cog stood its Lambda up beside a
  Railway container that was already reading the same queue, so it needed a
  stub to prove the wiring and a `worker_consumes_queue` toggle to keep the
  stub from racing the container. The stub won those races — it logged,
  probed the API, returned success, and SQS deleted the message. Queue
  empty, DLQ empty, no evaluation, indistinguishable from a job never
  enqueued. Five real evaluations went that way.

  No cog after this one has that problem. They move from Prefect to SQS, so
  the queue is created with exactly one reader and never has another, and
  there is nothing for a stub to prove that this cog has not already
  proven — see "Settled, and no longer a risk". The function is created
  holding a placeholder that cannot import, so an invocation before the
  first deploy dead-letters instead of succeeding, and the mapping is
  simply on.

Doppler names must be distinct per cog. The fleet shares one secrets store, so
`AWS_ACCESS_KEY_ID` in two services is a collision; use
`<COG>_QUEUE_CONSUMER_KEY_ID` and friends, with explicit boto3 credentials and a
default-chain fallback.

Producer and consumer keys are separate and must stay that way. A consumer that
can `SendMessage` on its own queue can enqueue its own work and loop.

### 3. Move the worker to Lambda

`handler()` does not change; this is packaging and deployment.

- **Write the Lambda entrypoint.** `process_message` was built for it — "shared
  with the Lambda entrypoint, which differs only in where the body comes from
  and who deletes it afterwards" — but nothing calls it from a `lambda_handler`
  yet. The event source mapping is already configured with
  `ReportBatchItemFailures`, so the entrypoint must return that shape.
- **A zip, not a container image.** An earlier draft asserted the tree was past
  the 50 MB direct-upload limit; it is not, and was not even before the strips
  below. Skipping ECR removes a registry, a build-and-push step and an entire
  class of "which image is actually deployed" confusion.
- **Exclude boto3 from the zip.** The runtime provides it, and bundling a copy
  that is then shadowed spends most of the headroom: 41.5 MB with it against a
  50 MB limit.
- **Strip the Google stack from the zip, not from common-utils.** Measured on
  the real package: 150 MB unzipped and 28.4 MB zipped with it, 42 MB and
  15.3 MB without. evaluator-cog imports none of it — no
  `mini_app_polis.google`, no `GoogleAPI`, no `googleapiclient` — and the
  library's lazy `__init__` means nothing reaches it transitively.

  Measured end to end on evaluator-cog, both strips applied: **15.3 MB
  zipped, 42 MB unzipped**, against limits of 50 MB and 250 MB.

  A `[google]` extra on common-utils is the right end state and the wrong
  move now: `google-api-python-client` is an unconditional dependency, so
  watcher, transcription, wiki-curator and deejay all get it free and would
  fail at import without declaring the extra. That is a major version and a
  coordinated release across five repos, four of which are still on Prefect.
  It belongs at the end of the per-cog migrations, when every consumer
  already declares what it needs.

  The strip is only correct while nothing imports what it removes, which is
  a property of the code rather than of the workflow — so the deploy build
  imports every module against the built package and fails if any of them
  needs what was stripped.
- GitHub Actions with OIDC to AWS, so CI holds no long-lived keys. Terraform
  owns the function's configuration and CI owns only its code. Note
  `lambda:GetFunctionConfiguration` is a **separate IAM action** from
  `GetFunction`, and `aws lambda wait function-updated` polls the former.
- **Retire the old trigger in the same change that points the new one at the
  API.** For these cogs the queue's consumer is never in question; what
  overlaps is the *trigger*, a Prefect schedule or a watcher call still
  firing while the API also enqueues. That duplicates work rather than
  losing it, which is the mild version — but it is still a cutover, not an
  overlap. Prefer a gap: stopping the old path first only delays events.
- Timeout above the slowest observed job with headroom: one repository is ~13s,
  a 16-repo fleet pass is ~46s. Currently 300s, which means a poison message
  takes ~17 minutes to reach the DLQ — shorten it if failures should escalate
  faster.
- A new account **cannot set reserved concurrency at all** until the Lambda
  concurrency quota is raised above 100. AWS refuses to reserve if doing so
  would leave the account with fewer than 100 unreserved, and the error names
  `UnreservedConcurrentExecution` rather than anything that sounds like a quota.

**Cog done when:** it runs no resident process, and its queue, DLQ, alarm and
deploy pipeline are its own.

---

## Retiring watcher-cog

watcher-cog is **replaced, not converted** — but in two stages, and only the
second is this section.

**Stage one, which belongs with the first cog conversion:** swap
`prefect_trigger.fire()` for a POST to the API. See step 0 of the per-cog
slice. That gives the three downstream cogs a producer and takes Prefect
out of watcher-cog altogether, while leaving its Drive polling exactly as
it is.

**Stage two is the rest of this section:** replacing that polling, which is
what stops the resident container.

It does two jobs that were bundled together, and only one of them is
Prefect's:

- **Noticing a Drive folder changed.** Four infinite loops, one per folder.
  Nothing about SQS replaces this.
- **Turning that into a trigger with a mode.** `prefect_trigger.fire()` calls
  `create_flow_run_from_deployment` with parameters that pin the dispatch mode —
  `{"mode": "process-new-files"}` at deejay-cog's router, `{"mode":
  "wcs-transcripts"}` and `{"mode": "voicenotes"}` at transcription-cog's. This
  becomes a `SendMessage`, and the static `folder_id → deployment_id + parameters`
  map in `config.py` is already the message schema.

The replacement is **Drive push notifications → an API webhook → the cog's
queue**. Constraints, confirmed against Google's documentation:

- Channels expire. Maximum TTL is 604800s (one week) for the `changes` resource,
  86400s for `files`, and there is **no automatic renewal** — you call `watch`
  again with a fresh channel id before it lapses. So this is not zero scheduled
  jobs; it is one renewal a week in place of a per-minute poll per folder. Say
  that plainly rather than "no polling", or the next reader wonders why there is
  a cron.
- **Notifications carry no payload.** "Push notifications don't contain resource
  metadata, content, or directory paths." You get a channel id and a resource
  state, then call `changes.list` with a saved `pageToken`. That token is
  persistent state, and it now lives in the API — watcher held its position in
  memory in a resident process.
- `changes.watch` is drive-wide, not per-folder, so watcher's static folder map
  becomes parent-folder filtering in the API.
- `X-Goog-Channel-Token` is set at watch time and returned on every
  notification; that is the webhook's authentication, not a key in the URL.
  `X-Goog-Message-Number` increments per channel and is the dedup key.
- The endpoint must answer 200/201/202/204/102 over HTTPS with a valid
  certificate.

**Check Cloudflare before writing any of it.** The settled finding below is that
Cloudflare does not challenge *AWS* egress — that was measured. Google's webhook
senders are a different source range and have not been tested. A challenge page
is a non-2xx, Drive reads that as failed delivery, and the symptom is a folder
that silently stops triggering — a failure that looks like nothing
happening, which is the shape worth fearing.

---

## Fan-out replaces the sweep

`run_fleet_sweep` handed the evaluator one message and let it read the registry
and work through the fleet serially. The API now fans out instead: one
`evaluation.repository` message per repository, from
`POST /v1/evaluations/fleet`.

**Why**, honestly stated — because one of the original arguments for this was
wrong. A serial pass does *not* strain Lambda's fifteen-minute ceiling; it is
~46 seconds. What fan-out actually buys is failure granularity and parallelism:
one repository failing retries alone, instead of redelivering a pass that
re-evaluates the fifteen repositories that already succeeded.

Three things the sweep got for free and the fan-out arranges deliberately:

- **One `run_id` across the pass**, minted at dispatch. The website's latest-run
  filter relies on a pass's findings belonging to one run.
- **One pinned `standards_version`**, resolved once from `standards_catalogs`.
  N jobs each resolving their own would let a catalog release landing mid-pass
  grade some repositories against the old rules and some against the new, inside
  a run id claiming one version for all of them. **This reverses the reasoning
  behind `ctx.catalog = None` in `run_fleet_sweep`**, which argues a pass must
  grade against the version it actually runs under. That was correct for a pass
  accepted as one unit; it is wrong for N independent messages.
- **The monorepo grouping travels in the message.** A monorepo is one job
  carrying every app in it. Flatten it and `monorepo_root`, the workspace
  `package.json` and `monorepo_context` all resolve to `None`, and
  `_deduplicate_sibling_findings` never fires — it is gated on a job carrying
  more than one service. Nothing raises. The findings post, the run reports
  success, and the same finding lands once per app looking like a deduplication
  bug in the evaluator.

The roster comes from the API. Today that is `ecosystem.yaml` fetched behind a
five-minute cache in `services/fleet_registry.py`, which is a down payment on
the API owning the registry rather than an end state — when it does, only the
fetch changes. The grouping is **ported from `_fleet_events`** rather than
rewritten, so the two agree while both exist; when `run_fleet_sweep` is retired,
that one deletes and the API's remains.

`/v1/evaluations/sweeps` and `run_fleet_sweep` are **gone**. They stayed live
until the fan-out had run a real pass — 15 jobs under one run id, with
deejaytools-com arriving grouped — and were deleted once it had. The four
registry helpers only `_fleet_events` called went with them; that
translation now exists once, in the API.

`_ping_healthcheck` moved to `run_introspection` in the same change. It was
called from the sweep's tail and nowhere else, and Healthchecks.io watches
for *absence* — deleting the sweep without rehoming it would have stopped
the pings and reported the evaluator dead at the moment it started working
properly.

### The checks that do not fan out

EVAL-003, MONO-003 and EVAL-007 are scoped to no repository at all (ADR-004).
They grade the inventory, the stored findings and the catalog itself, so there
is no per-repository message they belong to — and two of them read
`pipeline_evaluations`, which the sweep guaranteed by running them after its
loop. Fan-out has no "after".

**Decision: their own endpoint, called rather than scheduled.** A small second
message type that does one thing, dispatched deliberately, rather than a cron or
a completion barrier. An earlier draft of this document reached a per-check
version of the same answer that is still worth keeping as detail: EVAL-007 needs
nothing from the run and belongs on the standards-release trigger; EVAL-003
grades the table rather than the run; only MONO-003 wants completion, and it can
satisfy that by grading the previous pass rather than racing the current one.

Do **not** build a fan-in coordinator. Tracking N acks to fire three checks adds
distributed-completion state to the API, and a stuck message means the checks
never run at all.

---

## After the first slice

What evaluator-cog's migration left behind, and what the next cog inherits.

**A live credential that now consumes nothing.** `infra/consumer.tf` creates
an IAM user whose access key existed for the Railway container, and that
container is gone — the Lambda authenticates with its execution role. In a
design whose point was minimising standing credentials, this is the one that
should not exist. Delete the access key **first**: `aws_iam_user.consumer`
has no `force_destroy` and the key was minted by hand, so `apply` fails with
`DeleteConflict: Cannot delete entity, must delete access keys first`. Then
remove `consumer.tf`, apply, and drop the two Doppler secrets. A later cog
that keeps a container consumer needs this file; one that goes straight to
Lambda does not.

**The container consumer is deleted, not parked.** `main()`, the poll loop
and the SIGTERM handler are gone from `adapters/queue.py`, along with
`railway.json`. Rolling back to a container is a rewrite rather than a
restart — deliberately, because two consumers on one queue is the failure
that cost five evaluations, and keeping a second one runnable is how that
happens by accident. `nixpacks.toml` went with it — API-001 is the only
rule that reads it, and it gates on `is_api_service`, which a pipeline-cog
is not.

**The throttle is `scaling_config`, not `reserved_concurrency`.** AWS refuses
to reserve if it would leave the account under 100 unreserved, and this
account is below that. `maximum_concurrency` on the event source mapping
needs no quota and is what actually limits a fleet pass today.

`TODO(lambda-quota)` in `infra/variables.tf` tracks the increase. The
trigger for doing it is **the second cog going to Lambda**, not a date:
what a reservation adds over a mapping ceiling is guaranteed capacity
rather than a cap, and that only matters once two workers compete for the
same account pool.

**Deploys run on release.** `deploy-worker.yml` was manual while the mapping
was disabled, because a deploy changed what *would* run. Once the function
is what actually evaluates, manual deploys mean the deployed code drifts
behind main silently.

Call the deploy from the CI workflow as a job after `release`; do not
trigger it with `on: release`. semantic-release publishes with
`GITHUB_TOKEN`, and GitHub starts no workflows from that token's events, so
a release trigger never fires. evaluator-cog shipped that way, and kept
running manually-deployed code across releases until a finding's wording
gave it away. Anything that must run on the new code —
the fleet sweep here — then `needs:` the deploy job instead of sleeping.

**What is a template now.** The Lambda entrypoint's `batchItemFailures`
handling and `infra/` with `name_prefix` are copied. The producer and the
build are not copied any more — they are shared:

- **The API side is `services/job_queue.py`.** A cog's dispatcher builds
  its message and names its cog; the queue URL is derived from the cog and
  the API's environment (`<cog>-jobs` in production, `<cog>-dev-jobs`
  elsewhere), so there is no `<COG>_QUEUE_URL` to configure.
- **Test and deploy are `python-test.yml` and `lambda-deploy.yml` in
  mini-app-polis/.github**, called from `ci.yml` with the handler,
  architecture and runtime from `infra/worker.tf`. The copied
  `deploy-worker.yml` chose wheels for the GitHub runner and checked imports
  on the runner: deejay-cog's first deploy passed that check and failed at
  import on Lambda, and this cog had been shipping x86_64 builds of
  pydantic-core and cryptography to its arm64 function without tripping it.

In `infra/`, `create_github_oidc_provider`, `create_api_producer` and
`create_account_budget` are all false for every cog after the first —
deejay-cog's copy defaults them so, and is the better copy to start from.

## After the second slice

What deejay-cog's migration left behind. Most of it was learned by
breaking something on the day, so each item says what broke.

**Start from deejay-cog's `infra/`, not this repo's.** Account-level
resources default off, variables validate their shape, and Terraform runs
through `infra/tf`. A new cog changes `name_prefix`, the `SECRETS` list in
`tf`, the environment block in `worker.tf`, and the handler.

**Terraform runs through `./tf`, never bare.** It reads the cog's secrets
from Doppler (`doppler setup` once per directory), normalises the Google
key, refuses a `terraform.tfvars` that would override a secret, and never
lets Terraform prompt. The first apply shipped `terraform.tfvars.example`'s
placeholders — an API key of `...` and a 36-byte stub for the Google JSON —
and the function failed every run while its failure report 401'd on the
same key. The hand routes tried next lost the value to the clipboard and to
an `unset` pasted in the same block. evaluator-cog should adopt `tf` the
next time its infra is touched.

**Queues are derived, not configured.** `job_queue.queue_name(cog)` is
`<cog>-jobs` in production and `<cog>-dev-jobs` anywhere else, resolved
through `current_environment()` so an alias like `prod` cannot misroute.
The development API had been holding production's `evaluator-jobs` URL, so
dev-triggered evaluations ran in production. A dev stack, when wanted, is
the same `infra/` applied with `name_prefix = "<cog>-dev"`; the producer's
`*-jobs` wildcard already covers it.

**The watcher's API trigger is gated to production**, like its Prefect
trigger. A development watcher polls production's Drive folders, so
anything it fires is a second trigger for production's uploads — whatever
the API it reaches does with it. It reports "Would trigger" instead.

**Build for the runtime, test in the runtime.** The shared
`lambda-deploy.yml` installs wheels for `<arch>-manylinux_2_17` (python3.11
is Amazon Linux 2, glibc 2.26), fails if any library needs newer glibc, and
imports every module and probes the handler inside
`public.ecr.aws/lambda/python:<version>`. deejay-cog's first zip was built
for the Ubuntu runner, passed an import check run on the runner, and failed
at import on Lambda with `GLIBC_2.28 not found`.

**Pass a run id everywhere.** The worker passes the SQS message id into the
flow's `RunReport`; watcher passes the trigger's message id with its
"Triggered" report, so both carry the same id. Anything left to
`get_run_id()` arrives as `local-run`.

**INFO logging on Lambda needed a common-utils fix.** The runtime installs
a root handler at WARNING before any import, so `logging.basicConfig` in
`mini_app_polis.logger` was a no-op and every INFO line a cog wrote was
dropped, while httpx's — explicitly levelled — still printed. The shared
logger now carries `LOGGING_LEVEL` itself. Cogs pick it up on their next
lock update.

**Retries that were never retries.** deejay-cog's three `@task(retries=2)`
all decorated functions that catch every exception, so Prefect never
retried them. Check that before porting a retry to `tenacity`: the count of
`retries=` is not the count of retries that ever happened.

**Serialisation needs the Lambda quota.** deejay's sweep must run one at a
time; the event source mapping's floor is two. The quota increase to 1,000
was requested on 2026-09-21; once approved, set `reserved_concurrency = 1`
and `max_receive_count = 5` (throttled deliveries count as receives), with
`worker_timeout_seconds = 300` — a one-file run measured 14–15 s.

## Retire Prefect

Terminal. Once the last cog is converted, nothing creates a Prefect flow run, so
nothing Prefect offers has a subject. This is cleanup, not a decision.

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
  to write the exemption five times. Note evaluator-cog's own
  `engine/deterministic/` still *grades other repos* on these — those checks
  change meaning here rather than disappearing.
  *Done after the second slice:* ecosystem-standards ADR-009 redefines
  `pipeline-cog` as a Lambda behind its own queue. PIPE-001, PIPE-004,
  PIPE-006, PIPE-009, PIPE-012, CD-005, CD-015 and CD-016 are retired, their
  checks deleted here; PIPE-016–019 grade the new runtime from `infra/*.tf`,
  PIPE-007 asks for call-site retries without Prefect, and CD-010 and CD-024
  read a pipeline cog's alarm and limits from `infra/`. PIPE-015 is rescoped
  to the API → queue path.
- **Docs:** `evaluator-cog/docs/PREFECT_AUTOMATION.md`.
- **The `prefect` dependency** in each converted cog's `pyproject.toml`.
- **The Prefect Cloud account itself** — a cost line and a dependency.

## Constraints that will bite

- The shared release workflow retries the evaluation POST five times with a five
  second delay. Step 1 is what makes that safe.
- **Job retries are not task retries.** The single most likely thing to be
  quietly lost in a conversion.
- **deejaytools-com is a monorepo** — `deejaytools-com-api` and
  `deejaytools-com-app` — and is deliberately not wired for self-evaluation.
  `/invoke` carried one service, and ADR-0002 sibling deduplication needs every
  app in the workspace in the same event. Do not "fix" this by sending two
  requests. It is also the one repository that would expose a fan-out grouping
  bug, and a repository count will not show it: 16 active services group into 15
  jobs, so a count of 16 is itself the tell.
- **A fleet pass must keep one `run_id` across all repositories.** The website's
  latest-run filter (`website-astro-software/src/lib/latest-run.ts`) keys on
  `(repo, cluster)` and relies on a pass's findings belonging to one run graded
  against one catalog version.
- **`RunReport` counter keys collide with `post_run_finding`'s signature.**
  `send()` ends with `**self.counters`, so `count("repo", …)` is a `TypeError`
  at send time — on a path a green suite never walks. Worth a guard in
  common-utils before the next cog converts.
- **A run report is once per instance.** `RunReport.send` returns
  `DeliveryReport(suppressed=1)` on a second call, so whichever caller sends
  first spends it. This is why the per-repository outcome report lives in the
  consumer's message branch and not in `handler()`, which a fleet pass also
  calls.
- **A false-by-default flag on a live path is a loaded gun.**
  `worker_consumes_queue` defaulted to false and was passed with `-var`; an
  apply that forgot it disabled the mapping. Nothing raised — a queue with
  no consumer is not an error — so jobs accumulated, releases stayed green,
  and the first sign was somebody noticing evaluations had stopped. It
  happened on the apply that added the concurrency ceiling, hours after the
  cutover. Pinning it in `terraform.tfvars` fixed the immediate hazard; the
  variable has since been deleted, which fixes it properly. When a switch
  has exactly one correct setting, it should not be a switch.
- **A build-time strip is not a dependency.** The Lambda zip drops the Google
  stack because nothing imports it. That stays true only while it stays true,
  which is why the deploy build imports every module before uploading. An
  `ImportError` there is the guard working; an `ImportError` at invocation
  means the guard was removed.
- CD-026's canonical job set is `security, test, release, evaluate`.

### Settled, and no longer a risk

Each of these is a property of the account, the network or this Terraform —
all shared. A later cog inherits them and does not re-measure them.

- **Cloudflare does not challenge AWS egress.** A probe from Lambda got 200
  and JSON from `api.kaianolevine.com`, not a challenge page. No WAF rule is
  needed *for AWS*. This was the constraint flagged as most likely to bite.
  Google's Drive webhook range is a separate question and is not covered by this
  measurement — see "Retiring watcher-cog".
- **The zip limit is not a problem.** 25.8 MB against 50. An earlier draft
  asserted otherwise.
- **The DLQ works.** Watched end to end with a deliberately failing
  function: three receives, ~17 minutes, then the message in the DLQ and the
  `evaluator-dlq-not-empty` alarm firing.
