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
        deterministic/      # file / AST / YAML rule checks (~40 rules), split by domain
          __init__.py           # re-exports the public surface (run_all_checks + all check_*)
          _shared.py            # Finding, CheckResult, _finding helper
          runner.py             # run_all_checks dispatcher + dedup
          docs.py               # README / CHANGELOG / docstring checks
          python.py             # pyproject / src-layout / naming
          versioning.py         # .releaserc / conventional commits / changelog
          delivery.py           # CI / GHA / logging / secrets / observability
          pipeline.py           # Prefect flow presence / retry / serve pattern
          frontend.py           # Astro / Vite-React / Tailwind / shadcn
          api.py                # FastAPI / Postgres / HTTP API contract
          auth.py               # credential verification / scope coverage rules
          config.py             # env vars / settings / shared-library use
          testing.py            # TestClient / fixtures / mock / respx
          meta.py               # META-001..007 — catalog self-checks
          introspection.py      # EVAL-003, MONO-003, EVAL-007
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
| `conformance` (LLM) | `conformance_llm` | Manual trigger or Prefect automation (`run_llm=True`) | Structural — soft rule assessment by Claude |
| introspection (EVAL-007) | `standards_drift` | Runs inside every conformance invocation | Catalog vs evaluator drift |
| introspection (EVAL-003, MONO-003) | `data_quality` | Runs inside every conformance invocation | Quality of stored findings; ecosystem.yaml inventory integrity |

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
