# The AWS foundation: one queue, one function

Terraform for the minimum AWS footprint needed to run evaluator-cog as a
queue worker. The companion ticket is
[../docs/aws-foundation.md](../docs/aws-foundation.md); this is its
implementation, and that document wins on anything AWS-side when the two
disagree.

**Scope:** infrastructure. No cog code. The function is created with a
placeholder payload that cannot run — see `worker.tf` — and the real worker
arrives from CI at step 5 of
[../docs/serverless-migration.md](../docs/serverless-migration.md),
replacing the function's code and no resource in here.

## Decisions made here

**Region `us-east-1`,** matching the Railway fleet after its move to US
East. The worker POSTs findings back to api-kaianolevine-com dozens of
times per run; co-located, that distance is nothing.

**A zip, not a container image.** The ticket said to measure before
reaching for ECR, and the measurement says a zip fits with room to spare:

| | zipped (limit 50 MB) | unzipped (limit 250 MB) |
|---|---|---|
| worker as-is, once `prefect` is dropped | 25.8 MB | 136 MB |

This contradicts `serverless-migration.md` step 5, which asserts the tree
is "past the zip limit comfortably". It is not, and skipping ECR removes a
registry, a build-and-push step and an entire class of "which image is
actually deployed" confusion.

Worth knowing: **100 MB of that 136 MB is `googleapiclient`**, pulled in
transitively by `miniapppolis-common-utils` and never imported by the
evaluator. Behind a `miniapppolis-common-utils[google]` extra the package
is 27 MB unzipped and 8.2 MB zipped. That is a shared-library change
affecting every cog, so it is noted rather than done.

**Terraform owns configuration; CI owns code.** `terraform apply` runs from
a workstation and manages the role, environment, timeout and concurrency.
The GitHub Actions workflow can call `UpdateFunctionCode` and `GetFunction`
and nothing else. Two consequences worth understanding before changing
anything:

- `aws_lambda_function.worker` carries
  `ignore_changes = [filename, source_code_hash, last_modified]`. Without
  it, every apply after a deploy would silently roll the function back to
  the bootstrap placeholder.
- A deploy cannot repoint the worker at a different API. That needs a
  reviewed `.tf` change.

**Local state, deliberately.** CI needs no state, so the only consumer is
one workstation, and an S3 backend would buy a bootstrap chicken-and-egg
for little. The cost is real: lose `terraform.tfstate` and these resources
are orphaned and have to be re-imported by hand. Revisit when a second
person or a second machine applies this.

**Secrets come from Doppler, not tfvars.** `./tf` is a wrapper that reads
`evaluator_cog_api_key`, `github_token`, `anthropic_api_key` and `sentry_dsn`
out of Doppler and hands them to Terraform as `TF_VAR_*`. Use it instead of
bare `terraform` for anything that reads variables:

```bash
./tf plan -out tfplan
./tf apply tfplan
```

A secret placed in `terraform.tfvars` would win over the environment, so the
wrapper refuses to run while one is set. `terraform.tfvars` holds the
non-secret settings only — `alert_email`, `kaiano_api_base_url`,
`create_github_oidc_provider`. State still records every value Terraform
applies; that is why the state file is gitignored and why SEC-008 checks it.

One-time per machine, in this directory:

```bash
doppler setup --project <evaluator-cog project> --config prd
```

**No access key in Terraform.** `producer.tf` creates the IAM user and its
send-only policy but not its key, because Terraform would hold that secret
in plaintext local state. Mint it once by hand into Doppler — the file says
how. There is no OIDC path from Railway, so it is long-lived and it is the
thing here most worth a rotation reminder.

## Order of operations

Each step is provable before the next one starts.

```bash
cp terraform.tfvars.example terraform.tfvars   # non-secret settings only
terraform init
terraform fmt -check
terraform validate
./tf plan -out tfplan
```

1. **Account, billing alarm, MFA on root.** The budget is in `account.tf`
   and applies with everything else; do the MFA by hand, and then never use
   root again.
2. **Queue and DLQ.** `terraform apply -target=aws_sqs_queue.dlq -target=aws_sqs_queue.jobs`
   if you want them alone first, or just apply the lot.
3. **Function and event source mapping.** The mapping is enabled, and the
   function is created holding a placeholder that cannot import. Between
   here and step 5 the queue has exactly one consumer and that consumer
   fails every message, which is the correct state for a cog that has no
   code deployed: a job sent now retries, dead-letters, and is visible.

   There is no stub worker and no probing phase. This repo had one — it
   logged its event, called the API and returned success — and returning
   success is precisely what made it dangerous, because a consumed-and-
   discarded job looks exactly like one that was never enqueued. It ate
   five real evaluations. What it existed to prove is proven and recorded
   under **Verified** below; those are properties of the account and the
   network, not of any one cog, so no later cog re-proves them.
4. **Producer credentials.**

   ```bash
   aws iam create-access-key --user-name "$(terraform output -raw producer_user_name)"
   ```

   Straight into Doppler; it reaches the Railway API from there.
5. **CI deploy path.** Set three repository variables in GitHub — not
   secrets, none of these are secret:

   ```
   AWS_DEPLOY_ROLE_ARN = $(terraform output -raw deploy_role_arn)
   AWS_REGION          = $(terraform output -raw region)
   AWS_FUNCTION_NAME   = $(terraform output -raw function_name)
   ```

   Deploys run from the `deploy` job in `ci.yml` after each release, through
   the shared `lambda-deploy.yml` in mini-app-polis/.github. To redeploy
   without a release, re-run the `deploy` job of the release's CI run. It
   fails unless the checksum AWS reports is the artifact that run built, so a
   green run is evidence and not an assumption.
6. **Verify failure handling.** Only needed once per account, and it is
   done — see **Verified**. The redrive policy, the DLQ and the
   `evaluator-dlq-not-empty` alarm are the same Terraform in every cog, so
   a second cog confirming them again proves nothing new.

   If you do want to watch it for a cog, send a deliberately malformed
   message before deploying any code. The placeholder cannot import, so it
   fails, retries `max_receive_count` times and lands in the DLQ without
   needing a failure switch in the function's environment.

## The cutover

Bringing a cog's Lambda into service, and taking it back out. This is the
part that is dangerous by omission rather than by commission: every step
below fails loudly except the ordering, which fails silently.

**The rule: never let two things do the same work at once, and prefer a gap
to an overlap.** A gap where nothing runs is harmless — a queued message is
durable and a missed trigger can be re-sent. An overlap is either duplicated
work or lost work, and which one it is depends on where the overlap sits:

- **Two consumers on one queue loses work.** SQS gives each message to
  exactly one of them, so half the jobs are silently eaten. This is what
  the old stub Lambda did to five real evaluations — queue empty, DLQ
  empty, no findings, indistinguishable from a job never enqueued. It is
  recorded here because it is the worst failure this system has produced,
  not because it can still happen: there is one consumer per queue now and
  no switch that changes that.
- **Two triggers on one cog duplicates work.** This is the shape the
  remaining cogs actually face, because they move from Prefect to SQS and
  their queue is born with exactly one reader. The overlap is a Prefect
  schedule or a watcher trigger still firing while the API also enqueues.
  It is loud and cheap by comparison — two runs, server-side idempotency
  absorbing the second — but retire the old trigger in the same change
  that enables the new one.

### 1. Point Terraform at the real handler

`terraform apply`. Between this and the next step the function is
inconsistent — real handler string, placeholder code — and every invocation
fails with an import error. Harmless if nothing is being enqueued yet, and
visible in the DLQ if something is.

### 2. Deploy the code

Release, or re-run the `deploy` job of the last release's CI run. The
shared `lambda-deploy.yml` builds wheels for the function's architecture
and runtime, fails if any library needs a newer glibc than the runtime has,
imports every module and probes the handler inside Lambda's own image,
refuses a function whose architecture differs, and fails if the checksum
AWS reports is not the one it built.

### 3. Probe the function before any real traffic

Two invokes, cheapest first. Neither touches the queue.

```bash
aws lambda invoke --function-name "$(terraform output -raw function_name)" \
  --payload '{"Records":[{"messageId":"probe-1","body":"not json","attributes":{}}]}' \
  --cli-binary-format raw-in-base64-out /dev/stdout
```

Want `{"batchItemFailures":[{"itemIdentifier":"probe-1"}]}` and
`StatusCode: 200` with **no** `FunctionError`. That proves the package
imports, the handler resolves, boto3 comes from the runtime, and a failure
is reported rather than swallowed — with no side effects.

Then one real message for a single unit of work, from a file (quoting an
SQS event inline is miserable). It exercises the API key, the GitHub token
and the Cloudflare hop from Lambda's address space. Server-side idempotency
makes a repeat harmless.

### 4. Cut over

The Lambda is already the queue's only consumer, so there is no switch to
throw here — the cutover is retiring whatever used to do this work:

1. Stop the old runner — the Prefect deployment, or a Railway service
   scaled to zero. Do **not** delete it or its Doppler secrets yet.
2. Confirm it stopped. Its last log line should come from its own shutdown
   path, not from the platform killing it.
3. Point the trigger at the API, so releases and watcher events enqueue
   instead of starting a flow run.

Steps 1 and 3 are the pair that must not overlap. Doing 1 first leaves a
gap in which events are simply not processed; do 3 first and both paths
run the same work.

### 5. Verify

```bash
aws logs tail "/aws/lambda/$(terraform output -raw function_name)" --follow

aws sqs get-queue-attributes --queue-url "$(terraform output -raw queue_url)" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
aws sqs get-queue-attributes --queue-url "$(terraform output -raw dlq_url)" \
  --attribute-names ApproximateNumberOfMessages
```

All three counts at zero once the work drains. `NotVisible` above zero means
jobs are being picked up and failing mid-flight; DLQ above zero means they
failed `max_receive_count` times and the body will say why.

### Rolling back

Point the trigger back at the old runner and restart it. Messages already
on the queue are still consumed by the Lambda; anything in flight returns
to the queue when its visibility timeout expires. Nothing is lost — which
is the property the whole migration was for.

This only works while the old runner is still runnable, and it stops being
true on purpose. evaluator-cog deleted `main()`, the poll loop and
`railway.json` once its cutover had settled, so its rollback is a rewrite
rather than a restart. Keeping a second consumer runnable is how two of
them end up running; delete it deliberately, once, after the new path has
carried real traffic.

## What the code ticket needs back

`terraform output` — queue URL and ARN, DLQ ARN, function name, region,
deploy role ARN, producer user name.

## When the fleet generalises

`account.tf` is account-level and does not repeat: one budget, one GitHub
OIDC provider. Everything else becomes one module invocation per cog, with
`create_github_oidc_provider = false` for every cog after this one. Kept in
separate files so that is a move rather than an untangling.

## Verified

All of it has been applied, and evaluator-cog has run through it end to
end: the queue, the DLQ, the deploy pipeline, the cutover above, and a real
fleet pass on the Lambda afterwards.

These are the results a later cog does **not** need to reproduce. They are
properties of the account, the network and this Terraform, all of which are
shared:

- **Cloudflare does not challenge AWS egress.** A request from Lambda to
  `api.kaianolevine.com` got 200 and JSON, not a challenge page, so no WAF
  rule is needed. Google's Drive webhook range is a separate question and
  is not covered by this.
- **The DLQ works.** Watched end to end with a deliberately failing
  function: three receives, ~17 minutes, then the message in the DLQ and
  the `evaluator-dlq-not-empty` alarm firing.
- **A zip is enough.** No ECR, no image. See the table above.
- **The deploy role is sufficient and no wider.** CI can call
  `UpdateFunctionCode`, `GetFunction` and `GetFunctionConfiguration`, and
  the checksum assertion in the workflow means a green run is evidence.
