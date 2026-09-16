# evaluator-cog

Post-pipeline AI evaluation cog for the MiniAppPolis ecosystem. Evaluates
pipeline runs against the ecosystem standards document and posts structured
findings to api-kaianolevine-com.

## Overview

An AWS Lambda behind an SQS queue, two engine modules:
- `evaluator_cog.adapters.lambda_worker` — the entry point. The event source
  mapping hands it a message; it returns the ones that must come back. It
  serves no HTTP and listens on nothing
- `evaluator_cog.adapters.queue` — what a message means. Parses it, dispatches
  on type, and reports the run's outcome. Shared with the entrypoint, which
  differs only in where the body comes from and who deletes it
- `evaluator_cog.flows.conformance` — `handler(event)` is the unit of work: one
  repository, downloaded as a **zipball** from GitHub and run through the
  deterministic and/or LLM checks. `run_introspection()` is the rest of a fleet
  pass — the six checks that grade the inventory, the stored findings and the
  catalog itself, which no repository owns
- `evaluator_cog.flows.pipeline_eval` — post-run behavioral evaluation; calls
  Claude, posts findings. A library module called in-process by other cogs,
  not a served deployment
- `evaluator_cog.engine.deterministic` — file/AST/YAML rule checks (100+ rules)
- `evaluator_cog.engine.llm` — soft rule assessment, prompt builders, response parsing

Nothing here is scheduled and nothing is polled. A repository is evaluated when
it releases: its CI posts to `POST /v1/evaluations/runs` on
api-kaianolevine-com, which puts one message on the queue. A standards-catalog
or evaluator release posts to `POST /v1/evaluations/fleet`, which fans out to
one message per repository plus one for the fleet-scoped checks — those two
releases are what invalidate every repository's last result at once.

The process holds no memory between messages and idles at nothing. See
[the migration plan](docs/serverless-migration.md) for how it got here and
[ADR-0004](docs/decisions/ADR-0004-evaluation-on-demand.md) for why it stopped
being scheduled.

Rules arrive as a compiled catalog from `GET /v1/standards/catalog`. This repo does
not check out ecosystem-standards.

Findings are written to the `pipeline_evaluations` table via
`api-kaianolevine-com` with `source=flow_inline` (run reports),
`source=conformance_deterministic` / `conformance_check` (findings),
`source=data_quality` and `source=standards_drift` (the fleet-scoped checks).
`source=prefect_webhook` and `source=flow_hook` are legacy values on existing
rows; nothing writes them here any more.

## Standards coverage

Rules arrive as a compiled catalog from
`GET https://api.kaianolevine.com/v1/standards/catalog`. That endpoint is also
the answer to "how many rules are there" — it reports `rule_count`, and how
many are checkable, deterministic and LLM-assessed. This section used to carry
those numbers by hand and was stale by ten rules before anyone noticed, so it
no longer states them.

Coverage is not tracked by hand either. **EVAL-007 compares the rule ids this
engine registers against the ids in the published catalog on every fleet
pass**, and
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

**Implemented, and where they run.** EVAL-003, MONO-003, EVAL-007, XSTACK-006,
XSTACK-007 and XSTACK-008 carry `applies_to: None` — they grade the inventory,
the stored findings and the catalog rather than any repository's source, so no
per-repository job owns them. They run in `run_introspection()`, once per fleet
pass, asked for by `POST /v1/evaluations/introspection`.

XSTACK-008 is the odd one: it reports which registered repositories failed to
resolve, which it reads from the rows a pass wrote. It grades the **previous**
pass rather than the one being dispatched — handing it the current one is a
race it always loses, since the introspection job is small and the repository
jobs each clone a repository.

**Not yet implemented** (each blocked on a different piece):

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
| EVAL-002, EVAL-006 | Need runtime SQL queries against `pipeline_evaluations` table |

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

Nothing here is scheduled, and nothing calls this service directly — it has no
address to call. The front door is api-kaianolevine-com; the queue is between
them.

```
repo release → its CI → POST /v1/evaluations/runs  → 1 message  → handler()
                                 (api)                  (SQS)       (Lambda)

standards or evaluator release
             → its CI → POST /v1/evaluations/fleet → N messages → handler() ×N
                                                   + 1 message  → run_introspection()
```

The API resolves the fleet from the registry and sends each repository its
services already grouped, so a monorepo arrives as one message carrying every
app in it. One `run_id` is minted for the whole pass and one catalog version
pinned, so every repository in it is graded against the same rules.

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

`scope: fleet` asks for a whole-fleet pass instead, and belongs to
**ecosystem-standards** and **evaluator-cog** alone — a new rule catalog or a
new evaluator invalidates every repository's last result at once, where every
other release invalidates one.

evaluator-cog also passes `wait-seconds: 120`, and the reason changed with the
runtime. It used to be that the release redeployed the container the request
was about to reach. Now the release triggers `deploy-worker.yml` in parallel
with `evaluate`, so without the wait a fleet pass can be consumed by the
Lambda code the release is in the middle of replacing.

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

### Monorepos

A monorepo is **one** job carrying every app in it. Sibling deduplication
(ADR-0002) treats an identical finding on two apps as a single issue and cannot
know that until every app has been evaluated, so they have to arrive together
or not at all.

A fleet pass gets this right: the API groups from the registry before sending,
so deejaytools-com arrives as one message with both apps. A release-triggered
`POST /v1/evaluations/runs` still names one repository and synthesises a single
service from it, which is correct for a plain repo and why deejaytools-com is
**not** wired to evaluate itself on release — the fleet pass covers it.

The failure mode if the grouping is ever lost is worth knowing, because nothing
raises: `monorepo_root`, the workspace `package.json` and `monorepo_context` all
resolve to `None`, `_deduplicate_sibling_findings` never fires, and the same
finding lands once per app looking like a clean run. The tell is a repository
count — 16 active services group into 15 jobs, so a count of 16 means the
grouping did not survive.

## Inputs and outputs

**Inputs:** One SQS message, delivered by the event source mapping. Two types:
`evaluation.repository` names a repository and the services it carries, from
which the source is downloaded from GitHub as a zipball;
`evaluation.introspection` asks for the checks that belong to no repository.
Separately, pipeline run metrics (sets imported, failed, skipped, track counts)
passed directly from calling cogs via `evaluate_pipeline_run()` — a library
call, not a message.

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
