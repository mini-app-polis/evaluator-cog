# evaluator-cog

Post-pipeline AI evaluation cog for the MiniAppPolis ecosystem. Evaluates
pipeline runs against the ecosystem standards document and posts structured
findings to api-kaianolevine-com.

## Overview

Two modules:
- `evaluator_cog.evaluator` — builds prompts, calls Claude, posts findings
- `evaluator_cog.webhook` — handles Prefect flow-run state events

Findings are written to the `pipeline_evaluations` table via
`api-kaianolevine-com` with `source=flow_inline` (normal runs) or
`source=flow_hook` (failure/crash hooks) or `source=prefect_webhook`
(Prefect Cloud automation).

## Inputs and outputs

**Inputs:** Pipeline run metrics (sets imported, failed, skipped, track counts)
passed directly from calling cogs via `evaluate_pipeline_run()`. Prefect flow
state events received as JSON via stdin or webhook payload. Repo source code
cloned from GitHub for structural conformance checks.

**Outputs:** Structured findings written to the `pipeline_evaluations` table
via `POST /v1/evaluations` on api-kaianolevine-com. Each finding includes
`repo`, `run_id`, `severity`, `dimension`, `finding`, `suggestion`,
`standards_version`, and `source`.

## Running locally

Prerequisites: Python 3.11+, uv
```bash
uv sync --all-extras
pre-commit install
pre-commit run --all-files
uv run pytest
```

Copy `.env.example` to `.env` and fill in values before running.

## Wiring into a Prefect flow
```python
from evaluator_cog.evaluator import evaluate_pipeline_run

# At the end of your flow:
evaluate_pipeline_run(
    run_id=os.environ.get("GITHUB_RUN_ID", "local-run"),
    repo="your-repo-name",
    ...
)

# For crash/failure detection, add hooks to your @flow decorator:
def _handle_flow_failure(flow, flow_run, state) -> None:
    evaluate_pipeline_run(
        run_id=str(flow_run.id),
        repo="your-repo-name",
        direct_finding_text=f"Flow entered {state.name} unexpectedly",
        direct_severity="ERROR" if "crash" in state.name.lower() else "WARN",
        ...
    )

@flow(
    name="your-flow-name",
    on_failure=[_handle_flow_failure],
    on_crashed=[_handle_flow_failure],
)
def your_flow():
    ...
```

## Prefect automation setup

See `docs/PREFECT_AUTOMATION.md` for setting up the Prefect Cloud automation
that triggers evaluation on FAILED and CRASHED state changes.

## Versioning

Managed by semantic-release. Never manually edit `version` in `pyproject.toml`
or `CHANGELOG.md`.
