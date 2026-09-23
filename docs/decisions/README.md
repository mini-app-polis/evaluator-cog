# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for this
repository. ADRs document significant architectural decisions, the
context around them, and their consequences.

## Format

Each ADR is a markdown file named `NNNN-title-in-kebab-case.md` where
`NNNN` is a zero-padded sequence number starting at `0001`.

## Template

```markdown
# NNNN. Title of the decision

Date: YYYY-MM-DD

## Status

Proposed | Accepted | Superseded by [NNNN](./NNNN-other.md)

## Context

What is the issue that we're seeing that is motivating this decision?

## Decision

What is the change that we're actually proposing or doing?

## Consequences

What becomes easier or more difficult to do because of this change?
```

## Index

- [0001. Parameterized single conformance flow instead of two deployments](./ADR-0001-parameterized-single-conformance-flow.md)
- [0002. Deduplicate findings across monorepo sibling apps](./ADR-0002-monorepo-sibling-finding-deduplication.md) — superseded
- [0003. Per-repo evaluator.yaml for exemptions, deferrals, and type scoping](./ADR-0003-per-repo-evaluator-yaml.md)
- [0004. Evaluation on demand, behind an HTTP handler](./ADR-0004-evaluation-on-demand.md)
