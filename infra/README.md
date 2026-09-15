# The AWS foundation: one queue, one function

Terraform for the minimum AWS footprint needed to run evaluator-cog as a
queue worker. The companion ticket is
[../docs/aws-foundation.md](../docs/aws-foundation.md); this is its
implementation, and that document wins on anything AWS-side when the two
disagree.

**Scope:** infrastructure, plus a stub function. No cog code. The real
worker arrives at step 5 of
[../docs/serverless-migration.md](../docs/serverless-migration.md) and
replaces the contents of `stub/`, not any resource in here.

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
  whatever is in `stub/`.
- A deploy cannot repoint the worker at a different API. That needs a
  reviewed `.tf` change.

**Local state, deliberately.** CI needs no state, so the only consumer is
one workstation, and an S3 backend would buy a bootstrap chicken-and-egg
for little. The cost is real: lose `terraform.tfstate` and these resources
are orphaned and have to be re-imported by hand. Revisit when a second
person or a second machine applies this.

**No access key in Terraform.** `producer.tf` creates the IAM user and its
send-only policy but not its key, because Terraform would hold that secret
in plaintext local state. Mint it once by hand into Doppler — the file says
how. There is no OIDC path from Railway, so it is long-lived and it is the
thing here most worth a rotation reminder.

## Order of operations

Each step is provable before the next one starts. Do not skip the stub.

```bash
cp terraform.tfvars.example terraform.tfvars   # then fill it in
terraform init
terraform fmt -check
terraform validate
terraform plan
```

1. **Account, billing alarm, MFA on root.** The budget is in `account.tf`
   and applies with everything else; do the MFA by hand, and then never use
   root again.
2. **Queue and DLQ.** `terraform apply -target=aws_sqs_queue.dlq -target=aws_sqs_queue.jobs`
   if you want them alone first, or just apply the lot.
3. **Stub plus event source mapping.** The mapping is disabled by default
   (`worker_consumes_queue`), because SQS gives a message to exactly one
   consumer and the Railway container is the one that evaluates. To exercise
   the stub, apply with `-var worker_consumes_queue=true`, run the probe
   below, and turn it back off. Leaving it on is what makes an evaluation
   vanish: the stub eats the job and returns success, so the queue, the DLQ
   and the results are all empty at once.

   With it on, send a message by hand and watch it arrive:

   ```bash
   aws sqs send-message \
     --queue-url "$(terraform output -raw queue_url)" \
     --message-body '{"repo":"watcher-cog","ref":"main","mode":"deterministic"}'

   aws logs tail "/aws/lambda/$(terraform output -raw function_name)" --follow
   ```

   The log line to read is `stub: api probe {...}`. `"ok": true` means the
   Cloudflare hop works from AWS address space.
   `"looks_like_challenge_page": true` means Bot Fight Mode is challenging
   this source range and needs a WAF rule — which is the failure this
   probe exists to surface here, cheaply, rather than from a queue full of
   retried messages later.
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

   Then run the **Deploy worker** workflow by hand. It fails if the
   checksum AWS reports is not the artifact that run built, so a green run
   is evidence and not an assumption.
6. **Verify failure handling.** A DLQ nobody has watched a message enter
   is not yet a DLQ.

   ```bash
   aws lambda update-function-configuration \
     --function-name "$(terraform output -raw function_name)" \
     --environment "Variables={EVALUATOR_STUB_FAIL=1,...}"   # keep the others
   ```

   Send a message, watch it retry `max_receive_count` times, then confirm
   it is in the DLQ — and that `evaluator-dlq-not-empty` alarms. Unset the
   variable afterwards.

   Note this is the one step that reaches past the Terraform/CI split on
   purpose, and the next `terraform apply` will put the environment back.

## What the code ticket needs back

`terraform output` — queue URL and ARN, DLQ ARN, function name, region,
deploy role ARN, producer user name.

## When the fleet generalises

`account.tf` is account-level and does not repeat: one budget, one GitHub
OIDC provider. Everything else becomes one module invocation per cog, with
`create_github_oidc_provider = false` for every cog after this one. Kept in
separate files so that is a move rather than an untangling.

## Not verified here

The Terraform has not been run through `terraform fmt`, `validate` or
`plan` — the session that wrote it could not reach `releases.hashicorp.com`
to install Terraform. Treat step 0 above as mandatory rather than routine.
`stub/handler.py` was exercised against synthetic SQS events and mocked API
responses, including a Cloudflare challenge page, a 401 and the deliberate
failure switch.
