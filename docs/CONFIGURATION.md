# evaluator-cog — Configuration

Environment variables from `.env.example`. One section per variable.

The worker runs on AWS Lambda, declared in mini-app-polis/infra. On the
deployed worker, secrets are loaded at cold start from SSM Parameter Store
(synced from Doppler; `cogs.tf` there lists the names) and the rest is set
by Terraform — the `.env` file is for running the handler locally.

## ANTHROPIC_API_KEY

API key for Anthropic Claude. Only `mode='llm'` spends it; the release path
is deterministic and costs no tokens.

```
ANTHROPIC_API_KEY=your-anthropic-api-key
```

## ANTHROPIC_MODEL

Claude model to use for evaluation. Defaults to claude-sonnet-5.

```
ANTHROPIC_MODEL=claude-sonnet-5
```

## KAIANO_API_BASE_URL

Base URL for the api-kaianolevine-com instance. Findings go here, and so
does the standards catalog the run grades against.

```
KAIANO_API_BASE_URL=https://api.kaianolevine.com
```

## KAIANO_API_BASE_URL_DEV

The same for non-production. Production reads the unsuffixed name and every
other environment reads this one, **with no fallback between them** — an
unset dev URL in a dev environment is an error rather than a quiet
redirection to production.

```
KAIANO_API_BASE_URL_DEV=
```

## EVALUATOR_COG_API_KEY

This cog's own named API key, matching the machine name it declares to
`KaianoApiClient`. There is no fallback — unset or wrong means 401 on every
call to the API. See ecosystem-standards CD-019.

```
EVALUATOR_COG_API_KEY=YOUR_EVALUATOR_COG_API_KEY
```

## SENTRY_DSN

Sentry DSN for error tracking. Initialised once per cold start, in
`adapters/lambda_worker`.

```
SENTRY_DSN=your-sentry-dsn
```

## GITHUB_TOKEN

GitHub personal access token with read access to mini-app-polis repos.
Required for the conformance flow to download private repos as zipballs.

```
GITHUB_TOKEN=your-github-token
```

## HEALTHCHECKS_URL_EVALUATOR

Healthchecks.io ping URL for Layer 1 observability. Pinged at the end of a
fleet pass — specifically by `run_introspection`, which is the once-per-pass
job. It is a ping and not a check: what it reports is that a pass reached
its end, and Healthchecks.io watches for its *absence*.

```
HEALTHCHECKS_URL_EVALUATOR=https://hc-ping.com/your-uuid
```

## ENVIRONMENT

Which environment this process is in. Set explicitly on the Lambda by
Terraform. Unset locally resolves to `local`.

```
ENVIRONMENT=local
```

## Variables that used to be here

Listed because their absence is a decision, and a reader who remembers them
should find out what replaced them rather than assume they were forgotten.

**`AWS_REGION`** — nothing reads it. The container created a boto3 SQS
client and needed a region for it; the handler creates no client, because
it is handed a message rather than fetching one.

**`EVALUATION_QUEUE_URL`** — the worker does not read a queue. The Lambda
event source mapping receives and invokes; the handler is handed a message
and never asks for one. The API holds the queue URL, because the API sends.

**`EVALUATION_QUEUE_CONSUMER_KEY_ID` / `_SECRET`** — there is no key. The
Railway container needed one because a container cannot assume a role. The
Lambda uses `aws_iam_role.worker`, which carries the same receive-and-delete
permissions and expires on its own. The IAM user and its access key were
deleted with the cutover.

**`EVALUATOR_INVOKE_SECRET`** — there is no inbound HTTP. The evaluator had
an adapter serving `POST /invoke` behind a shared secret; that adapter was
replaced by the queue, and a service with no listener needs no guard on it.

**`PREFECT_API_KEY` / `PREFECT_API_URL`** — evaluator-cog does not run under
Prefect (ADR-0004) and no longer depends on it in any form.
