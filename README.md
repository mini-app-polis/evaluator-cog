# evaluator-cog

Post-pipeline AI evaluation cog for the MiniAppPolis ecosystem. Evaluates
pipeline runs against the ecosystem standards document and posts structured
findings to api-kaianolevine-com.

## Overview

Two flows, two engine modules:
- `evaluator_cog.flows.pipeline_eval` — post-run behavioral evaluation; calls
  Claude, posts findings; handles Prefect webhook state events
- `evaluator_cog.flows.conformance` — scheduled structural conformance checker;
  downloads each active repo as a **zipball** from GitHub, runs deterministic + LLM checks
- `evaluator_cog.engine.deterministic` — file/AST/YAML rule checks (100+ rules)
- `evaluator_cog.engine.llm` — soft rule assessment, prompt builders, response parsing

Findings are written to the `pipeline_evaluations` table via
`api-kaianolevine-com` with `source=flow_inline` (normal runs) or
`source=flow_hook` (failure/crash hooks) or `source=prefect_webhook`
(Prefect Cloud automation).

## Standards coverage

As of the current release, the engine covers **106 of 116 deterministic rules (91%)**
in `ecosystem-standards`. Checks are wired in `run_all_checks` in
`src/evaluator_cog/engine/deterministic.py` and dispatched per repo type via
the `applies_to` list in each rule's catalog entry.

**Checks grouped by subsystem:**
- **File/YAML scans** (majority): pyproject.toml, package.json, .github/workflows/*.yml,
  .env.example, CHANGELOG.md, and similar presence/content checks.
- **AST scans** (Python source): route decorators, Pydantic models, SQLAlchemy
  models, Settings class field parity, Prefect flow/task decorators, public
  docstring coverage.
- **Astro parsing**: `.astro` frontmatter/script-region splitting for FE-006, FE-009, FE-010.

Zipball downloads do not include `.git/` history, so rules that require git log
or tags are not run in evaluator-cog today (see **Not yet implemented**).

**Not yet implemented** (10 rules — each blocked on a different infrastructure piece):

| Rule | Blocker |
|---|---|
| VER-001 | Need git history (conventional commits on last 20) — requires full clone |
| VER-002 | Need git history (BREAKING CHANGE on major tags) — requires full clone |
| PRIN-008 | Need git history (fix commits touch tests) — requires full clone |
| AUTH-001 | Needs `ecosystem.yaml` service-context threading into check runner |
| CD-004 | Needs GitHub API (verify pinned action tags exist); rate-limited |
| EVAL-002, EVAL-003, EVAL-006, MONO-003 | Need runtime SQL queries against `pipeline_evaluations` table |
| EVAL-007 | Already covered by `scripts/check_drift.py` in `ecosystem-standards` itself — no evaluator-cog work needed |

**LLM-routed rules** (catalog-marked `LLM CHECK.`): META-004, PIPE-013, PIPE-014,
PIPE-015, PRIN-010, XSTACK-005. These pass through the routing layer in
`engine/routing.py` and reach Claude for judgment-based assessment; no
deterministic engine implementation is expected.

**Notes on check behavior:**
- Each check function tolerates missing files/directories gracefully (returns `[]`).
- Rules that over-fire on a specific repo should be handled via a targeted
  entry in that repo's `evaluator.yaml` `exemptions:` section, not by relaxing
  the check globally.

## Inputs and outputs

**Inputs:** Pipeline run metrics (sets imported, failed, skipped, track counts)
passed directly from calling cogs via `evaluate_pipeline_run()`. Prefect flow
state events received as JSON via stdin or webhook payload. Repo source code
downloaded from GitHub (zipball) for structural conformance checks.

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
from evaluator_cog.flows.pipeline_eval import evaluate_pipeline_run

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
