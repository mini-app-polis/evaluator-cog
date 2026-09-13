# evaluator-cog — Configuration

Environment variables from `.env.example`. One section per variable.

## ANTHROPIC_API_KEY

API key for Anthropic Claude.

ANTHROPIC_API_KEY=your-anthropic-api-key

## ANTHROPIC_MODEL

Claude model to use for evaluation. Defaults to claude-sonnet-4-20250514.

ANTHROPIC_MODEL=claude-sonnet-4-20250514

## KAIANO_API_BASE_URL

Base URL for the api-kaianolevine-com instance.

KAIANO_API_BASE_URL=https://your-api.railway.app

## KAIANO_API_OWNER_ID

Owner ID sent with API requests.

KAIANO_API_OWNER_ID=your-owner-id

## EVALUATOR_INVOKE_SECRET

Shared secret that api-kaianolevine-com presents on the `X-Evaluator-Token`
header when it forwards an evaluation or a sweep. Required: the adapter
refuses every request with 503 when this is unset, because an unconfigured
secret on a service that clones repositories and writes findings is a
misconfiguration rather than an invitation. Must match the value set on
api-kaianolevine-com.

EVALUATOR_INVOKE_SECRET=your-shared-invoke-secret

## SENTRY_DSN_EVALUATOR

Sentry DSN for error tracking.

SENTRY_DSN_EVALUATOR=your-sentry-dsn

## PREFECT_API_KEY

PREFECT_API_KEY=your-prefect-api-key

## PREFECT_API_URL

PREFECT_API_URL=https://api.prefect.cloud/api/accounts/<account-id>/workspaces/<workspace-id>

## GITHUB_TOKEN

GitHub personal access token with read access to mini-app-polis repos.
Required for the conformance flow to clone private repos.

GITHUB_TOKEN=your-github-token
