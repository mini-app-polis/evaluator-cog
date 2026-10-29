"""CI / delivery / build / observability rule checks (GitHub Actions, logging, secrets)."""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
    _is_checker_self_source,
    _tracked_paths,
)
from evaluator_cog.engine.deterministic._terraform import (
    dead_letter_queue_names,
    declares_terraform,
    of_type,
    terraform_resources,
    terraform_root,
)

#: CD-026's canonical job names.
#:
#: ``evaluate`` joined the set when conformance became something a repo asks
#: for on its own release rather than something a cron did to it. It is a
#: stage, not a variation on ``test``: it runs after ``release``, it asks
#: about the repository rather than about the change, and nothing waits for
#: the answer. Fifteen repos would otherwise carry the same exemption, which
#: is the shape of a rule that has fallen behind its architecture.
#:
#: ``deploy`` joined when the cogs moved to Lambda: it ships what ``release``
#: versioned, through the shared lambda-deploy.yml, and a stage that must run
#: on the new code says ``needs: deploy``. This repo and deejay-cog both had
#: the job, and this check flagged both on every release.
_CANONICAL_CI_JOBS = frozenset({"security", "test", "release", "deploy", "evaluate"})


def _ci_workflow(repo_path: Path) -> dict | None:
    """Parse ``.github/workflows/ci.yml``, or None when it is unusable.

    A file that will not parse is not a finding here. Something is wrong
    with it, but saying *what* is another rule's job, and a parse error
    reported as a job-naming violation sends the reader to the wrong
    place entirely.
    """
    import yaml

    ci = repo_path / ".github" / "workflows" / "ci.yml"
    if not ci.exists():
        return None
    try:
        loaded = yaml.safe_load(ci.read_text())
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def _declares_workflow_call(workflow: dict) -> bool:
    """True for a reusable workflow, which names its jobs for its own reasons."""
    # PyYAML resolves a bare `on:` key to the boolean True, so check both.
    triggers = workflow.get("on", workflow.get(True))
    if isinstance(triggers, dict):
        return "workflow_call" in triggers
    if isinstance(triggers, list):
        return "workflow_call" in triggers
    return triggers == "workflow_call"


def check_release_gated_on_security(repo_path: Path) -> list[Finding]:
    """CD-025: the release job must list the security job in needs."""
    CHECK_ID = "CD-025"
    findings: list[Finding] = []
    workflow = _ci_workflow(repo_path)
    if workflow is None:
        return findings
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return findings
    if "security" not in jobs or "release" not in jobs:
        return findings

    release = jobs.get("release")
    needs = release.get("needs") if isinstance(release, dict) else None
    if isinstance(needs, str):
        needs = [needs]
    elif not isinstance(needs, list):
        needs = []

    if "security" not in needs:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                "cd_readiness",
                f"ci.yml has a security job, but the release job does not depend "
                f"on it (needs: {needs or 'absent'}). The scan runs and reports "
                f"while the release proceeds regardless of its result.",
                "Add `security` to the release job's needs: `needs: [test, security]`.",
            )
        )
    return findings


def check_canonical_ci_job_names(repo_path: Path) -> list[Finding]:
    """CD-026: ci.yml jobs are named security, test, release, deploy, evaluate."""
    CHECK_ID = "CD-026"
    findings: list[Finding] = []
    workflow = _ci_workflow(repo_path)
    if workflow is None or _declares_workflow_call(workflow):
        return findings
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return findings

    for name in jobs:
        if name not in _CANONICAL_CI_JOBS:
            findings.append(
                _finding(
                    CHECK_ID,
                    "WARN",
                    "structural_conformance",
                    f"ci.yml job `{name}` is outside the canonical set "
                    f"(security, test, release, deploy, evaluate).",
                    f"Rename `{name}` to `test` if it is the work job, and "
                    f"update any `needs:` that reference it.",
                )
            )
    return findings


#: The fleet's shared test stage. It runs pytest with ``--cov=src`` unless a
#: caller overrides ``pytest-args``, so delegating to it measures coverage
#: without ci.yml ever containing ``--cov`` itself.
_SHARED_TEST_WORKFLOW = "workflows/python-test.yml"


def _delegates_coverage(repo_path: Path) -> bool:
    """True when a ci.yml job calls the shared test workflow with coverage on.

    Coverage stays on unless the caller passes ``pytest-args`` without
    ``--cov`` — that is the one way to turn it off through the delegation,
    so it is the one thing checked.
    """
    workflow = _ci_workflow(repo_path)
    jobs = workflow.get("jobs") if workflow else None
    if not isinstance(jobs, dict):
        return False
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if _SHARED_TEST_WORKFLOW not in str(job.get("uses") or ""):
            continue
        args = (job.get("with") or {}).get("pytest-args")
        if args is None or "--cov" in str(args):
            return True
    return False


def _measures_coverage(repo_path: Path, ci_text: str) -> bool:
    """True when CI runs the tests with coverage, inline or by delegation."""
    if "--cov" in ci_text or "pytest-cov" in ci_text or _delegates_coverage(repo_path):
        return True
    if "--coverage" in ci_text or "test:coverage" in ci_text:
        return True
    # `pnpm test` in CI, with coverage switched on in the script it runs.
    package = repo_path / "package.json"
    if re.search(r"\b(pnpm|npm)\s+(run\s+)?test\b", ci_text) and package.is_file():
        with suppress(OSError, ValueError):
            scripts = (
                json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
            )
            return "--coverage" in str(scripts.get("test", ""))
    return False


def _declared_floor(repo_path: Path) -> bool | None:
    """Whether a coverage floor is declared; None when there is no project file.

    Python reads only ``[tool.coverage.report] fail_under`` in pyproject.toml,
    parsed as TOML so a commented-out line does not count, and pytest-cov
    enforces it with no CI change. TypeScript reads a vitest config for
    ``thresholds``.
    """
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return False
        floor = (
            data.get("tool", {}).get("coverage", {}).get("report", {}).get("fail_under")
        )
        return (
            isinstance(floor, (int, float))
            and not isinstance(floor, bool)
            and floor > 0
        )
    if (repo_path / "package.json").is_file():
        for config in sorted(repo_path.glob("vitest.config.*")):
            with suppress(OSError, UnicodeDecodeError):
                if "thresholds" in config.read_text(encoding="utf-8"):
                    return True
        return False
    return None


def check_coverage_floor(repo_path: Path) -> list[Finding]:
    """TEST-019: coverage is measured in CI against a declared floor.

    Absorbs TEST-006, whose "coverage measured in CI" is condition (1) here,
    shared-workflow delegation included. Condition (2) is the floor, read
    from the one place each toolchain enforces it. Whether the floor follows
    coverage upward is the rule's convention half and is not checked:
    pytest-cov has no equivalent of Vitest's autoUpdate.

    A repo with neither pyproject.toml nor package.json has no project to
    measure and passes.
    """
    CHECK_ID = "TEST-019"
    findings: list[Finding] = []
    floor = _declared_floor(repo_path)
    if floor is None:
        return findings

    ci = repo_path / ".github" / "workflows" / "ci.yml"
    ci_text = ""
    with suppress(OSError, UnicodeDecodeError):
        ci_text = ci.read_text(encoding="utf-8")
    if not _measures_coverage(repo_path, ci_text):
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                "testing_coverage",
                "Coverage is not measured in CI: ci.yml runs no pytest --cov, "
                "vitest --coverage or test:coverage, and no job calls the shared "
                "python-test.yml with coverage on.",
                "Call mini-app-polis/.github's python-test.yml as the `test` job, "
                "or add --cov (pytest) or --coverage (vitest) to the test step.",
            )
        )
    if not floor:
        python = (repo_path / "pyproject.toml").is_file()
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                "testing_coverage",
                (
                    "No coverage floor: pyproject.toml declares no fail_under "
                    "in [tool.coverage.report], so coverage can fall without CI "
                    "noticing."
                    if python
                    else "No coverage floor: no vitest config declares "
                    "coverage.thresholds, so coverage can fall without CI "
                    "noticing."
                ),
                (
                    "Set [tool.coverage.report] fail_under to the repo's current "
                    "coverage, rounded down, and raise it when coverage rises."
                    if python
                    else "Declare coverage.thresholds with autoUpdate rounding "
                    "down: autoUpdate: (n) => Math.floor(n)."
                ),
            )
        )
    return findings


def _delegates_terraform(repo_path: Path) -> bool:
    """True when a ci.yml job hands a Terraform directory to the shared test workflow."""
    workflow = _ci_workflow(repo_path)
    jobs = workflow.get("jobs") if workflow else None
    if not isinstance(jobs, dict):
        return False
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if _SHARED_TEST_WORKFLOW not in str(job.get("uses") or ""):
            continue
        if str((job.get("with") or {}).get("terraform-dir") or "").strip():
            return True
    return False


def _workflow_texts(repo_path: Path) -> str:
    """Every workflow under .github/workflows, concatenated."""
    workflows = repo_path / ".github" / "workflows"
    if not workflows.is_dir():
        return ""
    return "\n".join(
        f.read_text(errors="replace")
        for f in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")])
    )


def check_terraform_checked_in_ci(
    repo_path: Path, repo_type: str = ""
) -> list[Finding]:
    """CD-027: a repo that declares infrastructure as code checks it in CI.

    Any workflow counts, not only ci.yml: mini-app-polis/infra checks its
    Terraform in terraform.yml, beside the plan it gates.
    """
    CHECK_ID = "CD-027"
    if not declares_terraform(repo_path, repo_type):
        return []
    text = _workflow_texts(repo_path)
    if not text:
        # CD-026 and check_ci report an absent workflow. A second finding
        # naming Terraform would send the reader to the wrong file.
        return []
    inline = "terraform fmt" in text and "terraform validate" in text
    if inline or _delegates_terraform(repo_path):
        return []
    where = "The repository" if repo_type == "infrastructure" else "infra/"
    return [
        _finding(
            CHECK_ID,
            "WARN",
            "cd_readiness",
            f"{where} declares Terraform, but no workflow runs "
            "`terraform fmt -check` and `terraform validate`, and none passes "
            "a terraform-dir to the shared python-test.yml.",
            "Run both on every push. Neither needs state or credentials.",
        )
    ]


def _required_provider_blocks(tf_text: str) -> list[str]:
    """The body of each ``required_providers { ... }``, found by brace count."""
    blocks: list[str] = []
    for m in re.finditer(r"required_providers\s*\{", tf_text):
        depth, i = 0, m.end() - 1
        while i < len(tf_text):
            if tf_text[i] == "{":
                depth += 1
            elif tf_text[i] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(tf_text[m.end() : i])
                    break
            i += 1
    return blocks


def _runs_terraform_in_ci(repo_path: Path) -> bool:
    """True when some workflow runs Terraform — inline, or by delegation (CD-027)."""
    text = _workflow_texts(repo_path)
    inline = "terraform fmt" in text or "terraform validate" in text
    return inline or _delegates_terraform(repo_path)


def check_terraform_versions_pinned(
    repo_path: Path, repo_type: str = ""
) -> list[Finding]:
    """CD-028: Terraform versions pinned, lock committed, lock covers CI's platform."""
    CHECK_ID = "CD-028"
    if not declares_terraform(repo_path, repo_type):
        return []
    infra = terraform_root(repo_path, repo_type)
    label = "the repository root" if repo_type == "infrastructure" else "infra/"

    findings: list[Finding] = []

    def fail(message: str, suggestion: str) -> None:
        findings.append(_finding(CHECK_ID, "WARN", "cd_readiness", message, suggestion))

    # The root's own files carry the pin; modules' required_providers are
    # read too, since a module that leaves a provider unpinned widens what
    # the root can resolve.
    tf_files = sorted(infra.glob("*.tf"))
    module_files = sorted((infra / "modules").rglob("*.tf"))
    tf_text = "\n".join(f.read_text(errors="replace") for f in tf_files)
    providers_text = "\n".join(
        f.read_text(errors="replace") for f in [*tf_files, *module_files]
    )
    if not re.search(r"(?m)^\s*required_version\s*=", tf_text):
        fail(
            f"No .tf file in {label} sets required_version in its terraform block.",
            'Pin the Terraform version (e.g. required_version = ">= 1.6") so '
            "the workstation and CI agree on what is running the stack.",
        )

    # Each `source = ...` inside required_providers needs a `version` in the
    # same provider block. A regex cannot find the end of the outer block —
    # the non-greedy form stops at the first inner `}` — so the extent is
    # found by counting braces, and the provider entries inside it (which
    # nest no further) are then read with one.
    for block in _required_provider_blocks(providers_text):
        for name, body in re.findall(r"(\w+)\s*=\s*\{([^{}]*)\}", block):
            if "source" in body and not re.search(r"(?m)^\s*version\s*=", body):
                fail(
                    f"Provider `{name}` declares a source with no version constraint.",
                    'Pin it (e.g. version = "~> 5.0"), so a second machine '
                    "resolves the provider this stack was written against "
                    "rather than whatever is newest that day.",
                )

    lock = infra / ".terraform.lock.hcl"
    if not lock.exists():
        fail(
            f".terraform.lock.hcl is not committed in {label}.",
            "Commit it. It holds provider versions and checksums and no "
            "values — it is the one generated Terraform file that belongs "
            "in the repository (SEC-008 covers the ones that do not).",
        )
        return findings

    if _runs_terraform_in_ci(repo_path):
        platforms = lock.read_text(errors="replace").count("h1:")
        if platforms < 2:
            fail(
                f"CI runs Terraform, but .terraform.lock.hcl carries "
                f"{platforms} platform hash(es) — it has only ever been "
                f"written on one operating system.",
                "Record the runner's platform too: `terraform providers lock "
                "-platform=darwin_arm64 -platform=linux_amd64`. Without it "
                "`init` on Linux rejects the provider it just downloaded, "
                "with an error that names checksums and reads like tampering.",
            )
    return findings


# VER-006's concern is that a bare `npx semantic-release` runs without the
# plugins the config names. `npx -p semantic-release@N -p @semantic-release/x`
# installs the named packages for that one call, which satisfies it without
# putting them in the lockfile. Matched on whitespace-collapsed content so a
# folded (`>-`) multi-line run block reads as one command.
_NPX_PACKAGE_SEMANTIC_RELEASE = re.compile(
    r"\bnpx\b(?:\s+(?:--yes|-y))?(?:\s+(?:-p|--package)[\s=]\S+)*"
    r"\s+(?:-p|--package)[\s=]semantic-release(?:@\S+)?\b"
)


def check_ci(
    repo_path: Path,
    exceptions: frozenset[str] | None = None,
) -> list[Finding]:
    """
    Runs all CI checks in one pass.
    Covers: VER-003, VER-005, VER-006.
    """
    CHECK_ID = "VER-003"
    findings: list[Finding] = []
    _exc = exceptions or frozenset()
    ci = repo_path / ".github" / "workflows" / "ci.yml"
    if not ci.exists():
        # Reported as VER-003, so it answers to VER-003's scope like the
        # checks below: a type the rule does not apply to — one with no
        # releases — is not told it lacks the workflow that cuts them.
        if "VER-003" in _exc:
            return findings
        findings.append(
            _finding(
                "VER-003",
                "ERROR",
                "cd_readiness",
                "ci.yml not found at .github/workflows/ci.yml.",
                "Add a CI workflow with test and release jobs.",
            )
        )
        return findings

    content = ci.read_text()

    if "VER-003" not in _exc and "semantic-release" not in content:
        findings.append(
            _finding(
                "VER-003",
                "ERROR",
                "cd_readiness",
                "semantic-release not found in ci.yml.",
                "Add a release job running npx semantic-release.",
            )
        )

    if "VER-005" not in _exc and "fetch-depth: 0" not in content:
        findings.append(
            _finding(
                "VER-005",
                "ERROR",
                "cd_readiness",
                "fetch-depth: 0 absent from ci.yml checkout step.",
                "Add fetch-depth: 0 to the actions/checkout step in the release job.",
            )
        )

    if "VER-006" not in _exc and (
        "npm install --no-save" not in content
        and "pnpm exec semantic-release" not in content
        and "pnpm run semantic-release" not in content
        and "pnpm add" not in content
        and not _NPX_PACKAGE_SEMANTIC_RELEASE.search(" ".join(content.split()))
    ):
        findings.append(
            _finding(
                "VER-006",
                "ERROR",
                "cd_readiness",
                "npm install --no-save step absent from release job.",
                "Add explicit npm install --no-save before npx semantic-release, "
                "or use pnpm exec semantic-release with plugins in devDependencies.",
            )
        )

    return findings


def check_no_print_statements(repo_path: Path) -> list[Finding]:
    """CD-003: No print() statements in production code paths."""
    CHECK_ID = "CD-003"

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                findings.append(
                    _finding(
                        "CD-003",
                        "WARN",
                        "cd_readiness",
                        f"print() statement found in {py_file.relative_to(repo_path)}.",
                        "Replace with structured logger from common-python-utils.",
                    )
                )
                break  # one finding per file is enough
    return findings


def check_no_hardcoded_urls(repo_path: Path) -> list[Finding]:
    """FE-007: No hardcoded API URLs in source."""
    CHECK_ID = "FE-007"
    import re

    findings: list[Finding] = []
    pattern = re.compile(r"https?://(localhost|.*railway\.app|.*up\.railway\.app)")
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        content = py_file.read_text()
        if pattern.search(content):
            findings.append(
                _finding(
                    "FE-007",
                    "ERROR",
                    "structural_conformance",
                    f"Hardcoded API URL found in {py_file.relative_to(repo_path)}.",
                    "Move URL to environment variable.",
                )
            )
    return findings


def _stdlib_logging_is_primary(text: str) -> bool:
    """True when this module reaches for stdlib logging as its own logger.

    Parsed rather than searched. The strings ``import logging`` and
    ``logging.getLogger`` appear in this very file as the patterns a
    text scan would look for, and in any module that documents the rule
    — matching those is matching prose, not code.

    A stdlib logger built inside an ``except`` handler is a fallback for
    a shared logger that was tried first (the Prefect run logger outside
    a run, say), not the module's primary logger, so it does not count.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    fallback: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            for inner in ast.walk(node):
                fallback.add(id(inner))

    for node in ast.walk(tree):
        if id(node) in fallback:
            continue
        if isinstance(node, ast.Import):
            if any(
                a.name == "logging" or a.name.startswith("logging.") for a in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "logging":
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"getLogger", "basicConfig"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "logging"
            ):
                return True
    return False


def check_structured_logging(repo_path: Path) -> list[Finding]:
    """CD-009: Structured logging via shared library."""
    CHECK_ID = "CD-009"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        text = py_file.read_text()
        if _stdlib_logging_is_primary(text):
            findings.append(
                _finding(
                    "CD-009",
                    "WARN",
                    "cd_readiness",
                    f"Hand-rolled logging detected in {py_file.relative_to(repo_path)}.",
                    "Use shared structured logger from the shared utility library.",
                )
            )
            break
    for ts_file in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
        text = ts_file.read_text()
        if "console.log(" in text:
            findings.append(
                _finding(
                    "CD-009",
                    "WARN",
                    "cd_readiness",
                    f"console.log used as primary logger in {ts_file.relative_to(repo_path)}.",
                    "Use shared structured logger helpers instead of console.log.",
                )
            )
            break
    return findings


#: The project every value is edited in. A repo reads it only through its
#: own project's references, so it is never what a repo pins.
_VAULT_PROJECT = "mini-app-polis-ecosystem"
_DOPPLER_CONFIG = "dev"


def _not_none(value: ast.AST) -> bool:
    """Whether an ``env_file`` value names a file: anything but a literal None."""
    return not (isinstance(value, ast.Constant) and value.value is None)


def _reads_dotenv(tree: ast.AST) -> bool:
    """Whether a module imports dotenv, calls load_dotenv, or points a
    pydantic-settings config at a ``.env`` file.

    Structural rather than textual, so a module that only quotes those
    names — this checker, a test fixture, a docstring — is not flagged.
    ``env_file`` is caught in every form pydantic-settings accepts it: a
    ``SettingsConfigDict``/``dict`` keyword or key, a ``model_config``
    dict literal, a v1 ``class Config`` attribute, and ``_env_file=`` at
    construction. Only an explicit None is allowed through.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "dotenv" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) names a module of the package's
            # own, whatever it is called.
            if node.level == 0 and (node.module or "").split(".")[0] == "dotenv":
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if name == "load_dotenv":
                return True
            for kw in node.keywords:
                if kw.arg == "_env_file" and _not_none(kw.value):
                    return True
                if (
                    kw.arg == "env_file"
                    and name in ("SettingsConfigDict", "dict")
                    and _not_none(kw.value)
                ):
                    return True
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "env_file"
                    and _not_none(value)
                ):
                    return True
        elif isinstance(node, ast.ClassDef) and node.name == "Config":
            for stmt in node.body:
                targets: list[ast.expr]
                assigned: ast.expr | None
                if isinstance(stmt, ast.Assign):
                    targets, assigned = stmt.targets, stmt.value
                elif isinstance(stmt, ast.AnnAssign):
                    targets, assigned = [stmt.target], stmt.value
                else:
                    continue
                if (
                    assigned is not None
                    and any(
                        isinstance(t, ast.Name) and t.id == "env_file" for t in targets
                    )
                    and _not_none(assigned)
                ):
                    return True
    return False


def _doppler_pin(doppler_yaml: Path) -> str | None:
    """What is wrong with ``doppler.yaml``, or None when it pins the repo to
    a project of its own and that project's dev config.

    The project's name is not checked against the repository's: a check
    sees the files, not where they came from. Pinning the vault is what
    goes wrong in practice, and that is flagged.
    """
    import yaml

    if not doppler_yaml.is_file():
        return "doppler.yaml is missing."
    try:
        loaded = yaml.safe_load(doppler_yaml.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return "doppler.yaml is not valid YAML."
    setup = loaded.get("setup") if isinstance(loaded, dict) else None
    # Doppler accepts one mapping or a list of them (one per path).
    candidates = setup if isinstance(setup, list) else [setup]
    entries: list[dict] = [e for e in candidates if isinstance(e, dict)]
    projects = [e.get("project") for e in entries]
    if not any(isinstance(p, str) and p.strip() for p in projects):
        return "doppler.yaml names no project."
    if _VAULT_PROJECT in projects:
        return (
            f"doppler.yaml pins {_VAULT_PROJECT}, where values are edited; "
            "pin the repo's own project, which references them."
        )
    if not any(e.get("config") == _DOPPLER_CONFIG for e in entries):
        return (
            f"doppler.yaml does not pin the {_DOPPLER_CONFIG} config; "
            "local runs never use prd."
        )
    return None


def _requirement_name(requirement: str) -> str:
    """A requirement's project name, normalized as PEP 503 does."""
    name = re.split(r"[\s\[<>=!~;@(]", requirement.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def _doppler_local_dev_findings(repo_path: Path) -> list[Finding]:
    """CD-011's local-development half, for Python repos with secrets.

    A repo that declares ``.env.example`` (it has secrets) and
    ``pyproject.toml`` (it is Python) must pin itself to its own Doppler
    project and that project's dev config in ``doppler.yaml``, so ``doppler
    setup`` asks nothing and ``doppler run`` injects the names the repo
    reads; and nothing in it may read a ``.env`` file, which is how secrets
    end up on disk with no sign of where a value came from.

    A file it cannot read is skipped rather than raised: one unreadable file
    must not cost the rest of CD-011, the committed-.env check included.
    """
    if not (repo_path / ".env.example").is_file():
        return []
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.is_file():
        return []

    findings: list[Finding] = []
    problem = _doppler_pin(repo_path / "doppler.yaml")
    if problem is not None:
        findings.append(
            _finding(
                "CD-011",
                "WARN",
                "cd_readiness",
                problem,
                "Add doppler.yaml pinning the repo to its own Doppler project's "
                f"{_DOPPLER_CONFIG} config (setup: - project: <repo> config: dev) "
                "so doppler run needs no setup questions.",
            )
        )

    with suppress(OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        deps = (
            tomllib.loads(pyproject.read_text())
            .get("project", {})
            .get("dependencies", [])
        )
        if any(
            isinstance(d, str) and _requirement_name(d) == "python-dotenv" for d in deps
        ):
            findings.append(
                _finding(
                    "CD-011",
                    "WARN",
                    "cd_readiness",
                    "python-dotenv is a runtime dependency.",
                    "Remove it: secrets come from Doppler (doppler run locally), "
                    "never from a .env file.",
                )
            )

    src = repo_path / "src"
    if src.is_dir():
        for path in sorted(src.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text())
            except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
                continue
            if _reads_dotenv(tree):
                findings.append(
                    _finding(
                        "CD-011",
                        "WARN",
                        "cd_readiness",
                        f"{path.relative_to(repo_path)} reads a .env file "
                        "(dotenv, or a pydantic-settings env_file).",
                        "Read the process environment only; run under "
                        "doppler run locally.",
                    )
                )
                break
    return findings


def check_no_hardcoded_secrets(repo_path: Path) -> list[Finding]:
    """CD-011: Doppler as canonical secret store."""
    CHECK_ID = "CD-011"
    import re

    findings = _doppler_local_dev_findings(repo_path)

    tracked = _tracked_paths(repo_path)
    for env_file in sorted(repo_path.rglob(".env*")):
        name = env_file.name
        if name == ".env.example" or any(
            marker in name for marker in ("example", "sample", "template")
        ):
            continue
        rel = env_file.relative_to(repo_path)
        # The finding says "committed", so check that it is. In the
        # production path the repo arrives as a zipball of tracked files
        # and every match is committed by construction; run the same
        # check against a working tree and an ignored local .env — the
        # very file the rule wants developers to keep — reads as a
        # violation. Only skip when git can answer; a zipball has no
        # .git, and there the earlier behaviour is the correct one.
        if tracked is not None and rel.as_posix() not in tracked:
            continue
        findings.append(
            _finding(
                "CD-011",
                "ERROR",
                "cd_readiness",
                f"Committed env file detected: {env_file.relative_to(repo_path)}.",
                "Remove committed env files and use Doppler-managed runtime secrets.",
            )
        )
        break

    secret_patterns = [
        re.compile(r"sk-[A-Za-z0-9]{16,}"),
        re.compile(r"Bearer\s+[A-Za-z0-9]{20,}"),
        re.compile(r"password\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE),
        re.compile(r"api[_-]?key\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE),
    ]
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    for path in (
        list(src.rglob("*.py")) + list(src.rglob("*.ts")) + list(src.rglob("*.tsx"))
    ):
        text = path.read_text()
        lowered = text.lower()
        if "os.getenv(" in lowered or "process.env" in lowered:
            pass
        for pat in secret_patterns:
            if pat.search(text):
                findings.append(
                    _finding(
                        "CD-011",
                        "ERROR",
                        "cd_readiness",
                        f"Potential hardcoded secret in {path.relative_to(repo_path)}.",
                        "Move secrets to Doppler/runtime env vars and remove literal values.",
                    )
                )
                break
        if any(
            f["rule_id"] == "CD-011" and "hardcoded secret" in f["finding"]
            for f in findings
        ):
            break
    return findings


def check_gha_not_trigger_relay(repo_path: Path) -> list[Finding]:
    """CD-006: GitHub Actions must not relay repository triggers into app code.

    Scans ``.github/workflows`` for workflows *triggered by*
    ``repository_dispatch`` that start pipeline work (a Prefect deployment,
    a POST to ``/v1/trigger`` or ``/v1/<cog>/runs``, an SQS send, a Lambda
    invoke), scheduled jobs calling Prefect Cloud, and internal trigger HTTP
    paths. A workflow that *sends* a dispatch — to run another repository's
    tests after a deploy — is CI chaining, not a relay, and is not flagged;
    nor is a dispatch-triggered workflow that only runs tests or builds,
    whatever its toolchain. Handles malformed YAML and the YAML 1.1
    ``on:`` → ``true`` quirk.
    """
    CHECK_ID = "CD-006"
    findings: list[Finding] = []
    import yaml as _yaml

    wf_dir = repo_path / ".github" / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.rglob("*.yml")) + sorted(wf_dir.rglob("*.yaml")):
            try:
                text = wf.read_text()
            except OSError:
                continue
            low = text.lower()
            rel = str(wf.relative_to(repo_path))
            parsed = None
            with suppress(Exception):
                parsed = _yaml.safe_load(text)

            # Triggered BY repository_dispatch — the retired relay shape. A
            # workflow that only sends one (``gh api .../dispatches``) is not.
            if isinstance(parsed, dict):
                on = parsed.get("on", parsed.get(True))
                dispatch_triggered = (
                    "repository_dispatch" in on
                    if isinstance(on, (dict, list))
                    else on == "repository_dispatch"
                )
            else:
                dispatch_triggered = bool(
                    re.search(r"^\s*repository_dispatch\s*:", text, re.M)
                )

            if dispatch_triggered:
                relay = any(
                    k in low
                    for k in (
                        "prefect deployment run",
                        "prefect deploy",
                        "run_deployment(",
                        "npx prefect",
                        "sqs send-message",
                        "lambda invoke",
                    )
                ) or bool(re.search(r"/v1/(?:trigger|runs|[a-z0-9-]+/runs)\b", low))
                if relay:
                    findings.append(
                        _finding(
                            "CD-006",
                            "WARN",
                            "structural_conformance",
                            f"repository_dispatch workflow starts pipeline work ({rel}).",
                            "Pipeline work starts through watcher-cog, which asks "
                            "api-kaianolevine-com to enqueue it (PIPE-019); a workflow "
                            "runs CI/CD only.",
                        )
                    )

            if ("schedule" in low or "cron:" in low) and "api.prefect.cloud" in low:
                findings.append(
                    _finding(
                        "CD-006",
                        "WARN",
                        "structural_conformance",
                        f"Scheduled workflow references Prefect Cloud API ({rel}).",
                        "Avoid cron-driven Prefect Cloud calls from GitHub Actions; use Prefect-native scheduling.",
                    )
                )

            if re.search(r"['\"]/v1/(trigger|runs)", text):
                findings.append(
                    _finding(
                        "CD-006",
                        "WARN",
                        "structural_conformance",
                        f"Workflow references internal trigger HTTP path ({rel}).",
                        "Do not POST to internal trigger endpoints from GitHub Actions.",
                    )
                )

    src = repo_path / "src"
    if src.is_dir():
        for py in src.rglob("*.py"):
            if "tests/" in str(py).replace("\\", "/"):
                continue
            try:
                t = py.read_text()
            except OSError:
                continue
            if re.search(
                r"(httpx|requests)\.(post|put)\([^\)]*api\.github\.com/[^\"'\)]+/dispatches",
                t,
                re.I,
            ):
                findings.append(
                    _finding(
                        "CD-006",
                        "WARN",
                        "structural_conformance",
                        f"Python source posts to GitHub dispatches API ({py.relative_to(repo_path)}).",
                        "Start pipeline work through watcher-cog and api-kaianolevine-com "
                        "(PIPE-019), not repository_dispatch relays.",
                    )
                )
    return findings


def check_migration_in_ci(
    repo_path: Path,
    language: str = "python",
) -> list[Finding]:
    """API-011: CI runs database migrations on deploy.

    Python (Alembic): ci.yml contains 'alembic upgrade head' in a deploy job.
    TypeScript (Drizzle): ci.yml contains 'drizzle-kit push' or 'drizzle-kit migrate'.
    """
    findings: list[Finding] = []

    ci_texts = []
    ci = repo_path / ".github" / "workflows" / "ci.yml"
    if ci.exists():
        with suppress(Exception):
            ci_texts.append(ci.read_text())

    if not ci_texts:
        findings.append(
            _finding(
                "API-011",
                "ERROR",
                "structural_conformance",
                "api-service has no .github/workflows/ci.yml — migration steps cannot be verified.",
                "Add a ci.yml with deploy job including migration step.",
            )
        )
        return findings

    combined = "\n".join(ci_texts)

    if language == "python":
        if "alembic upgrade head" not in combined and "alembic upgrade" not in combined:
            findings.append(
                _finding(
                    "API-011",
                    "ERROR",
                    "structural_conformance",
                    "ci.yml has no 'alembic upgrade head' step.",
                    "Add 'alembic upgrade head' to the deploy job so migrations run "
                    "automatically on release.",
                )
            )
    else:
        if "drizzle-kit push" not in combined and "drizzle-kit migrate" not in combined:
            findings.append(
                _finding(
                    "API-011",
                    "ERROR",
                    "structural_conformance",
                    "ci.yml has no 'drizzle-kit push' or 'drizzle-kit migrate' step.",
                    "Add a Drizzle migration step to the deploy job.",
                )
            )
    return findings


def _work_queue_names(resources: list) -> set[str]:
    """Names of the queues whose redrive policy dead-letters somewhere."""
    return {
        queue.name
        for queue in of_type(resources, "aws_sqs_queue")
        if re.search(
            r"deadLetterTargetArn\s*[=:]\s*aws_sqs_queue\.[\w-]+\.arn", queue.body
        )
    }


def _alarm_notifies(
    resources: list, queues: set[str], metric: str | None = None
) -> bool:
    """True when an alarm with an action watches one of `queues`.

    With `metric`, the alarm must also be on that metric. The dead-letter
    alarm is matched on its queue alone, as it always has been: any metric
    on a dead-letter queue is about jobs that failed every retry.
    """
    for alarm in of_type(resources, "aws_cloudwatch_metric_alarm"):
        actions = (alarm.attr("alarm_actions") or "").replace(" ", "")
        if actions in ("", "[]"):
            continue
        if metric is not None and metric not in (alarm.attr("metric_name") or ""):
            continue
        if any(f"aws_sqs_queue.{name}." in alarm.body for name in queues):
            return True
    return False


def check_cd_010_infrastructure(repo_path: Path) -> list[Finding]:
    """CD-010's liveness layer for the pipeline cogs, where it is declared.

    A queue-driven worker has no process to be alive between jobs, so a
    liveness ping has nothing to report. Two events have to reach a
    person instead, and each alarm is blind to the other's:

    - a job failed every retry — the dead-letter alarm;
    - nothing is consuming (mapping disabled, throttled to zero, a broken
      deploy) — the work queue's ApproximateAgeOfOldestMessage alarm.
      Nothing is received, so nothing fails and the dead-letter queue
      stays empty: this is the outage the first alarm cannot see.

    Both are declared once, in mini-app-polis/infra's cog module
    (ecosystem-standards ADR-0010), so they are checked there and not in each cog. The
    stalled alarm's threshold is not checked — only that it exists and
    notifies someone.
    """
    resources = terraform_resources(repo_path, "infrastructure") or []
    if not of_type(resources, "aws_lambda_event_source_mapping"):
        return []
    findings: list[Finding] = []
    if not _alarm_notifies(resources, dead_letter_queue_names(resources)):
        findings.append(
            _finding(
                "CD-010",
                "ERROR",
                "cd_readiness",
                "Layer 1 missing: no aws_cloudwatch_metric_alarm on a dead-letter "
                "queue has alarm_actions.",
                "Alarm on the DLQ's ApproximateNumberOfMessagesVisible and point "
                "alarm_actions at an SNS topic someone is subscribed to.",
            )
        )
    if not _alarm_notifies(
        resources, _work_queue_names(resources), "ApproximateAgeOfOldestMessage"
    ):
        findings.append(
            _finding(
                "CD-010",
                "ERROR",
                "cd_readiness",
                "Layer 1 missing: no aws_cloudwatch_metric_alarm on a work "
                "queue's ApproximateAgeOfOldestMessage has alarm_actions, so a "
                "consumer that stops is never reported — nothing fails, and "
                "nothing reaches the dead-letter queue.",
                "Alarm on the work queue's ApproximateAgeOfOldestMessage above "
                "max_receive_count x visibility_timeout_seconds, and point "
                "alarm_actions at the same SNS topic as the dead-letter alarm.",
            )
        )
    return findings


def check_three_layer_observability(
    repo_path: Path,
    cog_subtype: str | None = None,
    language: str = "python",
) -> list[Finding]:
    """CD-010: Three-layer observability — Healthchecks + logger + Sentry.

    Language-aware: Python services must start Sentry (the shared
    ``init_sentry``, or ``sentry_sdk`` directly) + common-python-utils;
    TypeScript services must use @sentry/node (or @sentry/react/@sentry/astro)
    + common-typescript-utils. The stack is equivalent; only the package
    names differ.
    """
    findings: list[Finding] = []
    src = repo_path / "src"
    env_example = repo_path / ".env.example"

    env_text = env_example.read_text() if env_example.exists() else ""
    src_text = ""
    if src.is_dir():
        exts = ("*.py",) if language == "python" else ("*.ts", "*.tsx", "*.astro")
        for ext in exts:
            for f in src.rglob(ext):
                try:
                    src_text += "\n" + f.read_text(errors="replace")
                except Exception:
                    continue

    package_json_text = ""
    package_json = repo_path / "package.json"
    if package_json.exists():
        with suppress(Exception):
            package_json_text = package_json.read_text()

    # Layer 1 for a queue-driven pipeline cog is an alarm on its dead-letter
    # queue, declared in mini-app-polis/infra and checked there
    # (check_cd_010_infrastructure, ecosystem-standards ADR-0010). The cog carries layers 2 and 3.

    # Layer 1: Healthchecks — the always-on trigger worker.
    if cog_subtype == "trigger":
        env_has_healthchecks = (
            "HEALTHCHECKS_URL" in env_text or "HEALTHCHECKS_URL_" in env_text
        )
        src_has_ping_signal = "healthchecks.io" in src_text.lower() or (
            "HEALTHCHECKS_URL" in src_text
        )
        if not (env_has_healthchecks and src_has_ping_signal):
            findings.append(
                _finding(
                    "CD-010",
                    "ERROR",
                    "cd_readiness",
                    "Layer 1 missing: no HEALTHCHECKS_URL env var or healthchecks.io ping in source.",
                    "Add HEALTHCHECKS_URL (or a per-service HEALTHCHECKS_URL_<NAME>) "
                    "to .env.example and reference it from the main loop to ping "
                    "healthchecks.io.",
                )
            )

    # Layer 2: structured logging via shared library.
    if language == "python":
        layer2_present = (
            "common_python_utils" in src_text or "mini_app_polis" in src_text
        )
        layer2_hint = (
            "Import the shared logger from common_python_utils "
            "(import package name: mini_app_polis) and use it throughout."
        )
    else:
        layer2_present = "common-typescript-utils" in src_text or (
            "common-typescript-utils" in package_json_text
        )
        layer2_hint = (
            "Import the shared logger from common-typescript-utils "
            "(createLogger) and use it throughout."
        )
    if not layer2_present:
        findings.append(
            _finding(
                "CD-010",
                "ERROR",
                "cd_readiness",
                "Layer 2 missing: no shared-library logger usage.",
                layer2_hint,
            )
        )

    # Layer 3: Sentry. A Python service starts it with the shared
    # mini_app_polis.sentry.init_sentry (CD-038), which owns the sentry_sdk
    # call, so its own source need not name sentry_sdk at all; a direct
    # sentry_sdk.init still counts.
    if language == "python":
        starts_sentry = "sentry_sdk" in src_text or (
            "mini_app_polis.sentry" in src_text and "init_sentry(" in src_text
        )
        layer3_present = starts_sentry and (
            "SENTRY_DSN" in env_text or "SENTRY_DSN" in src_text
        )
        layer3_hint = (
            "Start Sentry at the entry point with mini_app_polis.sentry."
            "init_sentry and add SENTRY_DSN (or a service-specific variant, "
            "e.g. SENTRY_DSN_COGS) to .env.example."
        )
    else:
        layer3_present = (
            "@sentry/node" in package_json_text
            or "@sentry/react" in package_json_text
            or "@sentry/astro" in package_json_text
        ) and ("SENTRY_DSN" in env_text or "SENTRY_DSN" in src_text)
        layer3_hint = (
            "Install @sentry/node (api) or @sentry/react/@sentry/astro (web), "
            "initialise it at entry point, and add SENTRY_DSN to .env.example."
        )
    if not layer3_present:
        findings.append(
            _finding(
                "CD-010",
                "ERROR",
                "cd_readiness",
                "Layer 3 missing: Sentry integration not detected.",
                layer3_hint,
            )
        )
    return findings


def check_pnpm_lockfile(repo_path: Path) -> list[Finding]:
    """XSTACK-003: pnpm for all TypeScript projects."""
    CHECK_ID = "XSTACK-003"
    findings = []
    if (repo_path / "package-lock.json").exists():
        findings.append(
            _finding(
                "XSTACK-003",
                "ERROR",
                "structural_conformance",
                "package-lock.json found — npm is not the approved package manager for TypeScript projects.",
                "Migrate to pnpm: remove package-lock.json, run pnpm install, commit pnpm-lock.yaml.",
            )
        )
    if (repo_path / "yarn.lock").exists():
        findings.append(
            _finding(
                "XSTACK-003",
                "ERROR",
                "structural_conformance",
                "yarn.lock found — yarn is not the approved package manager for TypeScript projects.",
                "Migrate to pnpm: remove yarn.lock, run pnpm install, commit pnpm-lock.yaml.",
            )
        )
    if not (repo_path / "pnpm-lock.yaml").exists():
        findings.append(
            _finding(
                "XSTACK-003",
                "WARN",
                "structural_conformance",
                "pnpm-lock.yaml not found — pnpm may not be in use.",
                "Use pnpm as the package manager and commit pnpm-lock.yaml.",
            )
        )
    return findings


#: The fleet's shared conformance-evaluation trigger, as every repo calls
#: it — and as the repo that owns it calls its own copy, by local path.
_EVALUATE_WORKFLOWS = (
    "mini-app-polis/.github/.github/workflows/evaluate.yml@*",
    "./.github/workflows/evaluate.yml",
)


def check_cd_031(repo_path: Path) -> list[Finding]:
    """CD-031: every release requests its own conformance evaluation.

    (1) A push-triggered workflow has a job calling the shared evaluate.yml.
    (2) That job's ``needs:`` reaches, directly or transitively, the job
    that ships the change — one that runs semantic-release, or, for a
    Terraform root with no releases, one that runs ``terraform apply`` — so
    it evaluates what shipped, not the tree before it.
    """
    from evaluator_cog.engine.deterministic._workflows import load_workflows

    CHECK_ID = "CD-031"
    findings: list[Finding] = []
    evaluate_jobs = []
    for wf in load_workflows(repo_path):
        if "push" not in wf.triggers:
            continue
        by_id = {job.job_id: job for job in wf.jobs}
        for job in wf.jobs:
            if job.uses and any(
                fnmatch.fnmatch(job.uses.strip(), pattern)
                for pattern in _EVALUATE_WORKFLOWS
            ):
                evaluate_jobs.append((wf, by_id, job))

    if not evaluate_jobs:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                "cd_readiness",
                "No CI job calls the shared evaluate.yml, so a release of this "
                "repository never requests its own conformance evaluation — its "
                "findings only move when a standards or evaluator release sweeps "
                "the fleet.",
                "Add an evaluate job after the release job: `needs: release`, "
                "`if: github.ref == 'refs/heads/main' && github.event_name == "
                "'push'`, `uses: mini-app-polis/.github/.github/workflows/"
                "evaluate.yml@v3`, with `secrets: api-key: "
                "${{ secrets.CI_VALIDATOR_API_KEY }}`.",
            )
        )
        return findings

    def _releases(job_id: str, by_id: dict, seen: set[str]) -> bool:
        if job_id in seen:
            return False
        seen.add(job_id)
        job = by_id.get(job_id)
        if job is None:
            return False
        if any(
            step.run_invokes("semantic-release") or step.run_invokes("terraform apply")
            for step in job.steps
        ):
            return True
        return any(_releases(n, by_id, seen) for n in job.needs)

    for _wf, by_id, job in evaluate_jobs:
        if any(_releases(n, by_id, set()) for n in job.needs):
            return findings

    wf, _by_id, job = evaluate_jobs[0]
    findings.append(
        _finding(
            CHECK_ID,
            "WARN",
            "cd_readiness",
            f"{wf.rel}::{job.job_id} calls evaluate.yml but does not run after the "
            f"release — its needs ({', '.join(job.needs) or 'none'}) reach no job "
            f"that runs semantic-release or terraform apply, so it can evaluate "
            f"the tree before the change shipped.",
            "Make the evaluate job depend on the release job (`needs: release`), "
            "on the deploy job that itself needs the release, or — in a "
            "Terraform root — on the apply job.",
        )
    )
    return findings


#: CD-036's subject: the shared request-metrics middleware, and the two
#: qualified names that reach it. The package re-exports the class at its
#: top level, so either import path is the shared one.
_METRICS_MIDDLEWARE = "RequestMetricsMiddleware"
_SHARED_METRICS_MIDDLEWARE = frozenset(
    {
        "mini_app_polis.request_metrics.RequestMetricsMiddleware",
        "mini_app_polis.RequestMetricsMiddleware",
    }
)
_ASGI_APP_CONSTRUCTORS = frozenset({"FastAPI", "Starlette"})


def _callee_name(func: ast.expr) -> str | None:
    """The last segment of a call's target: `FastAPI` for `fastapi.FastAPI(...)`."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _import_bindings(tree: ast.AST) -> dict[str, str]:
    """Map each name an import binds in a module to what it refers to.

    Walks the whole tree, not just module level, because an app factory
    may import inside the function that builds the app.

    ``import a.b`` binds ``a`` to ``a``; ``import a.b as m`` binds ``m``
    to ``a.b``; ``from a.b import C as X`` binds ``X`` to ``a.b.C``.
    Relative imports are left out: they point into the repo, never at the
    shared library, so resolving them could only produce a non-match.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    head = alias.name.split(".", 1)[0]
                    bindings[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bindings


def _qualified(expr: ast.expr, bindings: dict[str, str]) -> str | None:
    """Resolve a Name or Attribute chain through the module's imports."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    head = bindings.get(expr.id, expr.id)
    return ".".join([head, *reversed(parts)])


def _registered_middleware(tree: ast.AST) -> list[ast.expr]:
    """First arguments of every middleware registration in a module.

    Two forms: ``app.add_middleware(X, ...)``, and a ``Middleware(X, ...)``
    entry in a FastAPI/Starlette constructor's ``middleware=`` list.
    ``app.middleware("http")(fn)`` is not collected — it registers a
    function, and the shared middleware is a class.
    """
    registered: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_middleware"
            and node.args
        ):
            registered.append(node.args[0])
        elif name in _ASGI_APP_CONSTRUCTORS:
            for kw in node.keywords:
                if kw.arg != "middleware" or not isinstance(
                    kw.value, (ast.List, ast.Tuple)
                ):
                    continue
                for entry in kw.value.elts:
                    if (
                        isinstance(entry, ast.Call)
                        and _callee_name(entry.func) == "Middleware"
                        and entry.args
                    ):
                        registered.append(entry.args[0])
    return registered


def _constructs_asgi_app(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call) and _callee_name(node.func) in _ASGI_APP_CONSTRUCTORS
        for node in ast.walk(tree)
    )


def check_request_metrics_middleware(repo_path: Path) -> list[Finding]:
    """CD-036: APIs register the shared request-metrics middleware.

    Layer 4 of the observability stack (CD-010) for APIs. The dashboard
    and alarms read metrics by name and dimension, so only the shared
    class counts — a local copy publishes numbers nothing reads.

    Everything is done on the AST, per module, because every shortcut
    gets api-kaianolevine-com wrong in one direction or the other. Its app
    is built inside ``_build_app()`` rather than at module level, so
    "find the module-level app" finds nothing; and a substring scan for
    the class name matches the import line and every comment about it,
    so it passes a repo that imports the middleware and never adds it.

    A registration counts only in a module that constructs an app, and
    only when its first argument resolves through that module's imports
    to the shared class — directly, through an ``as`` alias, or as an
    attribute of an imported module.

    Three outcomes fail, in this order. A class of the same name defined
    under ``src/`` is reported alone: the registration finding it would
    also cause has the same cause and the same fix. No app constructed
    under ``src/`` is reported rather than skipped, because that is what
    a non-Python API looks like, and the rule says it records a deferral —
    a silent skip would leave the deferral nothing to defer. Otherwise,
    no app module registers the shared class.

    ``tests/`` is never read: a test double named after the middleware is
    not the drift this rule is about.
    """
    CHECK_ID = "CD-036"
    dimension = "cd_readiness"
    findings: list[Finding] = []

    trees: dict[str, ast.AST] = {}
    src = repo_path / "src"
    if src.is_dir():
        for py in sorted(src.rglob("*.py")):
            try:
                trees[str(py.relative_to(repo_path))] = ast.parse(
                    py.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue

    local_copies = sorted(
        rel
        for rel, tree in trees.items()
        if any(
            isinstance(node, ast.ClassDef) and node.name == _METRICS_MIDDLEWARE
            for node in ast.walk(tree)
        )
    )
    if local_copies:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                dimension,
                f"A class named {_METRICS_MIDDLEWARE} is defined in "
                f"{', '.join(local_copies)}. A local copy publishes metrics "
                f"under whatever names it chooses, and the fleet dashboard "
                f"and alarms read only the shared middleware's.",
                f"Delete the local class and register {_METRICS_MIDDLEWARE} "
                f"from mini_app_polis.request_metrics instead.",
            )
        )
        return findings

    app_modules = {
        rel: tree for rel, tree in trees.items() if _constructs_asgi_app(tree)
    }
    if not app_modules:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                dimension,
                "No module under src/ constructs a FastAPI or Starlette "
                "application, so there is nowhere the shared request-metrics "
                "middleware is registered. This API reports no request "
                "latency or error rate.",
                f"Register {_METRICS_MIDDLEWARE} from "
                f"mini_app_polis.request_metrics on the application. An API "
                f"not yet in Python cannot, and records a CD-036 deferral in "
                f"evaluator.yaml until it is rewritten.",
            )
        )
        return findings

    elsewhere: set[str] = set()
    for rel, tree in app_modules.items():
        bindings = _import_bindings(tree)
        for arg in _registered_middleware(tree):
            qualified = _qualified(arg, bindings)
            if qualified in _SHARED_METRICS_MIDDLEWARE:
                return findings
            if qualified and qualified.rsplit(".", 1)[-1] == _METRICS_MIDDLEWARE:
                elsewhere.add(f"{rel} ({qualified})")

    if elsewhere:
        detail = (
            f"registers a {_METRICS_MIDDLEWARE} that is not the shared one: "
            f"{', '.join(sorted(elsewhere))}"
        )
    else:
        detail = f"does not register {_METRICS_MIDDLEWARE}"
    findings.append(
        _finding(
            CHECK_ID,
            "WARN",
            dimension,
            f"The application ({', '.join(sorted(app_modules))}) {detail}. "
            f"Without it this API reports no request latency or error rate, "
            f"and the fleet dashboard has no row for it.",
            f"Import {_METRICS_MIDDLEWARE} from mini_app_polis.request_metrics "
            f"and register it with app.add_middleware(...) where the "
            f"application is built.",
        )
    )
    return findings


# ── CD-038: an empty Sentry DSN in production says so ────────────────────────

_SENTRY_INIT = "sentry_sdk.init"
_ENV_READS = frozenset({"os.getenv", "os.environ.get"})
#: Logger methods at ERROR or above.
_ERROR_LOG_METHODS = frozenset({"error", "exception", "critical", "fatal"})
#: The shared library's environment resolution (mini_app_polis.environment).
_PRODUCTION_TESTS = frozenset({"is_production", "current_environment"})


@dataclass(frozen=True)
class _DsnSource:
    """Where a ``sentry_sdk.init`` call's DSN comes from.

    ``variable`` is the name someone has to set — the environment variable
    or the Settings field. ``local`` is the module variable the value was
    bound to on its way to the call, when there was one.
    """

    kind: str  # "environment variable" or "Settings field"
    variable: str
    #: The read itself, unparsed, so a guard that repeats it is recognised.
    expr_text: str
    local: str | None = None


def _parent_links(tree: ast.AST) -> dict[int, tuple[ast.AST, str, int]]:
    """Map each statement-list child to (parent, field, index)."""
    links: dict[int, tuple[ast.AST, str, int]] = {}
    for node in ast.walk(tree):
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                for i, child in enumerate(value):
                    if isinstance(child, ast.AST):
                        links[id(child)] = (node, field, i)
            elif isinstance(value, ast.AST):
                links[id(value)] = (node, field, -1)
    return links


def _module_str_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, for ``os.getenv(NAME)``."""
    constants: dict[str, str] = {}
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            constants[stmt.targets[0].id] = stmt.value.value
    return constants


def _unwrap_dsn(expr: ast.expr) -> ast.expr:
    """Strip the coercions a DSN read wears: ``.strip()``, ``or ""``, ``str()``."""
    while True:
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Attribute)
            and expr.func.attr in {"strip", "lstrip", "rstrip"}
            and not expr.args
        ):
            expr = expr.func.value
        elif isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
            expr = expr.values[0]
        elif (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "str"
            and len(expr.args) == 1
        ):
            expr = expr.args[0]
        else:
            return expr


def _direct_source(
    expr: ast.expr, bindings: dict[str, str], constants: dict[str, str]
) -> _DsnSource | None:
    """An env read or a Settings field, or None when it is neither."""
    expr = _unwrap_dsn(expr)
    if isinstance(expr, ast.Call) and _qualified(expr.func, bindings) in _ENV_READS:
        if not expr.args:
            return None
        arg = expr.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            name = arg.value
        elif isinstance(arg, ast.Name) and arg.id in constants:
            name = constants[arg.id]
        else:
            name = ast.unparse(arg)
        return _DsnSource("environment variable", name, ast.unparse(expr))
    if isinstance(expr, ast.Attribute) and (
        expr.attr.isupper() or "dsn" in expr.attr.lower()
    ):
        return _DsnSource("Settings field", expr.attr, ast.unparse(expr))
    return None


def _last_binding(scope: ast.AST, name: str, before_line: int) -> ast.expr | None:
    """The value last assigned to ``name`` in ``scope`` before ``before_line``.

    Nested functions and classes are not entered: their locals are not
    this scope's.
    """
    found: tuple[int, ast.expr] | None = None
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if (
            any(isinstance(t, ast.Name) and t.id == name for t in targets)
            and node.lineno < before_line
            and (found is None or node.lineno > found[0])
        ):
            found = (node.lineno, node.value)
    return found[1] if found else None


def _matches_subject(expr: ast.expr, source: _DsnSource) -> bool:
    """True when ``expr`` is the DSN itself, as a guard would read it.

    The local it was bound to, the same read repeated, or — for a Settings
    field — the same field on any receiver (``settings.X`` against
    ``get_settings().X``).
    """
    expr = _unwrap_dsn(expr)
    if source.local and isinstance(expr, ast.Name) and expr.id == source.local:
        return True
    if source.kind == "Settings field" and isinstance(expr, ast.Attribute):
        return expr.attr == source.variable
    return ast.unparse(expr) == source.expr_text


def _is_production_test(expr: ast.expr) -> bool:
    return any(
        isinstance(node, ast.Call) and _callee_name(node.func) in _PRODUCTION_TESTS
        for node in ast.walk(expr)
    )


def _polarity(test: ast.expr, source: _DsnSource) -> str | None:
    """Which way a guard faces: ``present``, ``empty``, or None.

    ``present`` when the test is true only with a DSN, ``empty`` when only
    without.

    None when the test does not read the DSN, or reads it in a shape this
    check does not reason about.
    """
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _polarity(test.operand, source)
        return {"present": "empty", "empty": "present"}.get(inner or "")
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if (
            isinstance(right, ast.Constant)
            and right.value in (None, "")
            and _matches_subject(left, source)
        ):
            if isinstance(op, (ast.Is, ast.Eq)):
                return "empty"
            if isinstance(op, (ast.IsNot, ast.NotEq)):
                return "present"
        return None
    if isinstance(test, ast.BoolOp):
        parts = [_polarity(v, source) for v in test.values]
        if isinstance(test.op, ast.And) and "present" in parts:
            return "present"
        if isinstance(test.op, ast.Or) and "empty" in parts:
            return "empty"
        # `if not dsn and is_production():` — the empty branch, gated on
        # production, which is what the rule asks for.
        if isinstance(test.op, ast.And) and "empty" in parts:
            others = [v for v, p in zip(test.values, parts, strict=True) if p is None]
            if all(_is_production_test(v) for v in others):
                return "empty"
        return None
    if _matches_subject(test, source):
        return "present"
    return None


def _reports(stmts: list[ast.stmt]) -> bool:
    """True when ``stmts`` raise or log at ERROR or above.

    Unconditionally, or under a production test from the shared library's
    environment resolution. A report behind any other condition does not
    count. The logger's receiver is not resolved — CD-009 is what checks
    that a module's logger is the shared one — but the stdlib root logger
    (``logging.error``) is not accepted.
    """
    for stmt in stmts:
        if isinstance(stmt, ast.Raise):
            return True
        if isinstance(stmt, ast.If):
            if _is_production_test(stmt.test) and _reports(stmt.body):
                return True
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _ERROR_LOG_METHODS
                and not (
                    isinstance(func.value, ast.Name) and func.value.id == "logging"
                )
            ):
                return True
        elif isinstance(stmt, (ast.With, ast.AsyncWith)) and _reports(stmt.body):
            return True
    return False


def _assigns(stmts: list[ast.stmt], name: str | None) -> bool:
    if not name:
        return False
    return any(
        isinstance(node, ast.Name)
        and node.id == name
        and isinstance(node.ctx, ast.Store)
        for stmt in stmts
        for node in ast.walk(stmt)
    )


def _empty_dsn_problem(
    call: ast.Call,
    scope: ast.AST,
    source: _DsnSource,
    links: dict[int, tuple[ast.AST, str, int]],
) -> str | None:
    """Why an empty DSN reaches this call silently, or None if it does not.

    Two kinds of guard are found: an ``if`` the call sits inside, and an
    ``if`` that runs before it in the same or an enclosing block. Each
    guard's empty branch must raise or log at ERROR. A guard whose empty
    branch rebinds the DSN is a fallback this check does not follow, so it
    is given the benefit of the doubt.
    """
    empty_branches: list[list[ast.stmt]] = []
    node: ast.AST = call
    while node is not scope and id(node) in links:
        parent, field, index = links[id(node)]
        if isinstance(parent, ast.If) and field in ("body", "orelse"):
            polarity = _polarity(parent.test, source)
            if polarity == "present" and field == "body":
                empty_branches.append(parent.orelse)
            elif polarity == "empty" and field == "orelse":
                empty_branches.append(parent.body)
        if index >= 0 and field in ("body", "orelse", "finalbody", "handlers"):
            for earlier in getattr(parent, field)[:index]:
                if (
                    isinstance(earlier, ast.If)
                    and _polarity(earlier.test, source) == "empty"
                ):
                    empty_branches.append(earlier.body)
        node = parent

    if not empty_branches:
        return "nothing checks it for emptiness"
    for branch in empty_branches:
        if _assigns(branch, source.local):
            return None
        if not _reports(branch):
            return (
                "the branch taken when it is empty neither logs at ERROR nor raises"
                if branch
                else "nothing runs when it is empty"
            )
    return None


def check_sentry_dsn_absence_reported(repo_path: Path) -> list[Finding]:
    """CD-038: a production service with no Sentry DSN says so.

    ``sentry_sdk.init`` with an empty DSN is a documented no-op, and
    ``if not dsn: return`` is the same no-op written out. Each
    ``sentry_sdk.init`` call under ``src/`` is traced, within its module,
    to the environment read or Settings field its ``dsn=`` comes from —
    directly or through one local binding — and fails when an empty value
    can reach a path that neither logs at ERROR nor raises. A report under
    ``is_production()`` / ``current_environment()`` counts: outside
    production, silence is normal.

    Conservative where the AST runs out: a DSN this cannot trace (a
    function parameter, a dict lookup) is not reported, and nor is a guard
    that rebinds the DSN. A module that only calls the shared
    ``mini_app_polis.sentry.init_sentry`` has no ``sentry_sdk.init`` and so
    no subject — the helper owns the branch. No ``sentry_sdk.init`` at all
    is CD-002's finding, not this one's.
    """
    CHECK_ID = "CD-038"
    dimension = "cd_readiness"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not (repo_path / "pyproject.toml").is_file() or not src.is_dir():
        return findings

    for py in sorted(src.rglob("*.py")):
        if _is_checker_self_source(py):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        bindings = _import_bindings(tree)
        inits = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _qualified(node.func, bindings) == _SENTRY_INIT
        ]
        if not inits:
            continue
        links = _parent_links(tree)
        constants = _module_str_constants(tree)
        rel = py.relative_to(repo_path).as_posix()

        for call in inits:
            dsn_expr = next((kw.value for kw in call.keywords if kw.arg == "dsn"), None)
            if dsn_expr is None and call.args:
                dsn_expr = call.args[0]
            if dsn_expr is None:
                # `**options`, or no DSN at all: nothing here to trace.
                continue
            source = _direct_source(dsn_expr, bindings, constants)

            scope: ast.AST = tree
            walker: ast.AST = call
            while id(walker) in links:
                walker = links[id(walker)][0]
                if isinstance(walker, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope = walker
                    break

            if source is None:
                bare = _unwrap_dsn(dsn_expr)
                if not isinstance(bare, ast.Name):
                    continue
                bound = _last_binding(scope, bare.id, call.lineno)
                if bound is None and scope is not tree:
                    bound = _last_binding(tree, bare.id, call.lineno)
                traced = _direct_source(bound, bindings, constants) if bound else None
                if traced is None:
                    continue
                source = _DsnSource(
                    traced.kind, traced.variable, traced.expr_text, local=bare.id
                )
            problem = _empty_dsn_problem(call, scope, source, links)
            if problem is None:
                continue
            via = f" (through `{source.local}`)" if source.local else ""
            findings.append(
                _finding(
                    CHECK_ID,
                    "WARN",
                    dimension,
                    f"{rel}:{call.lineno}: sentry_sdk.init takes its DSN from "
                    f"the {source.kind} {source.variable}{via}, and {problem}. "
                    f"With {source.variable} unset or empty in production, "
                    f"Sentry is off and nothing records it.",
                    "Replace the hand-rolled init with "
                    "mini_app_polis.sentry.init_sentry(...), which logs an "
                    "ERROR naming the variable when the DSN is empty in "
                    "production. Or, on the empty branch, log at ERROR "
                    f"through the shared logger naming {source.variable}, "
                    "under is_production() from mini_app_polis.environment.",
                )
            )
    return findings
