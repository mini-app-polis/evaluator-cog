# evaluator-cog — Configuration

Every environment variable this cog reads is listed, with what it does and
whether it is required, in [`.env.example`](../.env.example). This page
holds only what that file cannot: how the values reach the worker and when a
change takes effect.

The worker runs on AWS Lambda, declared in mini-app-polis/infra. On the
deployed worker, secrets are loaded from SSM Parameter Store (synced from
Doppler) at cold start and **read again at the start of every invocation**,
so a value changed in Doppler applies to the next job once the sync has run
— no deploy or cold start. If SSM cannot be reached on a refresh, the values
already loaded are kept. The rest is set by Terraform. Two exceptions need
a cold start: `SENTRY_DSN_COGS` is read once per cold start, in
`adapters/lambda_worker`, and `ECOSYSTEM_STANDARDS_CATALOG_URL` when its
module loads.

On a container's first invocation, once the settings are loaded, the
worker logs each setting it reads with its default applied, one line per
name. A name listed in `SETTINGS_ALLOWLIST` (comma-separated, read by the
shared library) shows its value as `NAME=value (set|default)`; every other
name shows only `NAME (set|unset)`, so a secret's value is never logged.

Locally, run under `doppler run` with Doppler's `dev` config; nothing reads
a `.env` file. `uv run check-doppler-keys` checks that dev holds every
required name.
