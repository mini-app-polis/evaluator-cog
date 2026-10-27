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
  pass — the checks that grade the inventory, the stored findings and the
  catalog itself, which no repository owns
- `evaluator_cog.engine.deterministic` — file/AST/YAML rule checks
- `evaluator_cog.engine.llm` — soft rule assessment, prompt builders, response parsing

Nothing here is scheduled and nothing is polled. A repository is evaluated when
it releases: its CI posts to `POST /v1/evaluations/runs` on
api-kaianolevine-com, which puts one message on the queue. A standards-catalog
or evaluator release posts to `POST /v1/evaluations/fleet`, which fans out to
one message per repository plus one for the fleet-scoped checks — those two
releases are what invalidate every repository's last result at once.

The process holds no memory between messages and idles at nothing. See
[the migration record](https://github.com/mini-app-polis/ecosystem-standards/blob/main/docs/serverless-migration.md) for how it got here and
[ADR-0004](docs/decisions/ADR-0004-evaluation-on-demand.md) for why it stopped
being scheduled.

Rules arrive as a compiled catalog from `GET /v1/standards/catalog`. This repo does
not check out ecosystem-standards.

Findings are written to the `pipeline_evaluations` table via
`api-kaianolevine-com` with `source=conformance_deterministic` /
`conformance_llm` (findings; an LLM-mode service that could not be evaluated
is posted as `conformance_check`), `source=data_quality` and
`source=standards_drift` (the fleet-scoped checks). Run reports are not
findings: they go to `POST /v1/notify` and are not stored.
`source=flow_inline`, `source=prefect_webhook` and `source=flow_hook` are
legacy values on existing rows; nothing writes them here any more.

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
  models, Settings class field parity, public docstring coverage.
- **Astro parsing**: `.astro` frontmatter/script-region splitting for FE-006, FE-009, FE-010.

Zipball downloads do not include `.git/` history, so rules that require git log
or tags are not run in evaluator-cog today (see **Not yet implemented**).

**Implemented, and where they run.** EVAL-003, EVAL-007, XSTACK-006 and
XSTACK-008 carry `applies_to: None` — they grade the inventory,
the stored findings and the catalog rather than any repository's source, so no
per-repository job owns them. They run in `run_introspection()`, once per fleet
pass, enqueued by `POST /v1/evaluations/fleet` (or on its own by
`POST /v1/evaluations/introspection`). Their findings are
filed against whoever has to act: EVAL-003 under evaluator-cog (it grades the
evaluator's own output), the rest under ecosystem-standards. XSTACK-007
(shared-library pins) is a per-repo check: the stale pin is the consuming
repo's to fix, so its own evaluation reports it and its own release clears it.

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
| CD-004 | Needs GitHub API (verify pinned action tags exist); rate-limited |
| EVAL-002 (table half), EVAL-006 | Need runtime SQL queries against `pipeline_evaluations` table. EVAL-002's source scan for hardcoded versions does run |

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

The API resolves the fleet from the registry and sends one message per
repository. One `run_id` is minted for the whole pass and one catalog version
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

In evaluator-cog the sweep runs after the `deploy` job, which calls the
shared `lambda-deploy.yml` on the release tag and finishes only once AWS
reports the new code's checksum. So a fleet pass is always consumed by the
evaluator the release built. The deploy cannot trigger itself on `release:
published`: semantic-release publishes with `GITHUB_TOKEN`, and GitHub starts
no workflows from that token's events.

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

## Inputs and outputs

**Inputs:** One SQS message, delivered by the event source mapping. Two types:
`evaluation.repository` names a repository and the services it carries, from
which the source is downloaded from GitHub as a zipball;
`evaluation.introspection` asks for the checks that belong to no repository.

**Outputs:** Structured findings written to the `pipeline_evaluations` table
via `POST /v1/evaluations` on api-kaianolevine-com. Each finding includes
`repo`, `run_id`, `severity`, `dimension`, `finding`, `suggestion`,
`standards_version`, and `source`.

## Running locally

Prerequisites: Python 3.11+, uv, and the
[Doppler CLI](https://docs.doppler.com/docs/install-cli). Secrets come from
this repo's own Doppler `dev` config — nothing reads a `.env` file, and local runs
never use `prd`.

```bash
brew install gnupg dopplerhq/cli/doppler   # once per machine
doppler login                              # once per machine

doppler setup                              # once per clone: reads doppler.yaml
uv sync --all-extras
pre-commit install
pre-commit run --all-files
uv run pytest                              # no Doppler needed
uv run check-doppler-keys                  # every required .env.example name is in dev
```

Anything that needs secrets runs under `doppler run -- …`.

## Versioning

Managed by semantic-release. Never manually edit `version` in `pyproject.toml`
or `CHANGELOG.md`.
