# evaluator-cog — Architecture

## Structure

```
evaluator-cog/
  src/
    evaluator_cog/
      flows/
        pipeline_eval.py    # post-run behavioral evaluation; webhook handler
        conformance.py      # scheduled structural conformance checker
      engine/
        deterministic.py    # file / AST / YAML rule checks (~40 rules)
        llm.py              # soft rule assessment, prompt builders, response parsing
        evaluator_config.py # per-repo evaluator.yaml loader
        api_client.py       # posts findings to api-kaianolevine-com
      models.py             # shared Pydantic models (Finding, ConformanceResult)
```

## Evaluation types

| Flow | Source value | Trigger | What it evaluates |
| --- | --- | --- | --- |
| `pipeline_eval` | `flow_inline` / `flow_hook` / `prefect_webhook` | Called in-process by other cogs; Prefect Cloud automation webhook | Behavioral — did the run behave correctly? |
| `conformance` (deterministic) | `conformance_deterministic` | Daily cron via Prefect (`run_llm=False`) | Structural — file/AST/YAML rule checks |
| `conformance` (LLM) | `conformance_check` | Manual trigger or Prefect automation (`run_llm=True`) | Structural — soft rule assessment by Claude |

## Findings destination

All findings land in `pipeline_evaluations` table via `api-kaianolevine-com`.
The `source` field distinguishes evaluation type.

## LLM involvement

Hybrid approach:

- Deterministic checks for structural rules (file presence, `pyproject.toml`,
  CI YAML, AST) — covers ~40 of 50 checkable rules
- LLM for soft rules (docstrings, dead code) and to generate actionable
  suggestions and report narrative

## Known limitations

### Dedup window (api_client)
`post_findings` deduplicates against the single most-recent stored finding for
a repo. This is reliable for single-finding webhook events but may allow
duplicates in multi-finding conformance batches if the most-recent record does
not match the finding being posted. Future improvement: composite key lookup by
`run_id` + `dimension` + `finding` hash.
