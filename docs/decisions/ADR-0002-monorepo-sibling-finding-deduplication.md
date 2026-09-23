# 0002. Deduplicate findings across monorepo sibling apps

Date: 2026-04-08

## Status

Superseded (2026-09). The ecosystem no longer has a monorepo:
`deejaytools-com` was split into `deejaytools-com` and `deejaytools-api`,
and ecosystem-standards retired the MONO rules and the `monorepos:`
registry. The deduplication, the MONO-003 check and every monorepo code
path were removed. Kept as the record of why they existed.

## Context

A single monorepo (e.g. `deejaytools-com`) contains multiple services that
share a workspace root: `apps/api`, `apps/app`, and so on. The deterministic
checks run per-service, so a workspace-level issue — a missing root
`README.md`, a root `package.json` lockfile problem, a shared config
file — produces the same finding once per sibling. Three services means
three identical `DOC-001` findings on the same underlying file.

This duplicates rows in `pipeline_evaluations` and creates noise in the
conformance dashboard where the operator sees the same problem attributed
to three different services.

## Decision

In `conformance_check_flow` (monorepo branch), after collecting
`findings_by_service` for all sibling apps, run `_deduplicate_sibling_findings`:

- Choose the first service in the monorepo service list as the **primary**.
- For each sibling, drop findings whose `(rule_id, finding)` key is already
  present on the primary. Append a tag `(also affects <sibling_id>)` to the
  primary's finding text so attribution is preserved.
- Findings unique to a sibling are kept.

The primary is deterministic (order-of-declaration in `ecosystem.yaml`), so
the same service stays primary across runs.

## Consequences

- A single workspace-level issue posts once with full attribution rather
  than N times.
- Side effect: if the primary service is genuinely healthy but its siblings
  have issues they share, the finding lives on the primary's row. Acceptable
  because the `(also affects ...)` tag preserves visibility.
- Only applied to the deterministic pass. The LLM pass is already scoped
  per-service and does not have the same duplication pattern.
- If monorepo layouts ever grow to have multiple independent roots
  (workspaces-of-workspaces), this logic needs to become workspace-scoped
  rather than monorepo-scoped.
