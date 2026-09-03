"""Shared GitHub Actions workflow parser.

Six rules (SEC-001..005, CD-020) ask structurally similar questions of
``.github/workflows/``: does a step matching some ``uses`` or ``run``
pattern exist, is it reached by a given trigger, and is it allowed to
fail. Before this module each of those was a substring scan over the
raw YAML text, which cannot answer the last two questions at all — a
``continue-on-error: true`` step and a gating step look identical to
``"gitleaks" in text``.

The parser flattens every workflow into ``Workflow → Job → Step`` and
exposes the three predicates the rules actually need: trigger
membership, ``uses``/``run`` matching, and effective
``continue-on-error`` (a step is non-gating if either it or its job
sets the flag).

Two YAML quirks are handled here so no caller has to:

  - ``on:`` parses to the boolean ``True`` under YAML 1.1 truthiness,
    not the string ``"on"``. ``_triggers_of`` looks for both.
  - ``continue-on-error`` may be a literal boolean, the strings
    ``"true"``/``"false"``, or a ``${{ }}`` expression. Only a literal
    truthy value counts as non-gating; an expression is treated as
    gating, because we cannot evaluate it and the conservative reading
    is that the step blocks.

Never raises. An unparseable workflow is skipped, not flagged — YAML
validity is not what these rules are about.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Step:
    """One step in one job."""

    workflow: str
    job_id: str
    index: int
    name: str = ""
    uses: str = ""
    run: str = ""
    continue_on_error: bool = False
    job_continue_on_error: bool = False

    @property
    def is_gating(self) -> bool:
        """True when a failure of this step fails the build."""
        return not (self.continue_on_error or self.job_continue_on_error)

    @property
    def location(self) -> str:
        return f"{self.workflow}::{self.job_id}[{self.index}]"

    def uses_matches(self, pattern: str) -> bool:
        """Glob-match the ``uses:`` reference, e.g. ``gitleaks/gitleaks-action@*``."""
        if not self.uses:
            return False
        return fnmatch.fnmatch(self.uses.strip(), pattern)

    def run_invokes(self, command: str) -> bool:
        """True when the ``run:`` script invokes ``command`` as a command.

        Word-boundary matched so ``syft`` does not match ``syft-config``
        and ``npm audit`` matches across the intervening whitespace of a
        wrapped line. Substring matching was the old behaviour and it
        fired on comments and on unrelated identifiers.
        """
        if not self.run:
            return False
        pattern = r"\b" + r"\s+".join(re.escape(p) for p in command.split()) + r"\b"
        return re.search(pattern, self.run) is not None


@dataclass
class Job:
    """One job in one workflow."""

    workflow: str
    job_id: str
    uses: str = ""
    continue_on_error: bool = False
    steps: list[Step] = field(default_factory=list)


@dataclass
class Workflow:
    """One parsed workflow file."""

    path: Path
    rel: str
    triggers: set[str] = field(default_factory=set)
    jobs: list[Job] = field(default_factory=list)

    @property
    def steps(self) -> list[Step]:
        return [s for j in self.jobs for s in j.steps]


def _truthy(value: object) -> bool:
    """Literal-truthy only. A ``${{ }}`` expression is not literal."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _triggers_of(data: dict) -> set[str]:
    """Event names under ``on:``.

    ``on`` is YAML 1.1 truthy, so PyYAML gives the key as the boolean
    ``True``. Both spellings are consulted. The value may be a string
    (``on: push``), a sequence, or a mapping.
    """
    raw = None
    for key in ("on", True):
        if key in data:
            raw = data[key]
            break
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(x) for x in raw if isinstance(x, (str, int))}
    if isinstance(raw, dict):
        return {str(k) for k in raw}
    return set()


def load_workflows(repo_path: Path) -> list[Workflow]:
    """Parse every workflow under ``.github/workflows``.

    Returns [] when the directory is absent. Unparseable files are
    skipped silently.
    """
    wf_dir = repo_path / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []

    workflows: list[Workflow] = []
    paths = sorted(set(wf_dir.rglob("*.yml")) | set(wf_dir.rglob("*.yaml")))
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            rel = str(path.relative_to(repo_path))
        except ValueError:
            rel = path.name

        wf = Workflow(path=path, rel=rel, triggers=_triggers_of(data))

        raw_jobs = data.get("jobs")
        if isinstance(raw_jobs, dict):
            for job_id, job_data in raw_jobs.items():
                if not isinstance(job_data, dict):
                    continue
                job_coe = _truthy(job_data.get("continue-on-error"))
                job = Job(
                    workflow=rel,
                    job_id=str(job_id),
                    uses=str(job_data.get("uses") or ""),
                    continue_on_error=job_coe,
                )
                raw_steps = job_data.get("steps")
                if isinstance(raw_steps, list):
                    for i, step_data in enumerate(raw_steps):
                        if not isinstance(step_data, dict):
                            continue
                        job.steps.append(
                            Step(
                                workflow=rel,
                                job_id=str(job_id),
                                index=i,
                                name=str(step_data.get("name") or ""),
                                uses=str(step_data.get("uses") or ""),
                                run=str(step_data.get("run") or ""),
                                continue_on_error=_truthy(
                                    step_data.get("continue-on-error")
                                ),
                                job_continue_on_error=job_coe,
                            )
                        )
                wf.jobs.append(job)

        workflows.append(wf)

    return workflows


def with_trigger(workflows: list[Workflow], trigger: str) -> list[Workflow]:
    """Workflows whose ``on:`` includes ``trigger``."""
    return [w for w in workflows if trigger in w.triggers]


def find_steps(
    workflows: list[Workflow],
    *,
    uses_patterns: tuple[str, ...] = (),
    run_commands: tuple[str, ...] = (),
) -> list[Step]:
    """Every step matching any ``uses`` glob or any ``run`` command."""
    matched: list[Step] = []
    for wf in workflows:
        for step in wf.steps:
            if any(step.uses_matches(p) for p in uses_patterns) or any(
                step.run_invokes(c) for c in run_commands
            ):
                matched.append(step)
    return matched


def reusable_workflow_jobs(
    workflows: list[Workflow], name_tokens: tuple[str, ...]
) -> list[Job]:
    """Jobs delegating to a reusable workflow whose ref names a token.

    ``uses: <org>/<repo>/.github/workflows/<name>.yml@<ref>`` at the job
    level. The fleet is expected to converge on one shared workflow, so
    a rule satisfied by an inline step is equally satisfied by a call
    out to a shared one.
    """
    hits: list[Job] = []
    for wf in workflows:
        for job in wf.jobs:
            ref = job.uses.lower()
            if "/.github/workflows/" not in ref:
                continue
            if any(token in ref for token in name_tokens):
                hits.append(job)
    return hits
