# evaluator-cog — Architecture

## Structure

```
evaluator-cog/
  src/
    evaluator_cog/
      flows/
        pipeline_eval.py    # post-pipeline behavioral evaluation (existing)
        conformance.py      # structural repo checker (planned)
        monthly_report.py   # ecosystem drift summary (planned)
      engine/
        deterministic.py    # file / AST / YAML rule checks
        llm.py              # soft rule checks + report narrative + suggestions
      models.py             # shared Pydantic models
      api_client.py         # posts findings to api-kaianolevine-com
```

Note: the restructure into `flows/` and `engine/` is pending. Current layout
is flat under `src/evaluator_cog/` — `evaluator.py` and `webhook.py` will be
moved into this structure in the next phase.

## Evaluation types

| Flow | Source value | Trigger | What it evaluates |
| --- | --- | --- | --- |
| `pipeline_eval` | `flow_inline` / `flow_hook` / `prefect_webhook` | Post-pipeline Prefect hook | Behavioral — did the run behave correctly? |
| `conformance` | `conformance_check` | Scheduled cog, centralized | Structural — does the repo meet standards? |
| `monthly_report` | `monthly_report` | Monthly Prefect schedule | Ecosystem-wide drift summary |

## Findings destination

All findings land in `pipeline_evaluations` table via `api-kaianolevine-com`.
The `source` field distinguishes evaluation type.

## LLM involvement

Hybrid approach:

- Deterministic checks for structural rules (file presence, `pyproject.toml`,
  CI YAML, AST) — covers ~40 of 50 checkable rules
- LLM for soft rules (docstrings, dead code) and to generate actionable
  suggestions and report narrative
