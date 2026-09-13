# evaluator-cog

Post-pipeline AI evaluation cog for the MiniAppPolis ecosystem. Evaluates
pipeline runs against the ecosystem standards document and posts structured
findings to api-kaianolevine-com.

## Overview

A web service, two engine modules:
- `evaluator_cog.adapters.http` — the entry point. `POST /invoke` evaluates one
  repository; `POST /sweep` evaluates the whole registry. Both are secret-guarded,
  both answer 202 and do the work in the background. Railway starts this
  (`railway.json`), not a Prefect runner — see
  [ADR-0004](docs/decisions/ADR-0004-evaluation-on-demand.md)
- `evaluator_cog.flows.conformance` — `handler(event)` is the unit of work: one
  repository, downloaded as a **zipball** from GitHub and run through the
  deterministic and/or LLM checks. `run_fleet_sweep()` loops it over the registry
  and then runs the checks that scope to no repository at all
- `evaluator_cog.flows.pipeline_eval` — post-run behavioral evaluation; calls
  Claude, posts findings; handles Prefect webhook state events. A library module
  called in-process by other cogs, not a served deployment
- `evaluator_cog.engine.deterministic` — file/AST/YAML rule checks (100+ rules)
- `evaluator_cog.engine.llm` — soft rule assessment, prompt builders, response parsing

Nothing here is scheduled. A repository is evaluated when it releases: its CI posts
to `POST /v1/evaluations/runs` on api-kaianolevine-com, which forwards to `/invoke`.
A standards-catalog or evaluator release posts to `POST /v1/evaluations/sweeps`
instead, because those two are what invalidate every repository's last result at once.

Rules arrive as a compiled catalog from `GET /v1/standards/catalog`. This repo does
not check out ecosystem-standards.

Findings are written to the `pipeline_evaluations` table via
`api-kaianolevine-com` with `source=flow_inline` (normal runs) or
`source=flow_hook` (failure/crash hooks) or `source=prefect_webhook`
(Prefect Cloud automation).

## Standards coverage

Rules arrive as a compiled catalog from
`GET https://api.kaianolevine.com/v1/standards/catalog`. That endpoint is also
the answer to "how many rules are there" — it reports `rule_count`, and how
many are checkable, deterministic and LLM-assessed. This section used to carry
those numbers by hand and was stale by ten rules before anyone noticed, so it
no longer states them.

Coverage is not tracked by hand either. **EVAL-007 compares the rule ids this
engine registers against the ids in the published catalog on every sweep**, and
files a `standards_drift` finding for anything on either side without a
counterpart. A rule added to the catalog with no check here, or a check here
for a rule the catalog has retired, shows up as a finding rather than as a
number in a README that someone has to remember to update.

Checks are wired in `run_all_checks` in
`src/evaluator_cog/engine/deterministic/runner.py` and dispatched per repo type
via the `applies_to` list in each rule's catalog entry.

**Checks grouped by subsystem:**
- **File/YAML scans** (majority): pyproject.toml, package.json, .github/workflows/*.yml,
  .env.example, CHANGELOG.md, and similar presence/content checks.
- **AST scans** (Python source): route decorators, Pydantic models, SQLAlchemy
  models, Settings class field parity, Prefect flow/task decorators, public
  docstring coverage.
- **Astro parsing**: `.astro` frontmatter/script-region splitting for FE-006, FE-009, FE-010.

Zipball downloads do not include `.git/` history, so rules that require git log
or tags are not run in evaluator-cog today (see **Not yet implemented**).

**Registered under rule IDs the catalog no longer contains** — these emit findings
against rules that do not exist and must be removed:

| Orphaned check | Note |
|---|---|
| CD-012 | Retired 2026-09 (ADR-008). The check also gives inverted advice — it tells repos to acquire Clerk M2M JWTs and replace static keys, which is now backwards |
| AUTH-001 | Retired 2026-09 (ADR-008), superseded by AUTH-003 |
| TEST-GAP-001 | Pre-existing. Not in the current catalog |

**Not yet implemented** (13 rules — each blocked on a different piece):

| Rule | Blocker |
|---|---|
| VER-001 | Need git history (conventional commits on last 20) — requires full clone |
| VER-002 | Need git history (BREAKING CHANGE on major tags) — requires full clone |
| PRIN-008 | Need git history (fix commits touch tests) — requires full clone |
| AUTH-003 | Replaces retired AUTH-001. Needs route enumeration by registration call (decorators plus `add_api_route`), not by literal path |
| AUTH-004 | New. Needs to verify the guard delegates to `identity.policy` and audits both branches |
| CD-019 | Replaces retired CD-012. The existing CD-012 check gives inverted advice and must be removed, not adapted |
| CD-020 | New. Needs `.releaserc.json` parsing, `uv lock --check`, and a pyproject `dependencies` vs `[tool.uv.sources]` comparison |
| CD-016, CD-017 | Pre-existing gap — no check registered |
| CD-004 | Needs GitHub API (verify pinned action tags exist); rate-limited |
| EVAL-002, EVAL-003, EVAL-006, MONO-003 | Need runtime SQL queries against `pipeline_evaluations` table |
| EVAL-007 | Nothing covers this today. `scripts/check_drift.py` in `ecosystem-standards` does not exist — the repo has no `scripts/` directory. This is the check that would have caught CD-012 and AUTH-001 being retired while checks stayed registered under those IDs |

**LLM-routed rules** (catalog-marked `LLM CHECK.`): META-004, PIPE-013, PIPE-014,
PIPE-015, PRIN-010, XSTACK-005. These pass through the routing layer in
`engine/routing.py` and reach Claude for judgment-based assessment; no
deterministic engine implementation is expected.

**Notes on check behavior:**
- Each check function tolerates missing files/directories gracefully (returns `[]`).
- Rules that over-fire on a specific repo should be handled via a targeted
  entry in that repo's `evaluator.yaml` `exemptions:` section, not by relaxing
  the check globally.

## Triggering an evaluation

Nothing here is scheduled, and nothing calls this service directly. The front
door is api-kaianolevine-com; the evaluator is behind it.

```
repo release → its CI → POST /v1/evaluations/runs   → POST /invoke → handler()
                                    (api)                 (here)

standards or evaluator release
             → its CI → POST /v1/evaluations/sweeps → POST /sweep  → run_fleet_sweep()
```

### From a repository's CI

Add the `evaluate` job to `ci.yml`, after `release`:

```yaml
  evaluate:
    needs: release
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    uses: mini-app-polis/.github/.github/workflows/evaluate.yml@v3
    secrets:
      api-key: ${{ secrets.CI_VALIDATOR_API_KEY }}
```

`scope: fleet` asks for a whole-fleet sweep instead, and belongs to
**ecosystem-standards** and **evaluator-cog** alone — a new rule catalog or a
new evaluator invalidates every repository's last result at once, where every
other release invalidates one. evaluator-cog also passes `wait-seconds: 120`,
because its own release redeploys the evaluator the sweep is about to ask.

The contract is fire-and-forget. CI posts, reads a 202, and exits; the
evaluation runs after the runner is gone. What CI reports is whether the
request landed, never what was found.

### By hand

```bash
curl -X POST https://api.kaianolevine.com/v1/evaluations/runs \
  -H "Authorization: Bearer $CI_VALIDATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"repo": "watcher-cog", "ref": "main"}'
```

`mode` defaults to `deterministic`, which costs no tokens. `mode: "llm"` runs
the deterministic pass first (for `checked_rule_ids`) and then the soft-rule
assessment.

### What a monorepo can't do yet

`/invoke` builds an event carrying exactly one service, so a monorepo with two
apps cannot be evaluated through it — and sibling deduplication (ADR-0002)
needs every app in the workspace to arrive in the same event, so splitting it
into two requests would be worse than not asking. deejaytools-com is therefore
**not** wired to evaluate itself on release; the sweep builds its event
correctly and covers it.

## Inputs and outputs

**Inputs:** A conformance request over HTTP — `POST /invoke` for one
repository, `POST /sweep` for the registry — from which the repo source is
downloaded from GitHub as a zipball. Pipeline run metrics (sets imported,
failed, skipped, track counts) passed directly from calling cogs via
`evaluate_pipeline_run()`. Prefect flow state events received as JSON via
stdin or webhook payload.

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

For **other** cogs. evaluator-cog itself no longer runs under Prefect
(ADR-0004); this is how a Prefect-managed cog reports its own run to
`pipeline_eval`, which is unchanged.

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
