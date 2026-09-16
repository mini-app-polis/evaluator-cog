# The AWS foundation: one queue, one function

**Status: built and proven, bar the CI deploy path.** The implementation is
`infra/` and the runbook is [infra/README.md](../infra/README.md); this document
is the decision record and the remaining work. Anything operational —  how to
apply, how to test the DLQ, what to hand step 2 — lives in that README rather
than being repeated here.

## What was decided

**Region `us-east-1`,** matching the Railway fleet. An earlier draft chose
`ap-southeast-1` to match a fleet then deployed in Southeast Asia, and flagged
that placement as a default nobody had chosen. The fleet moved to US East first,
and the region here followed it. Nothing depends on which region it is, only on
the two being the same — the worker POSTs findings back to the API dozens of
times per run.

**A dedicated AWS account,** root locked behind MFA and then unused; all work
through an admin IAM user. Upgrading that to Identity Center, so the credential
on the workstation is short-lived rather than a long-lived admin key, is a real
follow-up.

**Terraform from the start, not click-ops.** Click-ops is defensible for two
resources and stops being defensible at five cogs, and a console setup called
temporary becomes permanent by default. State is local: CI needs none, so the
only consumer is one workstation, and an S3 backend would buy a bootstrap
chicken-and-egg for little. The cost is real — lose the state file and these
resources are orphaned and must be re-imported by hand.

**Terraform owns configuration; CI owns code.** The deploy role can call
`UpdateFunctionCode` and `GetFunction` and nothing else, so a compromised
workflow cannot repoint the worker at a different API without someone reviewing
a `.tf` change. The function carries `ignore_changes` on its code attributes, or
every apply after a deploy would silently roll it back to the bootstrap
placeholder it was created with.

**A zip, not a container image.** Measured rather than assumed: 25.8 MB zipped
against a 50 MB limit once `prefect` is dropped. No ECR, no build-and-push, no
"which image is actually deployed".

**No access key in Terraform.** The producer's IAM user and its send-only policy
are managed; its key is minted by hand into Doppler, because Terraform would
hold that secret in plaintext local state.

## What exists

Queue and DLQ with a redrive policy, the worker function and its execution role,
the event source mapping, a log group with retention, the send-only producer
user, the GitHub OIDC provider and a deploy role scoped to this repository, a
monthly budget, and a DLQ alarm with an SNS topic behind it.

Two things were verified rather than assumed:

- **SQS → event source mapping → Lambda delivers.** A message sent by hand
  reached the function, which is the property the whole shape exists for: the
  polling happens on AWS's side of the line, not in a container you pay to keep
  awake.
- **A poison message reaches the DLQ.** Three receives, ~17 minutes, then the
  redrive policy did its job. The seventeen minutes is a consequence of the
  300-second function timeout and the visibility timeout derived from it; it is
  the lever if failures should escalate faster.

## What is left

**The CI deploy path.** `.github/workflows/deploy-worker.yml` exists but GitHub
will not dispatch a `workflow_dispatch` workflow until it is on the default
branch. Once it is: set `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION` and
`AWS_FUNCTION_NAME` as repository *variables* (not secrets — none of them are
secret), then run the workflow by hand. It fails if the checksum AWS reports is
not the artifact that run built, so a green run is evidence rather than an
assumption.

**Raise the Lambda concurrency quota** before reserved concurrency can be set at
all. A new account is far below the 100 unreserved executions AWS requires to be
left over, so any positive value fails at apply time with a message that does
not sound like a quota. Until then the account limit is itself the throttle.

## Non-goals

- No Step Functions, no fan-in coordinator, no EventBridge.
- Not the other four cogs. This is one queue and one function; generalising to
  the fleet is what happens after the pilot proves the shape. `infra/account.tf`
  is the account-level part that does not repeat.
- Not a migration of anything off Railway. The API and Postgres stay.

## Settled

**Cloudflare does not challenge AWS egress.** This was called the thing most
likely to bite — Bot Fight Mode had previously managed-challenged CI traffic
from GitHub runner ranges and cost an evening. A probe from Lambda against
`api.kaianolevine.com` returned 200 and JSON, not a challenge page. No WAF rule
is needed.

This is a property of the account and the network rather than of a cog, so it
holds for every later cog without being re-measured. Re-check it only if
Cloudflare's bot settings change. (The probe used to live in a stub worker that
ran before the real code; that stub is gone — it was a second consumer on the
queue and ate real jobs. A cog that wants to re-measure can invoke the deployed
function against a harmless endpoint instead.)
