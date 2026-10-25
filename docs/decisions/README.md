# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for this
repository. ADRs document significant architectural decisions, the
context around them, and their consequences.

## Format

Each ADR is a markdown file named `ADR-NNNN-short-slug.md`, where `NNNN`
is a four-digit, zero-padded sequence number starting at `0001`
(ecosystem-standards DOC-005). Cite an ADR in this repository by its ID
alone, `ADR-0004`; cite one in another repository with that repository's
name first, `ecosystem-standards ADR-0009`.

## Template

```markdown
# ADR-NNNN: Title of the decision

Date: YYYY-MM-DD

## Status

Proposed | Accepted | Superseded by [ADR-NNNN](./ADR-NNNN-other-slug.md)

## Context

What is the issue that we're seeing that is motivating this decision?

## Decision

What is the change that we're actually proposing or doing?

## Consequences

What becomes easier or more difficult to do because of this change?
```

## Index

- [ADR-0001: Parameterized single conformance flow instead of two deployments](./ADR-0001-parameterized-single-conformance-flow.md) — superseded
- [ADR-0002: Deduplicate findings across monorepo sibling apps](./ADR-0002-monorepo-sibling-finding-deduplication.md) — superseded
- [ADR-0003: Per-repo evaluator.yaml for exemptions, deferrals, and type scoping](./ADR-0003-per-repo-evaluator-yaml.md)
- [ADR-0004: Evaluation on demand, behind an HTTP handler](./ADR-0004-evaluation-on-demand.md) — superseded in part (transport is now SQS + Lambda)
