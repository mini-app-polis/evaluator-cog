# evaluator-cog — Architecture

## Structure

```
evaluator-cog/
  src/
    evaluator_cog/
      _deadline.py          # stops a run before the Lambda timeout kills it
      adapters/
        lambda_worker.py    # the entry point: lambda_handler(event, context) for SQS records
        queue.py            # process_message(): parses a message, dispatches on type
      flows/
        conformance.py      # handler() + run_introspection(): structural conformance
      engine/
        deterministic/      # file / AST / YAML rule checks, split by domain
          __init__.py           # re-exports the public surface (run_all_checks + all check_*)
          _shared.py            # Finding, CheckResult, _finding helper
          _routes.py            # AST route enumeration for the identity rules
          _terraform.py         # reads the Terraform facts the runtime rules need
          _workflows.py         # shared GitHub Actions workflow parser
          runner.py             # run_all_checks dispatcher + dedup
          docs.py               # README / CHANGELOG / docstring checks
          python.py             # pyproject / src-layout / naming
          versioning.py         # .releaserc / conventional commits / changelog
          delivery.py           # CI / GHA / logging / secrets / observability
          pipeline.py           # pipeline runtime / retry / triggers / evaluation
          frontend.py           # Astro / Vite-React / Tailwind / shadcn
          api.py                # FastAPI / Postgres / HTTP API contract
          auth.py               # credential verification / scope coverage rules
          identity.py           # AUTH-003, AUTH-004, CD-019, CD-029, CD-030
          config.py             # env vars / settings / shared-library use
          containers.py         # CD-017, CD-021..CD-024 platform descriptors
          crossrepo.py          # XSTACK-006, XSTACK-008
          dependencies.py       # XSTACK-007 shared-library pins
          layout.py             # LAYOUT-001, LAYOUT-002
          operations.py         # OPS-001..006
          packaging.py          # CD-020 lockfile discipline
          security.py           # SEC-001..008
          testing.py            # TestClient / fixtures / mock / respx
          meta.py               # META-001..007 — catalog self-checks
          introspection.py      # EVAL-003, EVAL-007
        routing.py          # routes rules to the deterministic or LLM path
        llm.py              # soft rule assessment, prompt builders, response parsing
        evaluator_config.py # per-repo evaluator.yaml loader
        api_client.py       # posts findings to api-kaianolevine-com
      models.py             # shared Pydantic models (Finding, ConformanceResult)
```

## Evaluation types

| Flow | Source value | Trigger | What it evaluates |
| --- | --- | --- | --- |
| `conformance` (deterministic) | `conformance_deterministic` | An `evaluation.repository` message, from `POST /v1/evaluations/runs` on a repository's release or `POST /v1/evaluations/fleet` (`mode='deterministic'`) | Structural — file/AST/YAML rule checks |
| `conformance` (LLM) | `conformance_llm` | The same messages with `mode='llm'` | Structural — soft rule assessment by Claude |
| introspection (EVAL-007) | `standards_drift` | An `evaluation.introspection` message, once per **fleet pass** | Catalog vs evaluator drift |
| introspection (EVAL-003) | `data_quality` | An `evaluation.introspection` message, once per **fleet pass** | Quality of stored findings |

The introspection checks (EVAL-003, EVAL-007, XSTACK-006, XSTACK-008) scope
to no repository (ADR-004 in ecosystem-standards), so a per-repository message
is not a place they could run. `run_introspection()` runs them from their own
message, which `POST /v1/evaluations/fleet` enqueues alongside the
per-repository ones (or `POST /v1/evaluations/introspection` on its own).

## Findings destination

All findings land in `pipeline_evaluations` table via `api-kaianolevine-com`.
The `source` field distinguishes evaluation type.

## LLM involvement

Hybrid approach:

- Deterministic checks for structural rules (file presence, `pyproject.toml`,
  CI YAML, AST) — most checkable rules
- LLM for soft rules (docstrings, dead code) and to generate actionable
  suggestions and report narrative
