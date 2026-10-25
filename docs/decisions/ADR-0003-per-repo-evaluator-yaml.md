# ADR-0003: Per-repo evaluator.yaml for exemptions, deferrals, and type scoping

Date: 2026-04-10

## Status

Accepted

**Note (2026-10).** The decision stands; two details under Consequences are
dated. This repo's `evaluator.yaml` no longer carries the PIPE-006, PIPE-002
or CD-012 deferrals it cites, and `ecosystem.yaml` no longer records monorepo
membership — the ecosystem has no monorepo (see
[ADR-0002](./ADR-0002-monorepo-sibling-finding-deduplication.md)). The body below
is left as written.

## Context

Early versions of ecosystem-standards used a single central `ecosystem.yaml`
file (in `ecosystem-standards`) to hold every rule exemption for every
service. This had three problems:

1. **Remote coupling** — changing an exemption for `deejay-cog` required a
   PR to `ecosystem-standards`, then a version bump, then waiting for the
   conformance flow to pick up the new version. Too slow for in-repo
   refactors that temporarily need a deferral.
2. **No rationale proximity** — the reason for a deferral lives away from
   the code the deferral covers. Over time the reasons drift or become
   stale.
3. **No type scoping** — rules that only apply to certain service types
   (e.g. `pipeline-cog` vs `astro-site`) had to be excluded per-service in
   the central file.

## Decision

Each repo owns an `evaluator.yaml` at its root with four sections:

- `type:` — service type used to scope which rules apply.
- `traits:` — optional modifiers that further narrow rule applicability
  (e.g. `multi-flow`, `pipeline-cog-evaluator`).
- `exemptions:` — rules that legitimately do not apply, with free-text
  `reason` (surfaced in the finding when the checker skips).
- `deferrals:` — rules that apply but are intentionally not yet addressed,
  with `reason` and optional `until` target date.

`load_evaluator_config()` reads this file at check time, falling back to
the service's `type` in `ecosystem.yaml` when `evaluator.yaml` is absent.

## Consequences

- Exemption rationale lives in the same PR as the code it covers.
- Changing a deferral is a repo-local PR — no cross-repo coordination.
- The central `ecosystem.yaml` shrinks to service metadata only (repo,
  type, monorepo membership). Its `check_exceptions` field still works
  for backwards compatibility but is deprecated.
- Evaluator-cog itself uses this system (see this repo's `evaluator.yaml`
  for PIPE-006, PIPE-002, CD-012 deferrals).
- Downside: a repo with no `evaluator.yaml` inherits whatever its
  `ecosystem.yaml` row says, which can be surprising on first
  onboarding. Mitigation: `load_evaluator_config` logs which source it
  resolved from at every run.
