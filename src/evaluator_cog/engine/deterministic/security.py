"""Security-posture rule checks (SEC-001..006).

These six rules answer one question each about how a repo defends
itself against secrets and vulnerable dependencies:

  - SEC-001 a pre-commit hook scans for secrets before they are pushed;
  - SEC-002 secret scanning also runs on pull-request diffs, gating;
  - SEC-003 a dependency vulnerability scan runs in CI, gating;
  - SEC-004 some static analysis workflow exists (gating not required);
  - SEC-005 an SBOM is produced and retained as a build artifact;
  - SEC-006 the catalog declares vulnerability response deadlines.

Five of the six read ``.github/workflows/``. None of them parse YAML by
hand: ``_workflows.load_workflows`` already flattens every workflow into
``Workflow -> Job -> Step`` and answers the two questions a substring
scan cannot — is this step reached by the trigger we care about, and
does a failure of this step actually fail the build. "Gating" throughout
this module means ``Step.is_gating``: neither the step nor its job sets
``continue-on-error: true``. A scan that cannot fail the build is not a
control, which is why SEC-002 and SEC-003 treat a non-gating match as a
violation rather than a pass.

SEC-006 is the odd one out: it reads ``index.yaml`` at the repo root and
is scoped to the standards repo, in the same way as the META rules.

Nothing here raises. Missing directories, unreadable files and
unparseable YAML are reported as findings or skipped, never propagated —
scope filtering by ``applies_to`` is the dispatcher's job, not ours.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)
from evaluator_cog.engine.deterministic._workflows import (
    Step,
    Workflow,
    find_steps,
    load_workflows,
    reusable_workflow_jobs,
    with_trigger,
)

_DIMENSION = "security_posture"


def _ci_root(repo_path: Path, monorepo_root: Path | None) -> Path:
    """Where this service's CI actually lives.

    A monorepo service is evaluated at its own subdirectory — the
    per_app strategy hands the check ``apps/api`` — but its workflows
    and pre-commit hook sit at the repository root, one set covering
    every app. ecosystem.yaml says so for deejaytools-com in as many
    words: "CI is evaluated at the repo root."

    Without this, SEC-001 through SEC-005 looked for
    ``apps/api/.github/workflows`` and reported five findings per app
    against a monorepo whose CI was wired correctly at the root.
    """
    return monorepo_root or repo_path


# SEC-001: matched against the `repo:` URL of each pre-commit entry
# rather than against hook ids, because the ids differ between the three
# tools and churn between their releases while the URL does not.
_PRECOMMIT_SCANNERS = ("gitleaks", "detect-secrets", "trufflehog")

# SEC-002
_SECRET_USES = ("gitleaks/gitleaks-action@*",)
_SECRET_RUNS = ("gitleaks", "trufflehog", "detect-secrets")
_SECRET_WORKFLOW_TOKENS = ("security", "secret")

# SEC-003
_PY_AUDIT_RUNS = ("pip-audit",)
_TS_AUDIT_RUNS = ("pnpm audit", "npm audit")

# SEC-004
_SAST_USES = ("github/codeql-action/analyze@*",)
_SAST_RUNS = ("semgrep", "bandit")

# SEC-005
_SBOM_USES = ("anchore/sbom-action@*",)
_SBOM_RUNS = ("syft", "cyclonedx")
_UPLOAD_USES = "actions/upload-artifact@*"


def _describe_step(step: Step) -> str:
    """Human-readable identification of a step for a finding message.

    Prefers the step's ``name:`` because that is what a reader will look
    for in the workflow file; falls back to the ``uses:`` reference, and
    finally to the first non-blank line of the ``run:`` script.
    """
    if step.name:
        return step.name
    if step.uses:
        return f"uses: {step.uses}"
    for line in step.run.splitlines():
        if line.strip():
            return f"run: {line.strip()}"
    return "unnamed step"


def _why_not_gating(step: Step) -> str:
    """Say whether the step itself or its job disabled build failure.

    The two are worth distinguishing in the finding because they are
    fixed in different places in the YAML, and a job-level flag silently
    de-gates every step in the job rather than just this one.
    """
    if step.continue_on_error and step.job_continue_on_error:
        return f"both the step and job `{step.job_id}` set continue-on-error: true"
    if step.continue_on_error:
        return "the step sets continue-on-error: true"
    return f"its job `{step.job_id}` sets continue-on-error: true"


def _workflow_list(workflows: list[Workflow]) -> str:
    return ", ".join(w.rel for w in workflows) or "none"


def _first_error_line(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else exc.__class__.__name__


def check_sec_001(repo_path: Path, monorepo_root: Path | None = None) -> list[Finding]:
    """SEC-001: a pre-commit hook scans for secrets before they are pushed.

    Reads ``.pre-commit-config.yaml`` at the repo root and passes when any
    entry under ``repos[].repo`` names gitleaks, detect-secrets or
    trufflehog. Matching is on the repo URL, not the hook id, exactly as
    check_notes requires: the three tools spell their hook ids
    differently (``gitleaks`` vs ``detect-secrets`` vs
    ``trufflehog``-with-args) and rename them between releases, whereas
    the repository URL is stable.

    An absent config file is a failure — the rule is about the hook
    existing, and no config means no hook. An unreadable or syntactically
    invalid config is also reported: we cannot confirm the hook, and
    silently passing a repo whose pre-commit config does not load would
    be the wrong default for a security rule.
    """
    CHECK_ID = "SEC-001"
    findings: list[Finding] = []
    config = _ci_root(repo_path, monorepo_root) / ".pre-commit-config.yaml"

    if not config.is_file():
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                "No .pre-commit-config.yaml at the repo root, so no pre-commit "
                "secret scanning hook is configured.",
                "Create .pre-commit-config.yaml at the repo root with a repos: "
                "entry for https://github.com/gitleaks/gitleaks (or "
                "detect-secrets / trufflehog), then run `pre-commit install` so "
                "the hook runs before every commit.",
            )
        )
        return findings

    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f".pre-commit-config.yaml could not be read or parsed "
                f"({_first_error_line(exc)}), so the secret scanning hook "
                f"cannot be confirmed.",
                "Fix the YAML syntax or file permissions of "
                ".pre-commit-config.yaml so the secret scanning hook can be "
                "verified, then re-run pre-commit locally to confirm it loads.",
            )
        )
        return findings

    declared: list[str] = []
    raw_repos = data.get("repos") if isinstance(data, dict) else None
    if isinstance(raw_repos, list):
        for entry in raw_repos:
            if not isinstance(entry, dict):
                continue
            repo_url = str(entry.get("repo") or "").strip()
            if repo_url:
                declared.append(repo_url)

    if any(tool in url.lower() for url in declared for tool in _PRECOMMIT_SCANNERS):
        return findings

    listing = ", ".join(declared) if declared else "no repos: entries at all"
    findings.append(
        _finding(
            CHECK_ID,
            "WARN",
            _DIMENSION,
            f".pre-commit-config.yaml declares {listing} — none of these is a "
            f"secret scanner (gitleaks, detect-secrets or trufflehog).",
            "Add a repos: entry for https://github.com/gitleaks/gitleaks "
            "(or detect-secrets / trufflehog) to .pre-commit-config.yaml and "
            "run `pre-commit install` so secrets are caught before they land "
            "in a commit.",
        )
    )
    return findings


def check_sec_002(repo_path: Path, monorepo_root: Path | None = None) -> list[Finding]:
    """SEC-002: secret scanning runs on pull-request diffs and can fail the build.

    Restricted to workflows whose ``on:`` includes ``pull_request`` — a
    secret scan that only runs on ``push`` to main finds the secret after
    it is already merged, which is not the control this rule describes.
    Within those workflows a match is a step using
    ``gitleaks/gitleaks-action@*`` or running gitleaks, trufflehog or
    detect-secrets.

    A job delegating to a reusable workflow whose reference names
    ``security`` or ``secret`` also satisfies the rule. The fleet is
    expected to converge on one shared scanning workflow rather than
    sixteen copies of the same step, and check_notes is explicit that the
    check should not punish that; we cannot see inside the called
    workflow, so the name is the only signal available.

    Gating is enforced last, and separately from existence, so the
    finding tells the reader which of the two things is wrong. A match is
    only accepted when at least one matching step (or reusable job) is
    gating; if every match is de-gated we emit one finding per match
    naming whether the step or its job carries the flag.
    """
    CHECK_ID = "SEC-002"
    findings: list[Finding] = []
    workflows = load_workflows(_ci_root(repo_path, monorepo_root))

    if not workflows:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                "No parseable workflow files under .github/workflows/, so "
                "secret scanning cannot run on pull-request diffs.",
                "Add .github/workflows/ci.yml with an `on: pull_request` "
                "trigger and a gitleaks step (uses: "
                "gitleaks/gitleaks-action@v2) so every pull request is scanned "
                "for secrets before merge.",
            )
        )
        return findings

    pr_workflows = with_trigger(workflows, "pull_request")
    if not pr_workflows:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"No workflow under .github/workflows/ triggers on "
                f"pull_request (found: {_workflow_list(workflows)}), so secret "
                f"scanning never sees a pull-request diff.",
                "Add `pull_request` to the `on:` block of the workflow that "
                "runs secret scanning, so gitleaks (or trufflehog / "
                "detect-secrets) executes against every PR diff.",
            )
        )
        return findings

    steps = find_steps(
        pr_workflows, uses_patterns=_SECRET_USES, run_commands=_SECRET_RUNS
    )
    reusable = reusable_workflow_jobs(pr_workflows, _SECRET_WORKFLOW_TOKENS)

    if not steps and not reusable:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"Pull-request workflows ({_workflow_list(pr_workflows)}) "
                f"contain no secret scanning step — nothing uses "
                f"gitleaks/gitleaks-action, runs gitleaks / trufflehog / "
                f"detect-secrets, or calls a shared security workflow.",
                "Add a step `uses: gitleaks/gitleaks-action@v2` (or a `run:` "
                "invoking trufflehog / detect-secrets) to a job in a "
                "pull_request workflow, or call the shared "
                "org/.github/workflows/security.yml reusable workflow.",
            )
        )
        return findings

    if any(step.is_gating for step in steps) or any(
        not job.continue_on_error for job in reusable
    ):
        return findings

    for step in steps:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"Secret scanning step {step.location} "
                f"({_describe_step(step)}) cannot fail the build: "
                f"{_why_not_gating(step)}.",
                f"Remove `continue-on-error: true` from the secret scanning "
                f"step at {step.location} (and from job `{step.job_id}` if it "
                f"is set there) so a detected secret blocks the pull request "
                f"instead of being reported and ignored.",
            )
        )
    for job in reusable:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"Job {job.workflow}::{job.job_id} calls the shared secret "
                f"scanning workflow ({job.uses}) but sets "
                f"continue-on-error: true, so its result cannot fail the "
                f"build.",
                f"Remove `continue-on-error: true` from job `{job.job_id}` in "
                f"{job.workflow} so a failure of the shared secret scanning "
                f"workflow blocks the pull request.",
            )
        )
    return findings


def check_sec_003(repo_path: Path, monorepo_root: Path | None = None) -> list[Finding]:
    """SEC-003: a dependency vulnerability scan runs in CI and gates the build.

    Which scanner is expected follows the manifests present: a repo with
    ``pyproject.toml`` must run ``pip-audit``; a repo with
    ``package.json`` must run ``pnpm audit`` or ``npm audit``; a repo
    with both is satisfied by either, so a polyglot repo is not asked to
    run two scanners to pass one rule.

    check_notes explicitly forbids parsing the severity flag and
    comparing it to the SEC-006 deadlines: the flag spellings differ
    between pip-audit, pnpm and npm and change between versions, so any
    such comparison would be wrong more often than the thing it detects.
    Threshold agreement is left to review; this check answers only
    whether a gating scan runs at all.

    Unlike SEC-002 no trigger filter is applied — check_notes says
    "parse ``.github/workflows/``" without narrowing to pull_request, and
    a scheduled or push-triggered audit is a legitimate shape of this
    control. A repo with neither manifest returns no findings: the rule's
    two branches are both keyed on a manifest, and there is no third
    ecosystem for it to demand a scanner from.
    """
    CHECK_ID = "SEC-003"
    findings: list[Finding] = []

    is_python = (repo_path / "pyproject.toml").is_file()
    is_typescript = (repo_path / "package.json").is_file()
    if not is_python and not is_typescript:
        return findings

    expected: list[str] = []
    run_commands: tuple[str, ...] = ()
    if is_python:
        expected.append("`pip-audit`")
        run_commands += _PY_AUDIT_RUNS
    if is_typescript:
        expected.append("`pnpm audit` or `npm audit`")
        run_commands += _TS_AUDIT_RUNS
    expectation = " or ".join(expected)

    workflows = load_workflows(_ci_root(repo_path, monorepo_root))
    if not workflows:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"No parseable workflow files under .github/workflows/, so the "
                f"dependency vulnerability scan ({expectation}) does not run "
                f"in CI.",
                f"Add a CI job under .github/workflows/ with a step running "
                f"{expectation}, and leave it gating so a vulnerable "
                f"dependency fails the build.",
            )
        )
        return findings

    steps = find_steps(workflows, run_commands=run_commands)
    # A job delegating to the fleet's shared security workflow satisfies
    # this the same way an inline step does — the same escape hatch
    # SEC-002 has carried since it landed. Either shape is conformant and
    # neither is required: a repo may run the audit itself or call the
    # shared workflow. The check reads a reference rather than steps in
    # that case, which is why the repo hosting the shared workflow is
    # registered and evaluated in its own right.
    reusable = reusable_workflow_jobs(workflows, _SECRET_WORKFLOW_TOKENS)

    if not steps and not reusable:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"No step in .github/workflows/ ({_workflow_list(workflows)}) "
                f"runs a dependency vulnerability scan — expected a `run:` "
                f"invoking {expectation}, or a job calling a shared security "
                f"workflow.",
                f"Add a step running {expectation} to a CI job so every build "
                f"scans its resolved dependencies, and do not set "
                f"continue-on-error on it — or call the fleet's shared "
                f"security workflow, which runs it for you.",
            )
        )
        return findings

    # Gating applies to the delegating job exactly as it does to a step.
    if any(step.is_gating for step in steps) or any(
        not job.continue_on_error for job in reusable
    ):
        return findings

    for step in steps:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"Dependency vulnerability scan at {step.location} "
                f"({_describe_step(step)}) cannot fail the build: "
                f"{_why_not_gating(step)}.",
                f"Remove `continue-on-error: true` from the audit step at "
                f"{step.location} (and from job `{step.job_id}` if it is set "
                f"there) so a vulnerable dependency blocks the build rather "
                f"than producing an advisory nobody reads.",
            )
        )
    for job in reusable:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"Job {job.workflow}::{job.job_id} calls the shared security "
                f"workflow ({job.uses}) but sets continue-on-error: true, so "
                f"the dependency vulnerability scan it runs cannot fail the "
                f"build.",
                f"Remove `continue-on-error: true` from job `{job.job_id}` in "
                f"{job.workflow} so a vulnerable dependency found by the "
                f"shared security workflow blocks the build.",
            )
        )
    return findings


def check_sec_004(repo_path: Path, monorepo_root: Path | None = None) -> list[Finding]:
    """SEC-004: a static analysis workflow is present.

    Passes on any step using ``github/codeql-action/analyze@*`` or
    running semgrep or bandit, anywhere under ``.github/workflows/``.
    The ``analyze`` action is matched rather than ``init`` or
    ``autobuild`` because a CodeQL setup that never reaches the analyze
    step produces no results.

    Gating is deliberately not part of this rule — check_notes says so
    explicitly. SAST is noisy enough that fleets commonly land it
    non-blocking first and tighten later, so a ``continue-on-error: true``
    semgrep step still satisfies SEC-004, unlike SEC-002 and SEC-003.
    """
    CHECK_ID = "SEC-004"
    findings: list[Finding] = []
    workflows = load_workflows(_ci_root(repo_path, monorepo_root))

    if not workflows:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                "No parseable workflow files under .github/workflows/, so no "
                "static analysis (CodeQL, semgrep or bandit) runs in CI.",
                "Add .github/workflows/codeql.yml running "
                "github/codeql-action/init followed by "
                "github/codeql-action/analyze, or a step running semgrep or "
                "bandit over the source tree.",
            )
        )
        return findings

    # An inline SAST step, or a job delegating to the shared security
    # workflow that runs one. Gating is not part of this rule either way.
    if find_steps(
        workflows, uses_patterns=_SAST_USES, run_commands=_SAST_RUNS
    ) or reusable_workflow_jobs(workflows, _SECRET_WORKFLOW_TOKENS):
        return findings

    findings.append(
        _finding(
            CHECK_ID,
            "WARN",
            _DIMENSION,
            f"No static analysis step found in .github/workflows/ "
            f"({_workflow_list(workflows)}) — nothing uses "
            f"github/codeql-action/analyze or runs semgrep or bandit.",
            "Add a github/codeql-action/analyze step (preceded by "
            "github/codeql-action/init) to a CI job, or a step running semgrep "
            "or bandit. The step may stay non-blocking; SEC-004 only requires "
            "that it runs.",
        )
    )
    return findings


def check_sec_005(repo_path: Path, monorepo_root: Path | None = None) -> list[Finding]:
    """SEC-005: an SBOM is generated per build and retained as an artifact.

    Both halves — generation (``anchore/sbom-action@*``, or a ``run:``
    invoking syft or cyclonedx) and retention
    (``actions/upload-artifact@*``) — must live in the *same* job. That
    is the whole point of the rule: an SBOM written to a runner's
    filesystem in one job and some unrelated artifact uploaded in
    another leaves no SBOM behind, and jobs do not share a filesystem.
    Iterating ``workflow.jobs`` rather than ``workflow.steps`` is what
    makes this a real check rather than a keyword scan.

    The finding distinguishes the three ways this fails — generation
    present but never uploaded, uploads present but no SBOM generated,
    and neither present — because the remediation differs in each case.
    """
    CHECK_ID = "SEC-005"
    findings: list[Finding] = []
    workflows = load_workflows(_ci_root(repo_path, monorepo_root))

    if not workflows:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                "No parseable workflow files under .github/workflows/, so no "
                "SBOM is generated or retained per build.",
                "Add a job that runs anchore/sbom-action@v0 (or syft / "
                "cyclonedx) and then actions/upload-artifact@v4 in the same "
                "job, so each build leaves a retrievable SBOM.",
            )
        )
        return findings

    # A job delegating to the shared security workflow satisfies this:
    # the call *is* one job, so the rule's same-job requirement holds by
    # construction — whatever generation and upload it does, it does
    # together. Either shape is conformant and neither is required.
    if reusable_workflow_jobs(workflows, _SECRET_WORKFLOW_TOKENS):
        return findings

    sbom_jobs: list[str] = []
    upload_jobs: list[str] = []
    for workflow in workflows:
        for job in workflow.jobs:
            has_sbom = any(
                any(step.uses_matches(p) for p in _SBOM_USES)
                or any(step.run_invokes(c) for c in _SBOM_RUNS)
                for step in job.steps
            )
            has_upload = any(step.uses_matches(_UPLOAD_USES) for step in job.steps)
            if has_sbom and has_upload:
                return findings
            location = f"{workflow.rel}::{job.job_id}"
            if has_sbom:
                sbom_jobs.append(location)
            if has_upload:
                upload_jobs.append(location)

    if sbom_jobs:
        detail = (
            f"SBOM generation runs in {', '.join(sbom_jobs)} but that job "
            f"never uploads it"
        )
        if upload_jobs:
            detail += (
                f"; actions/upload-artifact only appears in a different job "
                f"({', '.join(upload_jobs)}), and jobs do not share a "
                f"filesystem"
            )
        suggestion = (
            f"Add an actions/upload-artifact@v4 step to job "
            f"{sbom_jobs[0].split('::')[-1]} itself, pointing at the SBOM file "
            f"the generation step writes, so the SBOM survives the build."
        )
    elif upload_jobs:
        detail = (
            f"No SBOM is generated anywhere under .github/workflows/ "
            f"({_workflow_list(workflows)}); actions/upload-artifact runs in "
            f"{', '.join(upload_jobs)} but uploads something else"
        )
        suggestion = (
            f"Add an anchore/sbom-action@v0 step (or a `run:` invoking syft / "
            f"cyclonedx) to job {upload_jobs[0].split('::')[-1]} alongside the "
            f"existing upload-artifact step, and upload the generated SBOM."
        )
    else:
        detail = (
            f"No job under .github/workflows/ ({_workflow_list(workflows)}) "
            f"generates an SBOM or uploads one as an artifact"
        )
        suggestion = (
            "Add a build job that runs anchore/sbom-action@v0 (or syft / "
            "cyclonedx) and then actions/upload-artifact@v4 in that same job, "
            "so every build retains an SBOM."
        )

    findings.append(
        _finding(
            CHECK_ID,
            "WARN",
            _DIMENSION,
            f"{detail} — SEC-005 requires SBOM generation and artifact upload "
            f"in the same job.",
            suggestion,
        )
    )
    return findings


def _declared_vulnerability_severities(data: dict) -> set[str]:
    """Names declared by the top-level ``vulnerability_severities:`` block.

    Deliberately *not* the ``severities:`` block. That one grades
    conformance findings — ERROR, WARN, INFO — while these are CVSS
    qualitative ratings as the scanners report them. The two are
    different scales and neither contains the other: CVSS has no ERROR,
    and a conformance finding is never HIGH.

    Validating deadline names against ``severities:`` was the original
    shape of this check, and it made SEC-006 impossible to pass: clause
    (2) requires a HIGH deadline, and HIGH is correctly absent from the
    finding vocabulary, so clause (3) rejected the very entry clause (2)
    demanded. The fix was a second named vocabulary in the catalog
    rather than forcing one list to serve both meanings.

    The block is a mapping of name to prose description, so the names
    are its keys. A sequence spelling is accepted too, because the
    catalog's other enumerations have historically been written both
    ways (see META-003) and the rule cares about the names, not the
    container.
    """
    raw = data.get("vulnerability_severities")
    if isinstance(raw, dict):
        return {str(k) for k in raw}
    if isinstance(raw, list):
        return {str(x) for x in raw if isinstance(x, (str, int))}
    return set()


def check_sec_006(repo_path: Path) -> list[Finding]:
    """SEC-006: vulnerability response deadlines are declared in the catalog.

    Reads ``index.yaml`` at the repo root and requires a top-level
    ``vulnerability_response:`` mapping with a ``deadlines:`` block that
    names at least CRITICAL and HIGH, each mapping to an integer number
    of days. Booleans are rejected as day counts even though ``bool`` is
    a subclass of ``int``, because ``CRITICAL: true`` declares nothing.

    Every severity named in ``deadlines:`` must also appear in the
    top-level ``vulnerability_severities:`` block — a deadline attached
    to a rating the catalog does not define cannot be enforced against
    any scanner output. That block, not ``severities:``, is the
    vocabulary here; see ``_declared_vulnerability_severities``.

    Two decisions check_notes leaves open. First, an absent (or
    unreadable) ``index.yaml`` is reported rather than skipped: the rule
    is scoped to standards-repo, where index.yaml is the catalog root, so
    its absence means the deadlines block is absent. Second, if the
    ``vulnerability_severities:`` block itself is missing we emit one
    finding saying the names cannot be validated, rather than one
    finding per deadline blaming names that may well be correct.
    """
    CHECK_ID = "SEC-006"
    findings: list[Finding] = []
    index_path = repo_path / "index.yaml"

    if not index_path.is_file():
        findings.append(
            _finding(
                CHECK_ID,
                "INFO",
                _DIMENSION,
                "index.yaml is absent from the repo root, so no "
                "vulnerability_response.deadlines block is declared.",
                "Add index.yaml with a top-level vulnerability_response: "
                "block whose deadlines: mapping gives CRITICAL and HIGH an "
                "integer number of days to remediate.",
            )
        )
        return findings

    try:
        data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        findings.append(
            _finding(
                CHECK_ID,
                "INFO",
                _DIMENSION,
                f"index.yaml could not be read or parsed "
                f"({_first_error_line(exc)}), so the "
                f"vulnerability_response.deadlines block cannot be validated.",
                "Fix the YAML syntax or file permissions of index.yaml so the "
                "vulnerability_response.deadlines block can be validated by "
                "the standards CI job.",
            )
        )
        return findings

    if not isinstance(data, dict):
        data = {}

    response = data.get("vulnerability_response")
    if not isinstance(response, dict):
        findings.append(
            _finding(
                CHECK_ID,
                "INFO",
                _DIMENSION,
                "index.yaml declares no top-level vulnerability_response: "
                "mapping, so remediation deadlines are undeclared.",
                "Add a top-level vulnerability_response: mapping to index.yaml "
                "containing a deadlines: block that maps CRITICAL and HIGH to "
                "an integer number of days.",
            )
        )
        return findings

    deadlines = response.get("deadlines")
    if not isinstance(deadlines, dict):
        findings.append(
            _finding(
                CHECK_ID,
                "INFO",
                _DIMENSION,
                "index.yaml has vulnerability_response: but no deadlines: "
                "mapping under it, so no remediation deadline is declared for "
                "any severity.",
                "Add a deadlines: mapping under vulnerability_response: in "
                "index.yaml, giving at least CRITICAL and HIGH an integer "
                "number of days (for example CRITICAL: 7, HIGH: 30).",
            )
        )
        return findings

    for required in ("CRITICAL", "HIGH"):
        if required not in deadlines:
            findings.append(
                _finding(
                    CHECK_ID,
                    "INFO",
                    _DIMENSION,
                    f"vulnerability_response.deadlines in index.yaml does not "
                    f"name {required} (declares: "
                    f"{', '.join(str(k) for k in deadlines) or 'nothing'}).",
                    f"Add {required} to vulnerability_response.deadlines in "
                    f"index.yaml with an integer number of days to remediate, "
                    f"so the deadline is fixed before a finding exists.",
                )
            )
            continue
        days = deadlines[required]
        if isinstance(days, bool) or not isinstance(days, int):
            findings.append(
                _finding(
                    CHECK_ID,
                    "INFO",
                    _DIMENSION,
                    f"vulnerability_response.deadlines.{required} in index.yaml "
                    f"is {days!r}, which is not an integer number of days.",
                    f"Set vulnerability_response.deadlines.{required} in "
                    f"index.yaml to a bare integer number of days (for example "
                    f"{7 if required == 'CRITICAL' else 30}), not a string or "
                    f"a duration expression.",
                )
            )

    declared = _declared_vulnerability_severities(data)
    if not declared:
        findings.append(
            _finding(
                CHECK_ID,
                "INFO",
                _DIMENSION,
                "index.yaml declares no top-level vulnerability_severities: "
                "block, so the ratings named in "
                "vulnerability_response.deadlines cannot be validated against "
                "the catalog's own vocabulary.",
                "Add a top-level vulnerability_severities: block to index.yaml "
                "listing the CVSS ratings the scanners report (CRITICAL, HIGH, "
                "MEDIUM, LOW), so deadline names can be checked against it. Do "
                "not reuse the severities: block — that one grades conformance "
                "findings and is a different scale.",
            )
        )
        return findings

    for name in deadlines:
        if str(name) not in declared:
            findings.append(
                _finding(
                    CHECK_ID,
                    "INFO",
                    _DIMENSION,
                    f"vulnerability_response.deadlines names "
                    f"{str(name)!r}, which is not declared in the top-level "
                    f"vulnerability_severities: block of index.yaml "
                    f"(declared: {', '.join(sorted(declared))}).",
                    f"Either rename the {str(name)!r} deadline to one of the "
                    f"declared ratings, or add {str(name)!r} to the top-level "
                    f"vulnerability_severities: block in index.yaml so the "
                    f"deadline attaches to a rating a scanner can report.",
                )
            )
    return findings
