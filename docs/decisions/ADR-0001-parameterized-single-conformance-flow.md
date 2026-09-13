# 0001. Parameterized single conformance flow instead of two deployments

Date: 2026-04-05

## Status

Superseded by [0004](./ADR-0004-evaluation-on-demand.md). The deployment
budget this decision was shaped by stopped applying when evaluator-cog
stopped being a Prefect deployment; the two modes it describes survive as
`mode='deterministic'` and `mode='llm'` on the evaluation event.

## Context

evaluator-cog runs two distinct passes:

- **Deterministic** — regex/AST-based rule checks, cheap, run daily on cron.
- **LLM** — soft-rule assessment via Claude, expensive in tokens, run weekly
  or manually.

The natural shape would be two Prefect flows, each with its own deployment:
`deterministic_check_flow` and `conformance_check_flow`. But the Prefect Cloud
Hobby tier caps deployments at 5 across the whole account, and the
MiniAppPolis ecosystem is already near that cap across cogs.

## Decision

Ship a single `@flow` named `conformance_check_flow` parameterized with
`run_llm: bool = False`:

- `run_llm=False` (default, daily cron): deterministic pass only, posts
  findings with `source='conformance_deterministic'` and run_id prefix
  `deterministic-{version}-{uuid}`.
- `run_llm=True` (manual/weekly automation): deterministic pass runs first
  (for `checked_rule_ids`), then the LLM pass. Posts LLM findings only with
  `source='conformance_check'` and run_id prefix `conformance-{version}-{uuid}`.

One Prefect deployment slot, two behaviors selected by parameter.

## Consequences

- Stays within Prefect Hobby tier deployment budget.
- Single source of truth for ecosystem scan logic — no drift between two
  near-identical flows.
- Finding source field distinguishes the two modes in `pipeline_evaluations`
  so dashboards can separate them.
- Backlog: split the deterministic pass into a higher-frequency flow once
  the deployment budget expands. The LLM flow will then need to receive
  `checked_rule_ids` from the deterministic run — likely stored in
  `pipeline_evaluations` and queried at LLM run time.
