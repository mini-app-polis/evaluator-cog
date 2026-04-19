"""Deterministic rule checks for the conformance flow.

Each check function takes a repo_path (Path) and returns a list of finding
dicts with keys: rule_id, severity, dimension, finding, suggestion.

Checks are grouped by what they inspect:
  - file_checks     — presence/absence of required files
  - pyproject_checks — pyproject.toml content
  - ci_checks       — .github/workflows/ci.yml content
  - ast_checks      — Python source AST scanning
  - test_checks     — tests/ directory structure
"""

from __future__ import annotations

import re
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluator_cog.engine.evaluator_config import EvaluatorConfig

Finding = dict[str, Any]


@dataclass
class CheckResult:
    findings: list[Finding]
    checked_rule_ids: set[str]


def _finding(
    rule_id: str,
    severity: str,
    dimension: str,
    finding: str,
    suggestion: str = "",
) -> Finding:
    return {
        "rule_id": rule_id,
        "violation_id": rule_id or None,
        "severity": severity,
        "dimension": dimension,
        "finding": finding,
        "suggestion": suggestion,
    }


# Pairs where the first rule supersedes the second — if both fire,
# drop the superseded rule's finding.
_SUPERSEDED_BY: dict[str, str] = {
    "CD-002": "CD-010",
    "CD-009": "CD-010",
}


def _deduplicate_same_repo_findings(findings: list[Finding]) -> list[Finding]:
    """
    Remove findings for rules that are superseded by a higher-level rule
    that also fired in the same check run.

    Example: if CD-010 fires, drop any CD-002 and CD-009 findings — they
    are sub-components of CD-010 and generating all three is redundant.
    """
    fired_rule_ids = {str(f.get("rule_id", "")) for f in findings}
    return [
        f
        for f in findings
        if not (
            str(f.get("rule_id", "")) in _SUPERSEDED_BY
            and _SUPERSEDED_BY[str(f.get("rule_id", ""))] in fired_rule_ids
        )
    ]


# -- File presence checks -----------------------------------------------------


def check_readme(repo_path: Path, monorepo_root: Path | None = None) -> list[Finding]:
    """DOC-001: README.md is mandatory."""
    CHECK_ID = "DOC-001"
    findings = []
    exists = (repo_path / "README.md").exists()
    if not exists and monorepo_root:
        exists = (monorepo_root / "README.md").exists()
    if not exists:
        findings.append(
            _finding(
                "DOC-001",
                "ERROR",
                "documentation_coverage",
                "README.md is absent.",
                "Create README.md documenting purpose, inputs, outputs, and how to run locally.",
            )
        )
    return findings


def check_changelog(
    repo_path: Path, monorepo_root: Path | None = None
) -> list[Finding]:
    """DOC-003: CHANGELOG.md required."""
    CHECK_ID = "DOC-003"
    findings = []
    exists = (repo_path / "CHANGELOG.md").exists()
    if not exists and monorepo_root:
        exists = (monorepo_root / "CHANGELOG.md").exists()
    if not exists:
        findings.append(
            _finding(
                "DOC-003",
                "WARN",
                "documentation_coverage",
                "CHANGELOG.md is absent.",
                "Create CHANGELOG.md — managed by semantic-release.",
            )
        )
    return findings


def check_env_example(
    repo_path: Path, monorepo_root: Path | None = None
) -> list[Finding]:
    """DOC-004: .env.example is required."""
    CHECK_ID = "DOC-004"
    findings = []
    # Check root first, then common monorepo locations
    candidates = [
        repo_path / ".env.example",
        repo_path / "apps" / "api" / ".env.example",
        repo_path / "apps" / "app" / ".env.example",
        repo_path / "app" / ".env.example",
        repo_path / "backend" / ".env.example",
        repo_path / "server" / ".env.example",
    ]
    if monorepo_root:
        candidates.append(monorepo_root / ".env.example")
    if not any(p.exists() for p in candidates):
        findings.append(
            _finding(
                "DOC-004",
                "WARN",
                "documentation_coverage",
                ".env.example is absent.",
                "Create .env.example documenting all required environment variables.",
            )
        )
    return findings


def check_pre_commit(repo_path: Path) -> list[Finding]:
    """PY-008: pre-commit configured."""
    CHECK_ID = "PY-008"
    findings = []
    if not (repo_path / ".pre-commit-config.yaml").exists():
        findings.append(
            _finding(
                "PY-008",
                "WARN",
                "structural_conformance",
                ".pre-commit-config.yaml is absent.",
                "Add .pre-commit-config.yaml with ruff hooks.",
            )
        )
    return findings


def check_releaserc(
    repo_path: Path, monorepo_root: Path | None = None
) -> list[Finding]:
    """VER-003: semantic-release on all repos."""
    CHECK_ID = "VER-003"
    findings = []
    exists = (repo_path / ".releaserc.json").exists()
    if not exists and monorepo_root:
        exists = (monorepo_root / ".releaserc.json").exists()
    if not exists:
        findings.append(
            _finding(
                "VER-003",
                "ERROR",
                "cd_readiness",
                ".releaserc.json is absent.",
                "Add .releaserc.json and a release job to ci.yml.",
            )
        )
    return findings


def check_src_layout(repo_path: Path) -> list[Finding]:
    """PY-005: src layout required."""
    CHECK_ID = "PY-005"
    findings = []
    if not (repo_path / "src").is_dir():
        findings.append(
            _finding(
                "PY-005",
                "ERROR",
                "structural_conformance",
                "src/ directory is absent — flat layout detected.",
                "Move package files under src/<package_name>/.",
            )
        )
    return findings


def check_no_setup_py(repo_path: Path) -> list[Finding]:
    """PY-007: pyproject.toml as single source of truth."""
    CHECK_ID = "PY-007"
    findings = []
    for bad in ("setup.py", "requirements.txt"):
        if (repo_path / bad).exists():
            findings.append(
                _finding(
                    "PY-007",
                    "WARN",
                    "structural_conformance",
                    f"{bad} found — pyproject.toml should be the single source of truth.",
                    f"Remove {bad} and consolidate into pyproject.toml.",
                )
            )
    return findings


# -- pyproject.toml checks ----------------------------------------------------


def check_common_python_utils_dep(repo_path: Path) -> list[Finding]:
    """PY-006: common-python-utils declared as dependency."""
    CHECK_ID = "PY-006"
    findings = []
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        return findings
    if "common-python-utils" not in pyproject.read_text():
        findings.append(
            _finding(
                "PY-006",
                "ERROR",
                "structural_conformance",
                "common-python-utils not declared as a dependency.",
                "Add common-python-utils to [project].dependencies.",
            )
        )
    return findings


def check_pyproject(
    repo_path: Path,
    exceptions: frozenset[str] | None = None,
) -> list[Finding]:
    """
    Runs all pyproject.toml checks in one pass.
    Covers: PY-001, PY-002, PY-003, PY-009, PY-010, CD-002.
    """
    CHECK_ID = "PY-001"
    findings = []
    _exc = exceptions or frozenset()
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        findings.append(
            _finding(
                "PY-007",
                "ERROR",
                "structural_conformance",
                "pyproject.toml is absent.",
                "Add pyproject.toml as the single source of truth.",
            )
        )
        return findings

    content = pyproject.read_text()

    if "PY-001" not in _exc and (
        "uv.lock" not in [p.name for p in repo_path.iterdir()]
        and "[tool.uv]" not in content
    ):
        findings.append(
            _finding(
                "PY-001",
                "WARN",
                "structural_conformance",
                "No uv.lock or [tool.uv] found — uv may not be in use.",
                "Use uv for dependency management.",
            )
        )

    if "PY-002" not in _exc and "[tool.ruff]" not in content:
        findings.append(
            _finding(
                "PY-002",
                "WARN",
                "structural_conformance",
                "[tool.ruff] section absent from pyproject.toml.",
                "Add ruff configuration to pyproject.toml.",
            )
        )

    if (
        "PY-003" not in _exc
        and 'requires-python = ">=3.11"' not in content
        and ">=3.12" not in content
    ):
        findings.append(
            _finding(
                "PY-003",
                "WARN",
                "structural_conformance",
                "Python minimum version may be below 3.11.",
                'Set requires-python = ">=3.11" in pyproject.toml.',
            )
        )

    if "PY-009" not in _exc and "hatchling" not in content:
        findings.append(
            _finding(
                "PY-009",
                "INFO",
                "structural_conformance",
                "hatchling not found as build backend.",
                'Set build-backend = "hatchling.build" in [build-system].',
            )
        )

    if "PY-010" not in _exc and "line-length = 88" not in content:
        findings.append(
            _finding(
                "PY-010",
                "INFO",
                "structural_conformance",
                "ruff line-length is not explicitly set to 88.",
                "Add line-length = 88 under [tool.ruff].",
            )
        )

    if "CD-002" not in _exc and "sentry-sdk" not in content:
        findings.append(
            _finding(
                "CD-002",
                "WARN",
                "cd_readiness",
                "sentry-sdk not found in pyproject.toml.",
                "Add sentry-sdk to dependencies and initialise at service entry point.",
            )
        )

    return findings


# -- CI checks ----------------------------------------------------------------


def check_pytest_coverage_in_ci(repo_path: Path) -> list[Finding]:
    """TEST-006: pytest coverage measured in CI."""
    CHECK_ID = "TEST-006"
    findings = []
    ci = repo_path / ".github" / "workflows" / "ci.yml"
    if not ci.exists():
        return findings
    content = ci.read_text()
    if "pytest --cov" not in content and "pytest-cov" not in content:
        findings.append(
            _finding(
                "TEST-006",
                "WARN",
                "testing_coverage",
                "Coverage not measured in CI — pytest --cov not found in ci.yml.",
                "Add --cov flag to pytest invocation in CI.",
            )
        )
    return findings


def check_ci(
    repo_path: Path,
    exceptions: frozenset[str] | None = None,
    monorepo_root: Path | None = None,
) -> list[Finding]:
    """
    Runs all CI checks in one pass.
    Covers: VER-003, VER-005, VER-006.
    """
    CHECK_ID = "VER-003"
    findings = []
    _exc = exceptions or frozenset()
    ci_root = monorepo_root or repo_path
    ci = ci_root / ".github" / "workflows" / "ci.yml"
    if not ci.exists():
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


# -- AST checks ---------------------------------------------------------------


def check_no_print_statements(repo_path: Path) -> list[Finding]:
    """CD-003: No print() statements in production code paths."""
    CHECK_ID = "CD-003"
    import ast

    findings = []
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

    findings = []
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


# -- Additional deterministic checks ------------------------------------------


def check_naming_conventions(repo_path: Path) -> list[Finding]:
    """PY-011: Naming conventions — Python."""
    CHECK_ID = "PY-011"
    import re

    findings = []
    pyproject = repo_path / "pyproject.toml"
    src = repo_path / "src"
    repo_expected = repo_path.name.replace("-", "_")

    project_name = ""
    if pyproject.exists():
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
        if m:
            project_name = m.group(1).replace("-", "_")

    src_pkg = ""
    if src.is_dir():
        for child in src.iterdir():
            if child.is_dir() and child.name != "__pycache__":
                src_pkg = child.name
                break

    if project_name and project_name != repo_expected:
        findings.append(
            _finding(
                "PY-011",
                "WARN",
                "structural_conformance",
                f"Project name '{project_name}' does not match repo naming '{repo_expected}'.",
                "Align [project].name with repository name (hyphens -> underscores).",
            )
        )

    if src_pkg and project_name and src_pkg != project_name:
        findings.append(
            _finding(
                "PY-011",
                "WARN",
                "structural_conformance",
                f"src package '{src_pkg}' does not match project name '{project_name}'.",
                "Rename the src package folder to match the project package identity.",
            )
        )

    snake_re = re.compile(r"^[a-z0-9_]+$")
    if src.is_dir():
        for py_file in src.rglob("*.py"):
            stem = py_file.stem
            if not snake_re.match(stem):
                findings.append(
                    _finding(
                        "PY-011",
                        "WARN",
                        "structural_conformance",
                        f"Non-snake_case Python module filename: {py_file.relative_to(repo_path)}.",
                        "Rename Python modules to snake_case.",
                    )
                )
    return findings


def check_failed_prefix(repo_path: Path) -> list[Finding]:
    """PY-012: FAILED_ prefix for failed inputs."""
    CHECK_ID = "PY-012"
    findings = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    content = "\n".join(f.read_text() for f in src.rglob("*.py"))
    has_file_processing = (
        ("shutil" in content or "pathlib" in content)
        and "except" in content
        and ("move(" in content or "rename(" in content)
    )
    if has_file_processing and "FAILED_" not in content:
        findings.append(
            _finding(
                "PY-012",
                "WARN",
                "structural_conformance",
                "File-processing exception paths do not use FAILED_ prefixing.",
                "Rename failed input files with FAILED_ to make failures visible and auditable.",
            )
        )
    return findings


def check_duplicate_prefix(repo_path: Path) -> list[Finding]:
    """PY-013: possible_duplicate_ prefix for duplicates."""
    CHECK_ID = "PY-013"
    findings = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    content = "\n".join(f.read_text().lower() for f in src.rglob("*.py"))
    dedup_signals = ("dedup", "duplicate", "already exists")
    if (
        any(s in content for s in dedup_signals)
        and "possible_duplicate_" not in content
    ):
        findings.append(
            _finding(
                "PY-013",
                "WARN",
                "structural_conformance",
                "Deduplication logic present but possible_duplicate_ prefixing is missing.",
                "Prefix duplicate files with possible_duplicate_ to preserve recoverability.",
            )
        )
    return findings


def check_finally_cleanup(repo_path: Path) -> list[Finding]:
    """PY-014: finally for temp file cleanup."""
    CHECK_ID = "PY-014"
    import ast

    findings = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    temp_calls = {"NamedTemporaryFile", "mkstemp", "mkdtemp", "TemporaryDirectory"}

    for py_file in src.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text())
        except Exception:
            continue

        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def _has_cleanup_context(
            node: ast.AST,
            parent_map: dict[ast.AST, ast.AST],
        ) -> bool:
            cur = node
            while cur in parent_map:
                cur = parent_map[cur]
                if isinstance(cur, ast.With):
                    return True
                if isinstance(cur, ast.Try) and cur.finalbody:
                    return True
            return False

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in temp_calls and not _has_cleanup_context(node, parents):
                findings.append(
                    _finding(
                        "PY-014",
                        "WARN",
                        "structural_conformance",
                        f"Temporary resource created without with/finally cleanup: {py_file.relative_to(repo_path)}.",
                        "Wrap temp resource usage in a context manager or try/finally cleanup.",
                    )
                )
                break
    return findings


def check_readme_io(
    repo_path: Path, monorepo_root: Path | None = None
) -> list[Finding]:
    """DOC-002: README describes inputs and outputs."""
    CHECK_ID = "DOC-002"
    findings = []
    readme = repo_path / "README.md"
    if not readme.exists() and monorepo_root:
        readme = monorepo_root / "README.md"
    if not readme.exists():
        return findings
    text = readme.read_text().lower()
    signals = [
        "input",
        "output",
        "source folder",
        "drive",
        "endpoint",
        "produces",
        "writes to",
        "openapi",
        "/v1/",
    ]
    found = sum(1 for s in signals if s in text)
    if found < 2:
        findings.append(
            _finding(
                "DOC-002",
                "WARN",
                "documentation_coverage",
                "README does not clearly describe data inputs and outputs.",
                "Document what goes in and what is produced/written by the service.",
            )
        )
    return findings


def check_no_dead_code(repo_path: Path) -> list[Finding]:
    """DOC-008: No dead code."""
    CHECK_ID = "DOC-008"
    import re

    findings = []
    # Match commented-out code by requiring a code CONSTRUCT at the start of the
    # payload, not merely a code token anywhere in prose. This avoids false
    # positives from explanatory comments that reference function names or use
    # prepositions like "if" or "for" mid-sentence.
    #
    # True positives caught: assignments (x = y), def/class declarations,
    # if/for statements with a colon, return/import statements, and standalone
    # function calls (must end the line — trailing prose indicates it's a
    # reference, not a call being commented out).
    code_like = re.compile(
        r"^("
        r"\w+\s*="  # assignment: x = ...
        r"|def\s+\w+"  # function definition
        r"|class\s+\w+"  # class definition
        r"|if\s+\w+.*:"  # if statement with colon
        r"|for\s+\w+\s+in\b"  # for loop
        r"|return\s+\w+"  # return statement
        r"|import\s+\w+"  # import statement
        r"|from\s+\w+"  # from import
        r"|\w+\(.*\)\s*$"  # standalone function call ending the line
        r")"
    )
    paths = []
    for pattern in ("*.py", "*.ts", "*.tsx", "*.astro"):
        paths.extend(
            (repo_path / "src").rglob(pattern) if (repo_path / "src").is_dir() else []
        )
    for path in paths:
        try:
            lines = path.read_text().splitlines()
        except Exception:
            continue
        run = 0
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                payload = stripped.lstrip("#/ ").strip()
                if code_like.match(payload):
                    run += 1
                else:
                    run = 0
            else:
                run = 0
            if run >= 3:
                findings.append(
                    _finding(
                        "DOC-008",
                        "WARN",
                        "documentation_coverage",
                        f"Potential dead/commented-out code in {path.relative_to(repo_path)} near line {idx}.",
                        "Remove dead code or convert it into active implementation/tests.",
                    )
                )
                break
    return findings


def check_split_package_identity(repo_path: Path) -> list[Finding]:
    """DOC-009: Split package identity documented at entry point."""
    CHECK_ID = "DOC-009"
    import re

    findings = []
    pyproject = repo_path / "pyproject.toml"
    src = repo_path / "src"
    readme = repo_path / "README.md"
    if not pyproject.exists() or not src.is_dir():
        return findings

    m = re.search(r'^\s*name\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
    if not m:
        return findings
    project_name = m.group(1)
    pkg_dirs = [d for d in src.iterdir() if d.is_dir() and d.name != "__pycache__"]
    if not pkg_dirs:
        return findings
    pkg_name = pkg_dirs[0].name
    if project_name.replace("-", "_") == pkg_name:
        return findings

    init_file = pkg_dirs[0] / "__init__.py"
    init_text = init_file.read_text().lower() if init_file.exists() else ""
    readme_text = readme.read_text().lower() if readme.exists() else ""

    if (
        project_name.lower() not in init_text
        or pkg_name.lower() not in init_text
        or project_name.lower() not in readme_text
        or pkg_name.lower() not in readme_text
    ):
        findings.append(
            _finding(
                "DOC-009",
                "WARN",
                "documentation_coverage",
                "Split package identity is not documented across __init__.py and README.",
                "Document both distribution name and import package name at the service entry points.",
            )
        )
    return findings


def check_readme_running_locally(
    repo_path: Path,
    dod_type: str | None = None,
) -> list[Finding]:
    """DOC-013: README Running locally section is complete."""
    CHECK_ID = "DOC-013"
    findings = []
    readme = repo_path / "README.md"
    if not readme.exists():
        return findings
    text = readme.read_text().lower()

    missing: list[str] = []
    if dod_type in ("new_cog", "new_fastapi_service"):
        required = ["uv sync", "pre-commit install", "pre-commit run", "uv run pytest"]
        missing.extend([r for r in required if r not in text])
        if "prereq" not in text and "python" not in text and "uv" not in text:
            missing.append("python/uv prerequisites")
    elif dod_type == "new_hono_service":
        required = ["pnpm install", "pnpm dev", "pnpm test", "node"]
        missing.extend([r for r in required if r not in text])
    elif dod_type in ("new_frontend_site", "new_react_app"):
        if "pnpm install" not in text and "npm install" not in text:
            missing.append("pnpm install or npm install")
        if "pnpm build" not in text and "npm run build" not in text:
            missing.append("pnpm build or npm run build")
        if (
            "pnpm dev" not in text
            and "npm run dev" not in text
            and "astro dev" not in text
        ):
            missing.append("pnpm dev or npm run dev or astro dev")
        if ".env.example" not in text:
            missing.append(".env.example")

    for item in missing:
        findings.append(
            _finding(
                "DOC-013",
                "WARN",
                "documentation_coverage",
                f"README Running locally is missing: {item}.",
                "Add the missing command/prerequisite to the Running locally section.",
            )
        )
    return findings


def check_healthchecks_integration(
    repo_path: Path,
    cog_subtype: str | None = None,
) -> list[Finding]:
    """CD-007: Healthchecks.io for trigger cogs."""
    CHECK_ID = "CD-007"
    findings = []
    if cog_subtype != "trigger":
        return findings
    env_example = repo_path / ".env.example"
    env_text = env_example.read_text() if env_example.exists() else ""
    src_text = (
        "\n".join(f.read_text() for f in (repo_path / "src").rglob("*.py"))
        if (repo_path / "src").is_dir()
        else ""
    )
    if "HEALTHCHECKS_URL_" not in env_text or (
        "HEALTHCHECKS_URL_" not in src_text and "healthchecks" not in src_text.lower()
    ):
        findings.append(
            _finding(
                "CD-007",
                "WARN",
                "cd_readiness",
                "Trigger cog is missing Healthchecks.io integration signals.",
                "Declare HEALTHCHECKS_URL_<SERVICE> in .env.example and ping it in trigger loop code.",
            )
        )
    return findings


def check_structured_logging(repo_path: Path) -> list[Finding]:
    """CD-009: Structured logging via shared library."""
    CHECK_ID = "CD-009"
    findings = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        text = py_file.read_text()
        if (
            "import logging" in text
            or "logging.basicConfig" in text
            or "logging.getLogger" in text
        ):
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


def check_no_hardcoded_secrets(repo_path: Path) -> list[Finding]:
    """CD-011: Doppler as canonical secret store."""
    CHECK_ID = "CD-011"
    import re

    findings = []

    for env_file in repo_path.rglob(".env*"):
        if env_file.name == ".env.example":
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


def check_no_manual_changelog(repo_path: Path) -> list[Finding]:
    """VER-004: Never manually edit version files or CHANGELOG."""
    CHECK_ID = "VER-004"
    import re

    findings = []
    changelog = repo_path / "CHANGELOG.md"
    if not changelog.exists():
        return findings
    lines = changelog.read_text().splitlines()
    sr_header = re.compile(r"^## \[\d+\.\d+\.\d+\]\(.+\) \(\d{4}-\d{2}-\d{2}\)$")
    bad_header = re.compile(r"^##\s+\d+\.\d+\.\d+")
    for line in lines:
        if bad_header.match(line) and not sr_header.match(line):
            findings.append(
                _finding(
                    "VER-004",
                    "ERROR",
                    "cd_readiness",
                    "CHANGELOG.md appears manually edited with non-semantic-release headers.",
                    "Let semantic-release manage version and changelog sections.",
                )
            )
            break
    return findings


def check_astro_framework(repo_path: Path) -> list[Finding]:
    """FE-001: Astro for all static sites."""
    CHECK_ID = "FE-001"
    findings = []
    pkg = repo_path / "package.json"
    pkg_text = pkg.read_text().lower() if pkg.exists() else ""
    has_config = (repo_path / "astro.config.mjs").exists() or (
        repo_path / "astro.config.ts"
    ).exists()
    if '"astro"' not in pkg_text or not has_config:
        findings.append(
            _finding(
                "FE-001",
                "WARN",
                "structural_conformance",
                "Astro framework signals are missing for frontend site.",
                "Use Astro with package dependency and astro.config.* file.",
            )
        )
    return findings


def check_vite_react_ts(repo_path: Path) -> list[Finding]:
    """FE-002: Vite + React + TypeScript for web apps."""
    CHECK_ID = "FE-002"
    findings = []
    pkg = repo_path / "package.json"
    pkg_text = pkg.read_text().lower() if pkg.exists() else ""
    tsconfig_exists = (repo_path / "tsconfig.json").exists()
    if '"typescript"' not in pkg_text or not tsconfig_exists:
        findings.append(
            _finding(
                "FE-002",
                "ERROR",
                "structural_conformance",
                "TypeScript setup missing for React web app.",
                "Add TypeScript dependency and tsconfig.json to satisfy FE-002.",
            )
        )
    for forbidden in ("webpack", "create-react-app", '"next"'):
        if forbidden in pkg_text:
            findings.append(
                _finding(
                    "FE-002",
                    "ERROR",
                    "structural_conformance",
                    f"Forbidden frontend stack signal found: {forbidden}.",
                    "Use Vite + React + TypeScript baseline for web apps.",
                )
            )
            break
    return findings


def check_tailwind(repo_path: Path) -> list[Finding]:
    """FE-003: Tailwind CSS for styling."""
    CHECK_ID = "FE-003"
    findings = []
    pkg = repo_path / "package.json"
    pkg_text = pkg.read_text().lower() if pkg.exists() else ""
    astro_mjs = repo_path / "astro.config.mjs"
    astro_ts = repo_path / "astro.config.ts"
    astro_cfg_text = ""
    if astro_mjs.exists():
        astro_cfg_text += "\n" + astro_mjs.read_text().lower()
    if astro_ts.exists():
        astro_cfg_text += "\n" + astro_ts.read_text().lower()

    has_astro_tailwind = "@astrojs/tailwind" in astro_cfg_text
    has_cfg = (
        (repo_path / "tailwind.config.js").exists()
        or (repo_path / "tailwind.config.ts").exists()
        or (repo_path / "tailwind.config.mjs").exists()
        or has_astro_tailwind
    )
    has_tailwind_signal = '"tailwindcss"' in pkg_text or has_astro_tailwind
    if not has_tailwind_signal or not has_cfg:
        findings.append(
            _finding(
                "FE-003",
                "WARN",
                "structural_conformance",
                "Tailwind CSS setup is incomplete or absent.",
                "Add tailwindcss dependency and tailwind.config.*.",
            )
        )
    if "styled-components" in pkg_text or "emotion" in pkg_text:
        findings.append(
            _finding(
                "FE-003",
                "WARN",
                "structural_conformance",
                "Alternative CSS-in-JS stack detected alongside/instead of Tailwind.",
                "Prefer Tailwind CSS as the primary styling approach.",
            )
        )
    return findings


def check_shadcn(repo_path: Path) -> list[Finding]:
    """FE-004: shadcn/ui for components."""
    CHECK_ID = "FE-004"
    findings = []
    pkg = repo_path / "package.json"
    pkg_text = pkg.read_text().lower() if pkg.exists() else ""
    has_radix = "@radix-ui/" in pkg_text
    has_ui_dir = (repo_path / "src" / "components" / "ui").is_dir()
    if not has_radix and not has_ui_dir:
        findings.append(
            _finding(
                "FE-004",
                "WARN",
                "structural_conformance",
                "shadcn/ui signals not detected (no Radix deps and no src/components/ui).",
                "Adopt shadcn/ui component structure for frontend consistency.",
            )
        )
    return findings


def check_react_hook_form_zod(repo_path: Path) -> list[Finding]:
    """FE-005: React Hook Form + Zod for forms and validation."""
    CHECK_ID = "FE-005"
    findings = []
    pkg = repo_path / "package.json"
    pkg_text = pkg.read_text().lower() if pkg.exists() else ""
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    form_exists = False
    for tsx in src.rglob("*.tsx"):
        text = tsx.read_text()
        if "<form" in text or "<Form" in text:
            form_exists = True
            break
    if form_exists and ('"react-hook-form"' not in pkg_text or '"zod"' not in pkg_text):
        findings.append(
            _finding(
                "FE-005",
                "WARN",
                "structural_conformance",
                "Form components exist but react-hook-form and/or zod is missing.",
                "Use React Hook Form + Zod for form handling and validation.",
            )
        )
    return findings


def check_railway_hosted_api(
    repo_path: Path, *, language: str = "python"
) -> list[Finding]:
    """API-001: API services are hosted on Railway (deterministic slice).

    Implements Railway deployment artifact presence (condition 1) and
    framework dependency presence (condition 3). Workflow-based checks for
    competing hosts belong to other rules.

    TODO(API-001-condition-2): ecosystem.yaml per-service ``host: railway`` is
    deferred — requires threading service context through deterministic checks.
    """
    CHECK_ID = "API-001"
    findings: list[Finding] = []
    has_railway = (
        (repo_path / "railway.toml").exists()
        or (repo_path / "railway.json").exists()
        or (repo_path / "nixpacks.toml").exists()
    )
    if not has_railway:
        findings.append(
            _finding(
                "API-001",
                "WARN",
                "structural_conformance",
                "Railway deployment configuration is missing (expected railway.toml, railway.json, or nixpacks.toml at repo root).",
                "Add Railway configuration so deployments are explicit and reviewable.",
            )
        )

    if language == "python":
        pyproject = repo_path / "pyproject.toml"
        py_text = pyproject.read_text().lower() if pyproject.exists() else ""
        req = repo_path / "requirements.txt"
        req_text = req.read_text().lower() if req.exists() else ""
        if "fastapi" not in py_text + "\n" + req_text:
            findings.append(
                _finding(
                    "API-001",
                    "WARN",
                    "structural_conformance",
                    "FastAPI is not declared for this Python API service.",
                    "Declare fastapi in pyproject.toml or requirements.txt dependencies.",
                )
            )
    else:
        pkg = repo_path / "package.json"
        pkg_text = pkg.read_text().lower() if pkg.exists() else ""
        if "hono" not in pkg_text:
            findings.append(
                _finding(
                    "API-001",
                    "WARN",
                    "structural_conformance",
                    "Hono is not declared for this TypeScript API service.",
                    "Add hono to package.json dependencies.",
                )
            )
    return findings


_NON_POSTGRES_STORE_MARKERS_PY = (
    "mysql",
    "mysqlclient",
    "pymysql",
    "aiosqlite",
    "sqlite3",
    "sqlalchemy[sqlite]",
    "mongodb",
    "motor",
    "pymongo",
    "dynamodb",
    "boto3",
)


def check_postgres_only_data_store(
    repo_path: Path, *, language: str = "python"
) -> list[Finding]:
    """API-002: PostgreSQL as the only primary relational data store.

    Scans declared Python and Node dependencies for obvious non-Postgres
    primary-store clients. Redis as a cache alongside Postgres is a judgment
    call — a bare ``redis`` dependency still flags here; narrow exemptions
    belong in evaluator.yaml when justified.
    """
    CHECK_ID = "API-002"
    findings: list[Finding] = []
    if language == "python":
        combined = ""
        for rel in (
            "pyproject.toml",
            "requirements.txt",
            "requirements/base.txt",
            "requirements/prod.txt",
        ):
            p = repo_path / rel
            if p.exists():
                combined += "\n" + p.read_text().lower()
        for marker in _NON_POSTGRES_STORE_MARKERS_PY:
            if marker in combined:
                findings.append(
                    _finding(
                        "API-002",
                        "ERROR",
                        "structural_conformance",
                        f"Non-Postgres data-store client or driver signal detected ({marker!r}).",
                        "Standardize on PostgreSQL as the primary relational store; remove alternate DB drivers unless formally excepted.",
                    )
                )
                break
    else:
        pkg = repo_path / "package.json"
        if not pkg.exists():
            return findings
        text = pkg.read_text().lower()
        node_markers = (
            '"mysql"',
            '"mysql2"',
            '"sqlite3"',
            '"better-sqlite3"',
            '"mongodb"',
            '"mongoose"',
            '"redis"',
            '"ioredis"',
            '"dynamodb"',
        )
        for marker in node_markers:
            if marker in text:
                findings.append(
                    _finding(
                        "API-002",
                        "ERROR",
                        "structural_conformance",
                        f"Non-Postgres data-store dependency present ({marker}).",
                        "Use PostgreSQL with an approved client (e.g. drizzle + postgres).",
                    )
                )
                break
    return findings


def _pyproject_and_requirements_text(repo_path: Path) -> str:
    parts: list[str] = []
    py = repo_path / "pyproject.toml"
    if py.exists():
        parts.append(py.read_text().lower())
    for rel in ("requirements.txt", "requirements/base.txt"):
        p = repo_path / rel
        if p.exists():
            parts.append(p.read_text().lower())
    return "\n".join(parts)


def check_prefect_present(
    repo_path: Path,
    cog_subtype: str = "pipeline",
) -> list[Finding]:
    """PIPE-001: Prefect dependency and usage signals.

    Pipeline cogs must declare ``prefect`` and use ``@flow`` in Python sources.
    Trigger cogs must declare ``prefect`` and call into deployment APIs such as
    ``run_deployment``.

    If ``prefect`` is not declared, emit only the dependency finding — do not
    also flag missing usage (the dependency gap is the root cause).
    """
    CHECK_ID = "PIPE-001"
    findings: list[Finding] = []
    blob = _pyproject_and_requirements_text(repo_path)
    if "prefect" not in blob:
        findings.append(
            _finding(
                "PIPE-001",
                "WARN",
                "pipeline_consistency",
                "Prefect is not declared as a dependency.",
                "Add prefect to pyproject.toml (or requirements.txt) for orchestrated flows.",
            )
        )
        return findings

    src = repo_path / "src"
    if not src.is_dir():
        findings.append(
            _finding(
                "PIPE-001",
                "WARN",
                "pipeline_consistency",
                "src/ tree missing — cannot verify Prefect usage in application code.",
                "Add a src/ package with flow entrypoints.",
            )
        )
        return findings

    py_src = "\n".join(f.read_text() for f in src.rglob("*.py"))
    if cog_subtype == "trigger":
        if "run_deployment" not in py_src:
            findings.append(
                _finding(
                    "PIPE-001",
                    "WARN",
                    "pipeline_consistency",
                    "Trigger cog source does not reference run_deployment (Prefect deployment API).",
                    "Use Prefect's Python client to trigger downstream deployments from the watcher/trigger cog.",
                )
            )
    elif "@flow" not in py_src:
        findings.append(
            _finding(
                "PIPE-001",
                "WARN",
                "pipeline_consistency",
                "Pipeline cog source has no @flow-decorated Prefect flow.",
                "Define orchestration entrypoints with @flow and register them from the cog main module.",
            )
        )
    return findings


def check_prefect_cloud_observability(
    repo_path: Path,
    cog_subtype: str = "pipeline",
) -> list[Finding]:
    """CD-005: Prefect Cloud wiring is documented for orchestrated cogs.

    When ``prefect`` is declared as a dependency, ``.env.example`` should
    document how the process reaches Prefect Cloud (``PREFECT_API_URL`` or
    equivalent). If Prefect is not a declared dependency, return no findings —
    PIPE-001 already covers the missing-dependency case.

    Competing schedulers referenced from application source (for example
    APScheduler) are surfaced at INFO with a manual review prompt — a
    deterministic scan cannot tell dev-only fallback from primary scheduling.

    GitHub Actions workflow scanning for competing orchestrators is owned by
    CD-006 (future wave) to avoid double-reporting once that check lands.
    """
    CHECK_ID = "CD-005"
    findings: list[Finding] = []
    blob = _pyproject_and_requirements_text(repo_path)
    if "prefect" not in blob:
        return findings

    env_example = repo_path / ".env.example"
    env_text = env_example.read_text() if env_example.exists() else ""
    lowered = env_text.lower()
    if not any(
        token in lowered
        for token in (
            "prefect_api_url",
            "prefect_api_key",
            "api.prefect.cloud",
            "prefect_cloud",
        )
    ):
        findings.append(
            _finding(
                "CD-005",
                "WARN",
                "cd_readiness",
                "Prefect Cloud connection is not documented in .env.example (expected PREFECT_API_URL or equivalent).",
                "Document Prefect Cloud API URL / workspace auth vars for operators.",
            )
        )

    src = repo_path / "src"
    if src.is_dir():
        py_src = "\n".join(f.read_text() for f in src.rglob("*.py"))
        if "apscheduler" in py_src.lower():
            findings.append(
                _finding(
                    "CD-005",
                    "INFO",
                    "cd_readiness",
                    "APScheduler is referenced — confirm it is only a local dev fallback, not the primary scheduler.",
                    "Primary orchestration should remain Prefect Cloud; document intentional APScheduler use if applicable.",
                )
            )
    return findings


def _fe008_version_is_pinned_exact(version: str) -> bool:
    v = str(version).strip().strip('"').strip("'")
    if not v or v in ("*", "latest"):
        return False
    if v.startswith(("^", "~", ">", "<")):
        return False
    return not re.search(r"\d+\.[xX](?:\D|$)", v)


def check_astro_pinned_versions(repo_path: Path) -> list[Finding]:
    """FE-008: Astro-related npm dependencies use exact semver pins.

    Flags range markers (^, ~, >=, …), ``latest``, wildcards, and ``1.x``-style
    placeholders in dependency strings for packages whose names contain
    ``astro``.
    """
    CHECK_ID = "FE-008"
    findings: list[Finding] = []
    pkg = repo_path / "package.json"
    if not pkg.exists():
        return findings
    try:
        import json as _json

        data = _json.loads(pkg.read_text())
    except Exception:
        findings.append(
            _finding(
                "FE-008",
                "WARN",
                "structural_conformance",
                "package.json is not valid JSON — cannot validate Astro pin policy.",
                "Repair package.json syntax.",
            )
        )
        return findings

    for section in ("dependencies", "devDependencies"):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, raw_ver in block.items():
            if "astro" not in str(name).lower():
                continue
            if not isinstance(raw_ver, str):
                findings.append(
                    _finding(
                        "FE-008",
                        "WARN",
                        "structural_conformance",
                        f"{section}: {name} version must be a string semver for FE-008 scanning.",
                        "Use explicit string versions for Astro-related packages.",
                    )
                )
                continue
            if not _fe008_version_is_pinned_exact(raw_ver):
                findings.append(
                    _finding(
                        "FE-008",
                        "WARN",
                        "structural_conformance",
                        f"{section}: {name} is not pinned to an exact version ({raw_ver!r}).",
                        "Pin Astro-related packages to exact versions (no ^, ~, >=, *, latest, or x-range placeholders).",
                    )
                )
    return findings


def check_gha_not_trigger_relay(repo_path: Path) -> list[Finding]:
    """CD-006: GitHub Actions must not relay repository triggers into app code.

    Scans ``.github/workflows`` for ``repository_dispatch`` paired with
    Prefect/deployment invocations, scheduled jobs calling Prefect Cloud, and
    internal trigger HTTP paths. Handles malformed YAML and the YAML 1.1
    ``on:`` → ``true`` quirk via ``suppress`` around ``yaml.safe_load``.
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
            with suppress(Exception):
                _yaml.safe_load(text)

            if "repository_dispatch" in low:
                relay = any(
                    k in low
                    for k in (
                        "prefect deployment run",
                        "prefect deploy",
                        "run_deployment(",
                        "npx prefect",
                    )
                ) or (
                    "/dispatches" in text
                    and any(k in low for k in ("curl ", "httpx.", "requests."))
                )
                pure_ci = ("pytest" in low or "ruff" in low) and not relay
                if relay and not pure_ci:
                    findings.append(
                        _finding(
                            "CD-006",
                            "WARN",
                            "structural_conformance",
                            f"repository_dispatch workflow appears to relay into automation ({rel}).",
                            "Prefer watcher-cog + Prefect; do not chain GitHub Actions into app invocations.",
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
                        "Use watcher-cog + Prefect instead of repository_dispatch relays.",
                    )
                )
    return findings


def check_adrs_present(repo_path: Path) -> list[Finding]:
    """DOC-005: ADR trail for non-trivial repos.

    The catalog spec asks for "fewer than 20 commits on main OR fewer than
    500 lines of source code" as the skip threshold. We use a LOC heuristic
    under src/ when git history is unavailable (zipball path), and also
    consult git log when it is available.
    """
    findings: list[Finding] = []
    src = repo_path / "src"
    loc = 0
    if src.is_dir():
        for p in src.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".mjs"}:
                continue
            with suppress(OSError, UnicodeDecodeError):
                loc += len(p.read_text().splitlines())

    # Git-history threshold when .git is present
    commit_count: int | None = None
    if (repo_path / ".git").is_dir():
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "rev-list", "--count", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                commit_count = int(result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, OSError):
            commit_count = None

    # Skip thresholds per catalog: <500 LOC OR <20 commits → repo too young
    if loc < 500:
        return findings
    if commit_count is not None and commit_count < 20:
        return findings

    dec = repo_path / "docs" / "decisions"
    if not dec.is_dir():
        findings.append(
            _finding(
                "DOC-005",
                "WARN",
                "documentation_coverage",
                "docs/decisions/ directory is missing for a non-trivial codebase.",
                "Add architecture decision records under docs/decisions/.",
            )
        )
        return findings

    if not any(dec.glob("ADR-*.md")):
        findings.append(
            _finding(
                "DOC-005",
                "WARN",
                "documentation_coverage",
                "docs/decisions/ exists but no ADR-NNN-*.md files were found.",
                "Author numbered ADR markdown files for significant decisions.",
            )
        )
    return findings


def check_response_shape_parity(
    repo_path: Path, *, language: str = "python"
) -> list[Finding]:
    """XSTACK-002: HTTP handlers expose typed response models / helpers."""
    CHECK_ID = "XSTACK-002"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    if language == "python":
        for py in src.rglob("*.py"):
            if "tests/" in str(py).replace("\\", "/"):
                continue
            try:
                text = py.read_text()
            except OSError:
                continue
            if not re.search(
                r"@(?:router|app)\.(get|post|put|delete|patch)\s*\(", text
            ):
                continue
            if "response_model=" not in text:
                findings.append(
                    _finding(
                        "XSTACK-002",
                        "WARN",
                        "structural_conformance",
                        f"FastAPI route missing response_model= in {py.relative_to(repo_path)}.",
                        "Declare response_model (or return type) for every public route.",
                    )
                )
                break
    else:
        for ts in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
            if "tests/" in str(ts).replace("\\", "/"):
                continue
            try:
                text = ts.read_text()
            except OSError:
                continue
            if not re.search(r"\bc\.json\s*\(", text):
                continue
            if "success(" in text or re.search(
                r"from\s+['\"][^'\"]*success", text, re.I
            ):
                continue
            findings.append(
                _finding(
                    "XSTACK-002",
                    "WARN",
                    "structural_conformance",
                    f"Hono handler uses raw c.json without success()/error() helper ({ts.relative_to(repo_path)}).",
                    "Wrap JSON responses with the shared success()/error() helpers.",
                )
            )
            break
    return findings


def _parse_astro_file(path: Path) -> dict[str, Any]:
    """Split an Astro file into frontmatter, <script> bodies, and client flags."""
    try:
        text = path.read_text()
    except OSError:
        return {"frontmatter": "", "scripts": [], "has_client": False, "body": ""}
    frontmatter = ""
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            body = parts[2]
    scripts = re.findall(r"<script[^>]*>([\s\S]*?)</script>", body, flags=re.IGNORECASE)
    has_client = bool(re.search(r"\bclient:[^\s=]+", body))
    return {
        "frontmatter": frontmatter,
        "scripts": scripts,
        "has_client": has_client,
        "body": body,
    }


def _extract_fetch_urls(chunk: str) -> list[str]:
    return re.findall(r"""fetch\s*\(\s*['"]([^'"]+)['"]""", chunk)


def check_astro_build_time_data(repo_path: Path) -> list[Finding]:
    """FE-009: Runtime fetch URLs must not duplicate build-time fetches."""
    CHECK_ID = "FE-009"
    findings: list[Finding] = []
    build_urls: set[str] = set()
    astro_files = list(repo_path.rglob("*.astro"))
    if not astro_files:
        return findings

    for path in astro_files:
        parsed = _parse_astro_file(path)
        for url in _extract_fetch_urls(parsed["frontmatter"]):
            build_urls.add(url)

    for path in astro_files:
        parsed = _parse_astro_file(path)
        if parsed["has_client"]:
            continue
        combined = "\n".join(parsed["scripts"])
        for url in _extract_fetch_urls(combined):
            if url in build_urls:
                findings.append(
                    _finding(
                        "FE-009",
                        "WARN",
                        "structural_conformance",
                        f"Astro component performs runtime fetch of URL also used in frontmatter ({path.relative_to(repo_path)}).",
                        "Move data to build-time fetch or isolate client-only access with client:* directives.",
                    )
                )
                break
    return findings


def check_astro_runtime_queries(repo_path: Path) -> list[Finding]:
    """FE-010: Undocumented runtime fetches in Astro islands."""
    CHECK_ID = "FE-010"
    findings: list[Finding] = []
    docs_blob = ""
    readme = repo_path / "README.md"
    if readme.exists():
        with suppress(OSError):
            docs_blob += readme.read_text().lower()
    for md in (
        (repo_path / "docs").rglob("*.md") if (repo_path / "docs").is_dir() else []
    ):
        with suppress(OSError):
            docs_blob += md.read_text().lower()

    for path in repo_path.rglob("*.astro"):
        parsed = _parse_astro_file(path)
        if parsed["has_client"]:
            continue
        combined = "\n".join(parsed["scripts"])
        if "fetch(" not in combined:
            continue
        for url in _extract_fetch_urls(combined):
            if url not in docs_blob:
                findings.append(
                    _finding(
                        "FE-010",
                        "WARN",
                        "structural_conformance",
                        f"Runtime fetch URL not documented in README/docs ({path.relative_to(repo_path)}: {url}).",
                        "Document external endpoints or mark the island as client:* when intentional.",
                    )
                )
                break
    return findings


def check_clerk_m2m_auth(repo_path: Path, *, language: str = "python") -> list[Finding]:
    """CD-012: Internal calls should use Clerk M2M JWTs, not static API keys."""
    CHECK_ID = "CD-012"
    findings: list[Finding] = []
    if language != "python":
        return findings
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    for py in src.rglob("*.py"):
        if "tests/" in str(py).replace("\\", "/"):
            continue
        try:
            text = py.read_text()
        except OSError:
            continue
        if "X-Internal-API-Key" in text:
            findings.append(
                _finding(
                    "CD-012",
                    "WARN",
                    "cd_readiness",
                    f"X-Internal-API-Key header referenced in {py.relative_to(repo_path)}.",
                    "Replace static internal API keys with Clerk machine-to-machine JWT acquisition.",
                )
            )
        elif (
            ("api.kaianolevine" in text or '"/v1/' in text)
            and "httpx" in text
            and not any(
                token in text.lower()
                for token in ("clerk", "jwt", "get_token", "authenticate")
            )
        ):
            findings.append(
                _finding(
                    "CD-012",
                    "WARN",
                    "cd_readiness",
                    f"Internal HTTP client without Clerk/JWT acquisition pattern ({py.relative_to(repo_path)}).",
                    "Acquire Clerk M2M JWTs before calling internal APIs.",
                )
            )
    return findings


def check_db_writes_use_upserts(repo_path: Path) -> list[Finding]:
    """PIPE-002: Database writes should use upsert / ON CONFLICT patterns."""
    CHECK_ID = "PIPE-002"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    for py in src.rglob("*.py"):
        if "tests/" in str(py).replace("\\", "/"):
            continue
        try:
            text = py.read_text()
        except OSError:
            continue
        if (
            "session.add(" in text
            and "on_conflict" not in text.lower()
            and "merge(" not in text
        ):
            findings.append(
                _finding(
                    "PIPE-002",
                    "WARN",
                    "pipeline_consistency",
                    f"session.add() without merge()/on_conflict in {py.relative_to(repo_path)}.",
                    "Prefer upsert patterns (merge or ON CONFLICT) for idempotent writes.",
                )
            )
        if (
            re.search(r"\bINSERT\s+INTO\b", text, re.I)
            and "ON CONFLICT" not in text.upper()
        ):
            findings.append(
                _finding(
                    "PIPE-002",
                    "WARN",
                    "pipeline_consistency",
                    f"Raw INSERT without ON CONFLICT in {py.relative_to(repo_path)}.",
                    "Use INSERT ... ON CONFLICT for idempotent persistence.",
                )
            )
    return findings


def check_inputs_not_deleted(repo_path: Path) -> list[Finding]:
    """PIPE-005: Input files must not be deleted or moved to trash."""
    CHECK_ID = "PIPE-005"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    for path in list(src.rglob("*.py")) + list(src.rglob("*.ts")):
        if "tests/" in str(path).replace("\\", "/"):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        if ".files().delete(" in text or "files().delete(" in text:
            findings.append(
                _finding(
                    "PIPE-005",
                    "WARN",
                    "pipeline_consistency",
                    f"Drive files().delete() referenced in {path.relative_to(repo_path)}.",
                    "Never delete raw input artifacts from Drive — move to derived outputs only.",
                )
            )
        if "trashed" in text.lower() and "update" in text.lower() and "files()" in text:
            findings.append(
                _finding(
                    "PIPE-005",
                    "WARN",
                    "pipeline_consistency",
                    f"Potential Drive trash update on input file in {path.relative_to(repo_path)}.",
                    "Avoid trashing upstream inputs; operate on copies.",
                )
            )
        if re.search(r"os\.(remove|unlink)\(|shutil\.rmtree\(", text) and re.search(
            r"\b(input_path|input_file|source_path|src_path|local_path)\b", text
        ):
            findings.append(
                _finding(
                    "PIPE-005",
                    "WARN",
                    "pipeline_consistency",
                    f"os.remove/unlink/rmtree may target input paths ({path.relative_to(repo_path)}).",
                    "Only remove scratch/temp paths — never input variables.",
                )
            )
    return findings


# Wave 9 — Coverage sweep. Implementations for 37 rules missing from
# engine but present in the catalog. Deliberately conservative on
# false-positive rate: where the rule has LLM-judgment components,
# only the mechanical part is implemented here.
# ==========================================================================


def check_orm_usage(repo_path: Path, language: str = "python") -> list[Finding]:
    """API-003: ORM usage required; no raw SQL outside ORM."""
    CHECK_ID = "API-003"
    findings: list[Finding] = []
    if language == "python":
        pyproject = repo_path / "pyproject.toml"
        py_text = pyproject.read_text().lower() if pyproject.exists() else ""
        if "sqlalchemy" not in py_text:
            findings.append(
                _finding(
                    "API-003",
                    "WARN",
                    "structural_conformance",
                    "api-service (Python) does not declare sqlalchemy in pyproject.toml.",
                    "Add sqlalchemy to dependencies and declare models via ORM.",
                )
            )
    else:
        pkg = repo_path / "package.json"
        pkg_text = pkg.read_text().lower() if pkg.exists() else ""
        if "drizzle-orm" not in pkg_text and "prisma" not in pkg_text:
            findings.append(
                _finding(
                    "API-003",
                    "WARN",
                    "structural_conformance",
                    "api-service (TypeScript) does not declare drizzle-orm or prisma.",
                    "Depend on an ORM (drizzle-orm preferred) instead of raw SQL.",
                )
            )
    return findings


def check_v1_route_prefix(repo_path: Path, language: str = "python") -> list[Finding]:
    """API-004: /v1/ prefix required on public routes."""
    CHECK_ID = "API-004"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    exempt_paths = ("/health", "/docs", "/openapi.json", "/metrics", "/redoc")

    if language == "python":
        import ast

        route_attrs = {"get", "post", "put", "delete", "patch", "head", "options"}
        for py_file in src.rglob("*.py"):
            try:
                text = py_file.read_text()
                tree = ast.parse(text)
            except Exception:
                continue
            rel = py_file.relative_to(repo_path)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    if not isinstance(dec.func, ast.Attribute):
                        continue
                    if dec.func.attr not in route_attrs:
                        continue
                    if not dec.args:
                        continue
                    path_arg = dec.args[0]
                    if not isinstance(path_arg, ast.Constant) or not isinstance(
                        path_arg.value, str
                    ):
                        continue
                    route = path_arg.value
                    if any(route.startswith(p) for p in exempt_paths):
                        continue
                    if not route.startswith("/v1/"):
                        findings.append(
                            _finding(
                                "API-004",
                                "ERROR",
                                "structural_conformance",
                                f"{rel}::{node.name}: route {route!r} missing /v1/ prefix.",
                                "Mount routes under /v1/ to support versioning.",
                            )
                        )
    else:
        import re

        route_re = re.compile(
            r"""(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*['"]([^'"]+)['"]"""
        )
        for ts_file in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
            try:
                text = ts_file.read_text()
            except Exception:
                continue
            rel = ts_file.relative_to(repo_path)
            for m in route_re.finditer(text):
                route = m.group(1)
                if any(route.startswith(p) for p in exempt_paths):
                    continue
                if not route.startswith("/v1/"):
                    findings.append(
                        _finding(
                            "API-004",
                            "ERROR",
                            "structural_conformance",
                            f"{rel}: route {route!r} missing /v1/ prefix.",
                            "Mount routes under /v1/ to support versioning.",
                        )
                    )
    return findings


def check_response_envelope_presence(repo_path: Path) -> list[Finding]:
    """API-005: Response envelope — endpoints declare response_model.

    Partial overlap with XSTACK-002, but this one specifically looks at
    shape consistency. Our deterministic pass just asserts response_model=
    exists on each endpoint (delegating shape inspection to the LLM).
    """
    CHECK_ID = "API-005"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    route_attrs = {"get", "post", "put", "delete", "patch"}
    flagged: set[tuple[str, str]] = set()
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in route_attrs:
                    continue
                has_rm = any(kw.arg == "response_model" for kw in dec.keywords)
                if not has_rm:
                    key = (str(rel), node.name)
                    if key in flagged:
                        continue
                    flagged.add(key)
                    findings.append(
                        _finding(
                            "API-005",
                            "ERROR",
                            "structural_conformance",
                            f"{rel}::{node.name}: endpoint missing response_model=.",
                            "Declare a response_model Pydantic class so the envelope "
                            "shape is explicit.",
                        )
                    )
    return findings


def check_owner_id_column(repo_path: Path) -> list[Finding]:
    """API-006: SQLAlchemy models carry owner_id."""
    CHECK_ID = "API-006"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    # Variable names that hint a model is internal/lookup and exempt.
    exempt_suffixes = ("_lookup", "_config", "_enum", "Lookup", "Config", "Enum")

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            # Heuristic: class is a SQLAlchemy model if it inherits from a
            # class ending in Base or DeclarativeBase.
            base_names = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    base_names.append(b.id)
                elif isinstance(b, ast.Attribute):
                    base_names.append(b.attr)
            if not any(bn.endswith("Base") or "Declarative" in bn for bn in base_names):
                continue
            if any(node.name.endswith(s) for s in exempt_suffixes):
                continue
            # Look for owner_id assignment in the class body
            has_owner_id = False
            for stmt in node.body:
                targets: list = []
                if isinstance(stmt, ast.Assign):
                    targets = stmt.targets
                elif isinstance(stmt, ast.AnnAssign):
                    targets = [stmt.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id == "owner_id":
                        has_owner_id = True
            if not has_owner_id:
                # Also check for comment indicating internal table
                findings.append(
                    _finding(
                        "API-006",
                        "WARN",
                        "structural_conformance",
                        f"{rel}::{node.name}: SQLAlchemy model missing owner_id column.",
                        "Add owner_id to enforce multi-tenant ownership. If this is a "
                        "join/lookup/config table, suffix the class name (_lookup, "
                        "_config, _enum) or document the exception in evaluator.yaml.",
                    )
                )
    return findings


def check_clerk_auth_dep(repo_path: Path, language: str = "python") -> list[Finding]:
    """API-007: Clerk verification helper referenced."""
    CHECK_ID = "API-007"
    findings: list[Finding] = []

    if language == "python":
        src = repo_path / "src"
        if not src.is_dir():
            return findings
        has_verify_token_usage = False
        for py_file in src.rglob("*.py"):
            try:
                text = py_file.read_text()
            except Exception:
                continue
            if "verify_token" in text or "clerk" in text.lower():
                has_verify_token_usage = True
                break
        if not has_verify_token_usage:
            findings.append(
                _finding(
                    "API-007",
                    "WARN",
                    "structural_conformance",
                    "api-service (Python) has no visible Clerk or verify_token usage.",
                    "Import verify_token from common-python-utils and add "
                    "Depends(verify_token) to protected routes.",
                )
            )
    else:
        pkg = repo_path / "package.json"
        pkg_text = pkg.read_text() if pkg.exists() else ""
        if "@clerk" not in pkg_text and "common-typescript-utils" not in pkg_text:
            findings.append(
                _finding(
                    "API-007",
                    "WARN",
                    "structural_conformance",
                    "api-service (TypeScript) has no Clerk SDK or common-typescript-utils dep.",
                    "Add @clerk/clerk-sdk-node or use verifyClerkToken from "
                    "common-typescript-utils.",
                )
            )
    return findings


def check_unauthenticated_routes(
    repo_path: Path, language: str = "python"
) -> list[Finding]:
    """API-008: Unauthenticated routes must be intentional.

    Deterministic part: flag FastAPI routes with no Depends(...) in the
    function signature. Whether the lack-of-auth is intentional is the
    LLM's job.
    """
    CHECK_ID = "API-008"
    findings: list[Finding] = []
    if language != "python":
        return findings
    import ast

    src = repo_path / "src"
    if not src.is_dir():
        return findings

    route_attrs = {"get", "post", "put", "delete", "patch"}
    exempt_paths = ("/health", "/metrics", "/docs", "/openapi.json", "/redoc")

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Is this a route handler?
            route_path: str | None = None
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in route_attrs:
                    continue
                if (
                    dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)
                ):
                    route_path = dec.args[0].value
                    break
            if route_path is None:
                continue
            if any(route_path.startswith(p) for p in exempt_paths):
                continue
            # Any Depends(...) default in args?
            has_depends = False
            for arg_default in node.args.defaults + node.args.kw_defaults:
                if arg_default is None:
                    continue
                if (
                    isinstance(arg_default, ast.Call)
                    and isinstance(arg_default.func, ast.Name)
                    and arg_default.func.id == "Depends"
                ):
                    has_depends = True
                    break
            if not has_depends:
                findings.append(
                    _finding(
                        "API-008",
                        "ERROR",
                        "structural_conformance",
                        f"{rel}::{node.name}: route {route_path!r} has no Depends(...) auth.",
                        "Add Depends(verify_token) for protected routes, or document the "
                        "intentional public access in the route's description.",
                    )
                )
    return findings


def check_cors_config(repo_path: Path, language: str = "python") -> list[Finding]:
    """API-009: CORS middleware configured; no hardcoded origins."""
    CHECK_ID = "API-009"
    findings: list[Finding] = []
    src = repo_path / "src"

    if language == "python":
        has_cors = False
        has_cors_origins_env = False
        if src.is_dir():
            for py_file in src.rglob("*.py"):
                try:
                    text = py_file.read_text()
                except Exception:
                    continue
                if "CORSMiddleware" in text:
                    has_cors = True
                if (
                    "CORS_ORIGINS" in text
                    or 'getenv("CORS_ORIGINS"' in text
                    or "getenv('CORS_ORIGINS'" in text
                ):
                    has_cors_origins_env = True
        if not has_cors:
            findings.append(
                _finding(
                    "API-009",
                    "ERROR",
                    "structural_conformance",
                    "api-service (Python) has no CORSMiddleware configuration.",
                    "Register CORSMiddleware from fastapi.middleware.cors with origins "
                    "sourced from CORS_ORIGINS env var.",
                )
            )
        elif not has_cors_origins_env:
            findings.append(
                _finding(
                    "API-009",
                    "WARN",
                    "structural_conformance",
                    "api-service (Python) uses CORSMiddleware but CORS_ORIGINS env var is not referenced.",
                    "Source allowed origins from CORS_ORIGINS rather than hardcoded values.",
                )
            )
    else:
        has_cors_import = False
        if src.is_dir():
            for ts_file in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
                try:
                    text = ts_file.read_text()
                except Exception:
                    continue
                if (
                    "cors(" in text
                    or "from 'hono/cors'" in text
                    or 'from "hono/cors"' in text
                ):
                    has_cors_import = True
                    break
        if not has_cors_import:
            findings.append(
                _finding(
                    "API-009",
                    "ERROR",
                    "structural_conformance",
                    "api-service (TypeScript) has no cors() middleware import.",
                    "Import and register cors() from hono/cors with origins from process.env.CORS_ORIGINS.",
                )
            )
    return findings


def check_health_endpoint(repo_path: Path, language: str = "python") -> list[Finding]:
    """API-010: GET /health endpoint present."""
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    exts = ("*.py",) if language == "python" else ("*.ts", "*.tsx")
    has_health = False
    for ext in exts:
        for f in src.rglob(ext):
            try:
                text = f.read_text()
            except Exception:
                continue
            if "/health" in text and (
                "def health" in text or '"/health"' in text or "'/health'" in text
            ):
                has_health = True
                break
        if has_health:
            break
    if not has_health:
        findings.append(
            _finding(
                "API-010",
                "WARN",
                "structural_conformance",
                "api-service has no visible GET /health endpoint.",
                "Add a GET /health route that returns {'status': 'ok'} with no auth "
                "and no DB queries.",
            )
        )
    return findings


def check_migration_in_ci(
    repo_path: Path,
    language: str = "python",
    monorepo_root: Path | None = None,
) -> list[Finding]:
    """API-011: CI runs database migrations on deploy.

    Python (Alembic): ci.yml contains 'alembic upgrade head' in a deploy job.
    TypeScript (Drizzle): ci.yml contains 'drizzle-kit push' or 'drizzle-kit migrate'.
    For monorepo services, also checks the workspace root ci.yml.
    """
    findings: list[Finding] = []

    ci_texts = []
    ci = repo_path / ".github" / "workflows" / "ci.yml"
    if ci.exists():
        with suppress(Exception):
            ci_texts.append(ci.read_text())
    if monorepo_root is not None:
        root_ci = monorepo_root / ".github" / "workflows" / "ci.yml"
        if root_ci.exists():
            with suppress(Exception):
                ci_texts.append(root_ci.read_text())

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


def check_auth_header_parity(repo_path: Path) -> list[Finding]:
    """AUTH-002: Auth header parity — shared library vs api.

    Evaluator-cog can't compare two repos at once. We check a lighter
    property: auth.py (if present) has a cross-reference comment to
    common-python-utils.
    """
    CHECK_ID = "AUTH-002"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    # Find auth.py files
    auth_files = list(src.rglob("auth.py"))
    if not auth_files:
        return findings
    for auth_file in auth_files:
        try:
            text = auth_file.read_text()
        except Exception:
            continue
        if "common-python-utils" not in text and "common_python_utils" not in text:
            rel = auth_file.relative_to(repo_path)
            findings.append(
                _finding(
                    "AUTH-002",
                    "WARN",
                    "cross_repo_coherence",
                    f"{rel}: auth.py has no reference to common-python-utils.",
                    "Add a cross-reference comment noting the auth header parity with "
                    "CommonPythonApiClient.",
                )
            )
    return findings


def check_env_var_prefix(repo_path: Path) -> list[Finding]:
    """XSTACK-004: Client-exposed env vars use PUBLIC_ or VITE_ prefix."""
    CHECK_ID = "XSTACK-004"
    import re

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    # Detect repo flavor from presence of astro vs vite config
    has_astro = (repo_path / "astro.config.mjs").exists() or (
        repo_path / "astro.config.ts"
    ).exists()
    has_vite = (
        (repo_path / "vite.config.ts").exists()
        or (repo_path / "vite.config.js").exists()
        or (repo_path / "vite.config.mjs").exists()
    )

    expected_prefix = None
    wrong_prefix = None
    if has_astro:
        expected_prefix = "PUBLIC_"
        wrong_prefix = "VITE_"
    elif has_vite:
        expected_prefix = "VITE_"
        wrong_prefix = "PUBLIC_"
    else:
        return findings  # Not a frontend repo with client-side env vars

    env_re = re.compile(r"""import\.meta\.env\.(\w+)""")
    for f in (
        list(src.rglob("*.ts"))
        + list(src.rglob("*.tsx"))
        + list(src.rglob("*.astro"))
        + list(src.rglob("*.js"))
        + list(src.rglob("*.jsx"))
    ):
        try:
            text = f.read_text()
        except Exception:
            continue
        rel = f.relative_to(repo_path)
        for m in env_re.finditer(text):
            var = m.group(1)
            if var.startswith(wrong_prefix):
                findings.append(
                    _finding(
                        "XSTACK-004",
                        "WARN",
                        "structural_conformance",
                        f"{rel}: env var {var} uses {wrong_prefix} prefix in a repo expecting {expected_prefix}.",
                        f"Rename to {expected_prefix}{var[len(wrong_prefix) :]}.",
                    )
                )
    return findings


def check_logger_misuse(repo_path: Path) -> list[Finding]:
    """CD-008: logger.error not used for expected outcomes."""
    CHECK_ID = "CD-008"
    import re

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    noise_patterns = (
        r"not found",
        r"no files",
        r"skipping",
        r"already exists",
        r"no new items",
    )
    noise_re = re.compile("|".join(noise_patterns), re.IGNORECASE)
    error_call_re = re.compile(r"""logger\.error\s*\(\s*['"]([^'"]+)['"]""")
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for m in error_call_re.finditer(text):
            msg = m.group(1)
            if noise_re.search(msg):
                findings.append(
                    _finding(
                        "CD-008",
                        "WARN",
                        "structural_conformance",
                        f"{rel}: logger.error used for expected outcome: {msg!r}.",
                        "Downgrade to logger.warning or logger.info — errors should "
                        "indicate unexpected failures.",
                    )
                )
    return findings


def check_three_layer_observability(
    repo_path: Path, cog_subtype: str | None = None
) -> list[Finding]:
    """CD-010: Three-layer observability — Healthchecks + logger + Sentry."""
    CHECK_ID = "CD-010"
    findings: list[Finding] = []
    src = repo_path / "src"
    env_example = repo_path / ".env.example"

    env_text = env_example.read_text() if env_example.exists() else ""
    src_text = ""
    if src.is_dir():
        for py_file in src.rglob("*.py"):
            try:
                src_text += "\n" + py_file.read_text()
            except Exception:
                continue

    # Layer 1: Healthchecks — only required for worker-style services
    # (pipeline-cog, trigger-cog). HTTP services (api-service) rely on
    # Railway restart.
    if cog_subtype in ("pipeline", "trigger") and (
        "HEALTHCHECKS_URL" not in env_text or "healthchecks.io" not in src_text.lower()
    ):
        findings.append(
            _finding(
                "CD-010",
                "ERROR",
                "structural_conformance",
                "Layer 1 missing: no HEALTHCHECKS_URL env var or healthchecks.io ping in source.",
                "Add HEALTHCHECKS_URL to .env.example and ping healthchecks.io from "
                "the main loop.",
            )
        )

    # Layer 2: structured logging via shared library
    if "common_python_utils" not in src_text and "mini_app_polis" not in src_text:
        findings.append(
            _finding(
                "CD-010",
                "ERROR",
                "structural_conformance",
                "Layer 2 missing: no common-python-utils logger usage.",
                "Import the shared logger from common_python_utils and use it throughout.",
            )
        )

    # Layer 3: Sentry
    if "sentry_sdk" not in src_text or "SENTRY_DSN" not in env_text:
        findings.append(
            _finding(
                "CD-010",
                "ERROR",
                "structural_conformance",
                "Layer 3 missing: no sentry_sdk.init() or SENTRY_DSN in .env.example.",
                "Initialise sentry_sdk at entry point and add SENTRY_DSN (or a "
                "service-specific variant) to .env.example.",
            )
        )
    return findings


def check_cloudflare_pages_deploy(repo_path: Path) -> list[Finding]:
    """CD-014: Static site deployed via Cloudflare Pages."""
    CHECK_ID = "CD-014"
    findings: list[Finding] = []
    ci = repo_path / ".github" / "workflows" / "ci.yml"
    readme = repo_path / "README.md"

    ci_text = ci.read_text() if ci.exists() else ""
    readme_text = readme.read_text() if readme.exists() else ""

    has_cf_pages = (
        "cloudflare/pages-action" in ci_text
        or "wrangler pages" in ci_text
        or "pages.dev" in readme_text
        or "Cloudflare Pages" in readme_text
    )

    # Check for competing deploy targets
    has_netlify = (repo_path / "netlify.toml").exists()
    has_vercel = (repo_path / "vercel.json").exists()
    has_gh_pages = "gh-pages" in ci_text or "peaceiris/actions-gh-pages" in ci_text

    if has_netlify:
        findings.append(
            _finding(
                "CD-014",
                "WARN",
                "structural_conformance",
                "Static site has netlify.toml — expected Cloudflare Pages deployment.",
                "Remove netlify.toml and configure Cloudflare Pages deploy instead.",
            )
        )
    if has_vercel:
        findings.append(
            _finding(
                "CD-014",
                "WARN",
                "structural_conformance",
                "Static site has vercel.json — expected Cloudflare Pages deployment.",
                "Remove vercel.json and configure Cloudflare Pages deploy instead.",
            )
        )
    if has_gh_pages:
        findings.append(
            _finding(
                "CD-014",
                "WARN",
                "structural_conformance",
                "Static site uses GitHub Pages deploy — expected Cloudflare Pages.",
                "Switch to Cloudflare Pages deploy.",
            )
        )

    if not has_cf_pages and not (has_netlify or has_vercel or has_gh_pages):
        findings.append(
            _finding(
                "CD-014",
                "WARN",
                "structural_conformance",
                "No deployment target detected (no Cloudflare Pages markers in ci.yml or README).",
                "Document the Cloudflare Pages deploy in ci.yml or README.",
            )
        )
    return findings


def check_public_docstrings(repo_path: Path) -> list[Finding]:
    """DOC-006: Public functions/classes have docstrings."""
    CHECK_ID = "DOC-006"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            if node.name.startswith("_"):
                continue
            # Skip dunder methods
            if node.name.startswith("__") and node.name.endswith("__"):
                continue
            if ast.get_docstring(node):
                continue
            findings.append(
                _finding(
                    "DOC-006",
                    "WARN",
                    "documentation_coverage",
                    f"{rel}::{node.name}: public {type(node).__name__.replace('Def', '').lower()} missing docstring.",
                    "Add a docstring explaining the purpose and usage.",
                )
            )
    return findings


def check_pydantic_field_descriptions(repo_path: Path) -> list[Finding]:
    """DOC-007: Pydantic fields use Field(description=...)."""
    CHECK_ID = "DOC-007"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    base_names.append(b.id)
                elif isinstance(b, ast.Attribute):
                    base_names.append(b.attr)
            if "BaseModel" not in base_names:
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                if not isinstance(stmt.target, ast.Name):
                    continue
                fname = stmt.target.id
                if fname.startswith("_"):
                    continue
                # Check if value is Field(... description=...)
                has_description = False
                if (
                    stmt.value
                    and isinstance(stmt.value, ast.Call)
                    and (
                        isinstance(stmt.value.func, ast.Name)
                        and stmt.value.func.id == "Field"
                    )
                ):
                    has_description = any(
                        kw.arg == "description" for kw in stmt.value.keywords
                    )
                if not has_description:
                    findings.append(
                        _finding(
                            "DOC-007",
                            "WARN",
                            "documentation_coverage",
                            f"{rel}::{node.name}.{fname}: Pydantic field missing Field(description=...).",
                            "Wrap the field with Field(description='...') for OpenAPI docs.",
                        )
                    )
    return findings


def check_fastapi_route_docs(repo_path: Path) -> list[Finding]:
    """DOC-010: FastAPI route decorators have summary=, description=, response_model=."""
    CHECK_ID = "DOC-010"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    route_attrs = {"get", "post", "put", "delete", "patch"}
    required = ("summary", "description", "response_model")

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in route_attrs:
                    continue
                kwargs = {kw.arg for kw in dec.keywords}
                missing = [r for r in required if r not in kwargs]
                if missing:
                    findings.append(
                        _finding(
                            "DOC-010",
                            "ERROR",
                            "documentation_coverage",
                            f"{rel}::{node.name}: route decorator missing: {', '.join(missing)}.",
                            "Add all three (summary, description, response_model) to the "
                            "route decorator for complete OpenAPI docs.",
                        )
                    )
    return findings


def check_unauthenticated_routes_documented(repo_path: Path) -> list[Finding]:
    """DOC-011: Unauthenticated routes document their intent."""
    CHECK_ID = "DOC-011"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    route_attrs = {"get", "post", "put", "delete", "patch"}
    exempt_paths = ("/health", "/metrics", "/docs", "/openapi.json", "/redoc")
    public_markers = (
        "intentionally public",
        "no auth required",
        "read-only public",
        "public endpoint",
    )

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route_path: str | None = None
            description: str | None = None
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in route_attrs:
                    continue
                if (
                    dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)
                ):
                    route_path = dec.args[0].value
                for kw in dec.keywords:
                    if (
                        kw.arg == "description"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        description = kw.value.value
            if route_path is None:
                continue
            if any(route_path.startswith(p) for p in exempt_paths):
                continue
            # Has auth?
            has_depends = False
            for arg_default in node.args.defaults + node.args.kw_defaults:
                if arg_default is None:
                    continue
                if (
                    isinstance(arg_default, ast.Call)
                    and isinstance(arg_default.func, ast.Name)
                    and arg_default.func.id == "Depends"
                ):
                    has_depends = True
                    break
            if has_depends:
                continue
            # No auth — must have public-intent marker in description or docstring
            ds = ast.get_docstring(node) or ""
            combined = (description or "") + " " + ds
            if not any(marker in combined.lower() for marker in public_markers):
                findings.append(
                    _finding(
                        "DOC-011",
                        "WARN",
                        "documentation_coverage",
                        f"{rel}::{node.name}: unauthenticated route {route_path!r} lacks public-intent marker.",
                        "Add 'intentionally public' or 'no auth required' to the route "
                        "description or docstring.",
                    )
                )
    return findings


def check_fetch_error_handling(repo_path: Path) -> list[Finding]:
    """FE-006: Astro fetch calls wrapped in try/catch with fallback."""
    CHECK_ID = "FE-006"
    import re

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for astro_file in src.rglob("*.astro"):
        try:
            text = astro_file.read_text()
        except Exception:
            continue
        # Extract frontmatter
        m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not m:
            continue
        fm = m.group(1)
        if "fetch(" not in fm:
            continue
        # Crude heuristic: the frontmatter should contain `try` and `catch`
        # somewhere around the fetch call.
        if "try" not in fm or "catch" not in fm:
            rel = astro_file.relative_to(repo_path)
            findings.append(
                _finding(
                    "FE-006",
                    "ERROR",
                    "structural_conformance",
                    f"{rel}: frontmatter fetch() without try/catch error handling.",
                    "Wrap fetch calls in try/catch with a fallback value so build "
                    "succeeds when the API is unavailable.",
                )
            )
    return findings


def check_monorepo_shared_lib_root(
    repo_path: Path,
    workspace_package_json_text: str | None = None,
    language: str = "python",
) -> list[Finding]:
    """MONO-001: Monorepo shared-lib check is satisfied by workspace root.

    When a service sits inside a monorepo, XSTACK-001's dep check should
    also honor the workspace root package.json. This rule itself is a
    sanity check that the root actually carries the shared lib when the
    per-app does not.
    """
    CHECK_ID = "MONO-001"
    findings: list[Finding] = []
    if language != "typescript":
        return findings  # MONO-001 cares about pnpm workspaces; TS only in practice
    if workspace_package_json_text is None:
        return findings  # Not a monorepo context
    per_app = repo_path / "package.json"
    per_app_text = per_app.read_text() if per_app.exists() else ""
    if (
        "common-typescript-utils" not in per_app_text
        and "common-typescript-utils" not in workspace_package_json_text
    ):
        findings.append(
            _finding(
                "MONO-001",
                "ERROR",
                "cross_repo_coherence",
                "Monorepo: common-typescript-utils absent from both workspace root "
                "and per-app package.json.",
                "Add common-typescript-utils to the workspace root to satisfy XSTACK-001 "
                "for all apps.",
            )
        )
    return findings


def check_monorepo_root_ci(
    repo_path: Path, monorepo_root: Path | None = None
) -> list[Finding]:
    """MONO-002: Monorepo root carries CI; per-app doesn't need its own."""
    CHECK_ID = "MONO-002"
    findings: list[Finding] = []
    if monorepo_root is None:
        return findings  # Not a monorepo
    root_ci = monorepo_root / ".github" / "workflows" / "ci.yml"
    if not root_ci.exists():
        findings.append(
            _finding(
                "MONO-002",
                "WARN",
                "structural_conformance",
                "Monorepo root missing .github/workflows/ci.yml.",
                "Add a root CI workflow that covers all sibling apps.",
            )
        )
    return findings


def check_per_item_vs_collection_tasks(repo_path: Path) -> list[Finding]:
    """PIPE-003: Per-item and collection tasks are separate."""
    CHECK_ID = "PIPE-003"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            has_task_decorator = any(
                (isinstance(d, ast.Name) and d.id == "task")
                or (
                    isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Name)
                    and d.func.id == "task"
                )
                or (isinstance(d, ast.Attribute) and d.attr == "task")
                for d in node.decorator_list
            )
            if not has_task_decorator:
                continue
            body_src = ast.unparse(node)
            # Heuristic: task references both per-item (append, add, process)
            # AND collection rebuild (rebuild, full, all_items, summary)
            per_item_markers = ["append(", "add(", "insert("]
            collection_markers = ["rebuild", "all_items", "full_list", "regenerate"]
            has_per_item = any(m in body_src for m in per_item_markers)
            has_collection = any(m in body_src for m in collection_markers)
            if has_per_item and has_collection:
                findings.append(
                    _finding(
                        "PIPE-003",
                        "WARN",
                        "pipeline_consistency",
                        f"{rel}::{node.name}: task mixes per-item processing with collection rebuild.",
                        "Split into two tasks — one per-item, one collection-level — so "
                        "retries can target the right level.",
                    )
                )
    return findings


def check_shared_resource_concurrency(repo_path: Path) -> list[Finding]:
    """PIPE-004: Flows writing shared resources use concurrency guards."""
    CHECK_ID = "PIPE-004"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_flow = any(
                (isinstance(d, ast.Name) and d.id == "flow")
                or (
                    isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Name)
                    and d.func.id == "flow"
                )
                or (isinstance(d, ast.Attribute) and d.attr == "flow")
                for d in node.decorator_list
            )
            if not is_flow:
                continue
            body_src = ast.unparse(node)
            writes_shared_resource = any(
                m in body_src
                for m in [
                    "session.commit()",
                    "session.add(",
                    "drive_service.files().move",
                    "drive_service.files().update",
                    ".post(",
                    ".patch(",
                ]
            )
            if not writes_shared_resource:
                continue
            has_concurrency_param = False
            for d in node.decorator_list:
                if isinstance(d, ast.Call):
                    for kw in d.keywords:
                        if kw.arg == "concurrency_limit":
                            has_concurrency_param = True
            has_concurrency_block = (
                "with concurrency(" in body_src or "concurrency.sync" in body_src
            )
            if not (has_concurrency_param or has_concurrency_block):
                findings.append(
                    _finding(
                        "PIPE-004",
                        "ERROR",
                        "pipeline_consistency",
                        f"{rel}::{node.name}: flow writes to shared resource without concurrency guard.",
                        "Add concurrency_limit= on the @flow decorator, or wrap the write "
                        "block with a 'with concurrency(...)' slot from prefect.concurrency.sync.",
                    )
                )
    return findings


def check_prefect_run_logger(repo_path: Path) -> list[Finding]:
    """PIPE-006: Prefect flows use get_run_logger() with fallback."""
    CHECK_ID = "PIPE-006"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_flow = any(
                (isinstance(d, ast.Name) and d.id == "flow")
                or (
                    isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Name)
                    and d.func.id == "flow"
                )
                or (isinstance(d, ast.Attribute) and d.attr == "flow")
                for d in node.decorator_list
            )
            if not is_flow:
                continue
            body_src = ast.unparse(node)
            if "get_run_logger" not in body_src:
                findings.append(
                    _finding(
                        "PIPE-006",
                        "WARN",
                        "pipeline_consistency",
                        f"{rel}::{node.name}: flow does not call get_run_logger().",
                        "Use get_run_logger() inside flows for Prefect-integrated logging; "
                        "fall back to stdlib logging outside Prefect context.",
                    )
                )
    return findings


def check_final_evaluation_task(
    repo_path: Path, cog_subtype: str | None = None
) -> list[Finding]:
    """PIPE-011: Pipeline cogs end with an AI evaluation task.

    Exempt: trigger-cogs (they fire flow runs, don't run pipelines),
    and evaluator-cog itself.
    """
    CHECK_ID = "PIPE-011"
    findings: list[Finding] = []
    if cog_subtype == "trigger":
        return findings
    # Check if this is evaluator-cog itself
    if (repo_path / "src" / "evaluator_cog").is_dir():
        return findings
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    evaluation_markers = (
        "pipeline_eval",
        "evaluation_client",
        "/v1/evaluations",
        "/v1/pipeline_evaluations",
        "PipelineEvaluator",
    )
    found_marker = False
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
        except Exception:
            continue
        if any(m in text for m in evaluation_markers):
            found_marker = True
            break
    if not found_marker:
        findings.append(
            _finding(
                "PIPE-011",
                "WARN",
                "pipeline_consistency",
                "No AI evaluation task found in pipeline-cog source.",
                "Add a final task that writes to pipeline_evaluations (via the evaluation "
                "client from common-python-utils) so quality can be tracked.",
            )
        )
    return findings


def check_hardcoded_retry_delay(repo_path: Path) -> list[Finding]:
    """PIPE-012: retry_delay_seconds not hardcoded to non-zero."""
    CHECK_ID = "PIPE-012"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "retry_delay_seconds":
                    continue
                # Must be a hardcoded non-zero numeric literal
                if (
                    isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, (int, float))
                    and kw.value.value != 0
                ):
                    # Check for PYTEST_CURRENT_TEST guard nearby
                    py_text = py_file.read_text()
                    if "PYTEST_CURRENT_TEST" not in py_text:
                        findings.append(
                            _finding(
                                "PIPE-012",
                                "WARN",
                                "pipeline_consistency",
                                f"{rel}: retry_delay_seconds={kw.value.value} hardcoded without PYTEST_CURRENT_TEST guard.",
                                "Source from Settings field or wrap with os.getenv('PYTEST_CURRENT_TEST') "
                                "conditional so tests don't sleep.",
                            )
                        )
    return findings


def check_per_item_error_handling(repo_path: Path) -> list[Finding]:
    """PRIN-002: Per-item try/except in pipeline entry points.

    Heuristic: if a flow loops over a collection and there's no try/except
    inside the loop, flag it — one bad item would abort the full run.
    """
    CHECK_ID = "PRIN-002"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_flow = any(
                (isinstance(d, ast.Name) and d.id == "flow")
                or (
                    isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Name)
                    and d.func.id == "flow"
                )
                or (isinstance(d, ast.Attribute) and d.attr == "flow")
                for d in node.decorator_list
            )
            if not is_flow:
                continue
            # Find loops in the function body
            for sub in ast.walk(node):
                if not isinstance(sub, (ast.For, ast.AsyncFor)):
                    continue
                # Does the loop body contain a Try?
                has_try = any(isinstance(x, ast.Try) for x in ast.walk(sub))
                if not has_try:
                    findings.append(
                        _finding(
                            "PRIN-002",
                            "ERROR",
                            "principles",
                            f"{rel}::{node.name}: loop body has no try/except — one bad item aborts the full run.",
                            "Wrap per-item processing in try/except so a single failure doesn't "
                            "crash the whole flow.",
                        )
                    )
                    break  # One finding per flow is enough
    return findings


def check_production_observability(
    repo_path: Path, language: str = "python"
) -> list[Finding]:
    """PRIN-005: Production services have Sentry + structured logging."""
    CHECK_ID = "PRIN-005"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    src_text = ""
    for ext in ("*.py",) if language == "python" else ("*.ts", "*.tsx"):
        for f in src.rglob(ext):
            try:
                src_text += "\n" + f.read_text()
            except Exception:
                continue

    missing: list[str] = []
    if language == "python":
        if "sentry_sdk" not in src_text:
            missing.append("Sentry")
        if "common_python_utils" not in src_text and "mini_app_polis" not in src_text:
            missing.append("structured logging (common-python-utils)")
    else:
        if "@sentry/" not in src_text and "Sentry.init" not in src_text:
            missing.append("Sentry")
        if "common-typescript-utils" not in src_text:
            missing.append("structured logging (common-typescript-utils)")

    if missing:
        findings.append(
            _finding(
                "PRIN-005",
                "ERROR",
                "principles",
                f"Production service missing: {', '.join(missing)}.",
                "Add the missing observability components.",
            )
        )
    return findings


def check_pydantic_for_external_data(repo_path: Path) -> list[Finding]:
    """PY-004: External data goes through Pydantic.

    Heuristic: flag files that access response.json() or csv.DictReader
    results directly without defining a BaseModel subclass.
    """
    CHECK_ID = "PY-004"
    import re

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    suspect_patterns = (
        r"\.json\(\)\[",  # response.json()["..."]
        r"csv\.DictReader",
        r"csv\.reader",
    )
    suspect_re = re.compile("|".join(suspect_patterns))
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        if suspect_re.search(text) and "BaseModel" not in text:
            findings.append(
                _finding(
                    "PY-004",
                    "WARN",
                    "structural_conformance",
                    f"{rel}: accesses external data (JSON/CSV) without a Pydantic BaseModel.",
                    "Define a Pydantic model and validate external payloads through it.",
                )
            )
    return findings


def check_async_sqlalchemy(repo_path: Path) -> list[Finding]:
    """PY-015: SQLAlchemy uses async API."""
    CHECK_ID = "PY-015"
    findings: list[Finding] = []
    pyproject = repo_path / "pyproject.toml"
    pyp_text = pyproject.read_text() if pyproject.exists() else ""

    src = repo_path / "src"
    if not src.is_dir():
        return findings

    src_text = ""
    for py_file in src.rglob("*.py"):
        try:
            src_text += "\n" + py_file.read_text()
        except Exception:
            continue

    if "sqlalchemy" not in src_text.lower() and "sqlalchemy" not in pyp_text.lower():
        return findings  # Not a SQLAlchemy repo

    # Flag sync imports
    if (
        (
            "from sqlalchemy.orm import Session" in src_text
            or "from sqlalchemy.orm import sessionmaker" in src_text
        )
        and "AsyncSession" not in src_text
        and "async_sessionmaker" not in src_text
    ):
        findings.append(
            _finding(
                "PY-015",
                "ERROR",
                "structural_conformance",
                "Sync Session/sessionmaker imported without AsyncSession/async_sessionmaker counterpart.",
                "Use AsyncSession and async_sessionmaker from sqlalchemy.ext.asyncio.",
            )
        )
    # Flag sync create_engine
    if "create_engine(" in src_text and "create_async_engine(" not in src_text:
        findings.append(
            _finding(
                "PY-015",
                "ERROR",
                "structural_conformance",
                "Sync create_engine() used without create_async_engine() counterpart.",
                "Use create_async_engine from sqlalchemy.ext.asyncio.",
            )
        )
    # asyncpg required when sqlalchemy is present
    if "sqlalchemy" in pyp_text.lower() and "asyncpg" not in pyp_text.lower():
        findings.append(
            _finding(
                "PY-015",
                "ERROR",
                "structural_conformance",
                "sqlalchemy declared without asyncpg in pyproject.toml.",
                "Add asyncpg to dependencies for async PostgreSQL.",
            )
        )
    return findings


def check_settings_field_consistency(repo_path: Path) -> list[Finding]:
    """CFG-001: getattr(settings, X) / settings.X keys declared on Settings."""
    CHECK_ID = "CFG-001"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    declared_fields: set[str] = set()
    # First pass: collect fields on any Settings class
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not (node.name == "Settings" or node.name.endswith("Settings")):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    declared_fields.add(stmt.target.id)
                elif isinstance(stmt, ast.Assign):
                    for t in stmt.targets:
                        if isinstance(t, ast.Name):
                            declared_fields.add(t.id)

    if not declared_fields:
        return findings  # No Settings class; rule doesn't apply here

    # Second pass: find getattr(settings, "X") calls and settings.X access
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            # getattr(settings, "X")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "settings"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                key = node.args[1].value
                if key not in declared_fields:
                    findings.append(
                        _finding(
                            "CFG-001",
                            "WARN",
                            "configuration_consistency",
                            f"{rel}: getattr(settings, {key!r}) but {key} not declared on Settings.",
                            "Declare the field on Settings or remove the access.",
                        )
                    )
            # settings.KEY
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
                and node.attr not in declared_fields
                and not node.attr.startswith("_")
            ):
                findings.append(
                    _finding(
                        "CFG-001",
                        "WARN",
                        "configuration_consistency",
                        f"{rel}: settings.{node.attr} access but not declared on Settings.",
                        "Declare the field on Settings or remove the access.",
                    )
                )
    return findings


def check_env_example_settings_parity(repo_path: Path) -> list[Finding]:
    """CFG-002: .env.example keys match Settings declared fields."""
    CHECK_ID = "CFG-002"
    import ast

    findings: list[Finding] = []
    env_example = repo_path / ".env.example"
    if not env_example.exists():
        return findings
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    declared_fields: set[str] = set()
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not (node.name == "Settings" or node.name.endswith("Settings")):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    declared_fields.add(stmt.target.id)

    if not declared_fields:
        return findings

    env_text = env_example.read_text()
    lines = env_text.splitlines()
    prev_is_external_marker = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            # Check if this comment marks the next key as external tooling
            if "external" in stripped.lower() or "tooling" in stripped.lower():
                prev_is_external_marker = True
            continue
        if not stripped or "=" not in stripped:
            prev_is_external_marker = False
            continue
        key = stripped.split("=", 1)[0].strip()
        # Uppercase env vars correspond to Settings fields case-insensitively
        if key in declared_fields or key.upper() in (
            f.upper() for f in declared_fields
        ):
            prev_is_external_marker = False
            continue
        if prev_is_external_marker:
            prev_is_external_marker = False
            continue
        findings.append(
            _finding(
                "CFG-002",
                "WARN",
                "configuration_consistency",
                f".env.example key {key!r} not declared on Settings.",
                "Declare the field on Settings or mark the key with a comment noting "
                "'external tooling'.",
            )
        )
    return findings


def check_hardcoded_time_values(
    repo_path: Path, language: str = "python"
) -> list[Finding]:
    """TEST-013: No hardcoded numeric sleeps/timeouts/delays."""
    CHECK_ID = "TEST-013"
    import re

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    if language == "python":
        patterns = [
            re.compile(r"time\.sleep\(\s*(\d+(?:\.\d+)?)\s*\)"),
            re.compile(r"retry_delay_seconds\s*=\s*(\d+)"),
            re.compile(r"\btimeout\s*=\s*(\d+)"),
        ]
        exts = ("*.py",)
    else:
        patterns = [
            re.compile(r"setTimeout\(\s*\w+\s*,\s*(\d+)"),
            re.compile(r"setInterval\(\s*\w+\s*,\s*(\d+)"),
        ]
        exts = ("*.ts", "*.tsx")

    for ext in exts:
        for f in src.rglob(ext):
            try:
                text = f.read_text()
            except Exception:
                continue
            rel = f.relative_to(repo_path)
            for pat in patterns:
                for m in pat.finditer(text):
                    val = m.group(1)
                    if val == "0":
                        continue
                    # Skip if guarded by env/settings — we check if the line
                    # has "os.getenv", "settings.", or "process.env" nearby.
                    start = max(0, m.start() - 200)
                    context = text[start : m.end()]
                    if (
                        "os.getenv" in context
                        or "settings." in context
                        or "process.env" in context
                        or "PYTEST_CURRENT_TEST" in text
                    ):
                        continue
                    findings.append(
                        _finding(
                            "TEST-013",
                            "INFO",
                            "principles",
                            f"{rel}: hardcoded numeric value {val} in time/retry/timeout call.",
                            "Source from Settings or env var so tests can override with 0.",
                        )
                    )
                    break  # One finding per file is enough
    return findings


def check_testclient_for_v1_routes(repo_path: Path) -> list[Finding]:
    """TEST-008: /v1/ route tests use TestClient or AsyncClient."""
    CHECK_ID = "TEST-008"
    import re

    findings: list[Finding] = []
    tests_dir = repo_path / "tests"
    if not tests_dir.is_dir():
        return findings

    for test_file in tests_dir.rglob("test_*.py"):
        try:
            text = test_file.read_text()
        except Exception:
            continue
        # Skip if file uses TestClient/AsyncClient
        if "TestClient" in text or "AsyncClient" in text:
            continue
        # Look for test functions referencing /v1/
        for m in re.finditer(r"def (test_\w+)\([^)]*\):([\s\S]*?)(?=\ndef |\Z)", text):
            fn_name, body = m.group(1), m.group(2)
            if "/v1/" in body:
                rel = test_file.relative_to(repo_path)
                findings.append(
                    _finding(
                        "TEST-008",
                        "WARN",
                        "test_coverage",
                        f"{rel}::{fn_name}: references /v1/ without TestClient/AsyncClient.",
                        "Use fastapi.testclient.TestClient or httpx.AsyncClient for route tests.",
                    )
                )
    return findings


def check_db_test_fixtures(repo_path: Path) -> list[Finding]:
    """TEST-009: conftest has DB test fixtures."""
    CHECK_ID = "TEST-009"
    findings: list[Finding] = []
    src = repo_path / "src"
    tests = repo_path / "tests"
    if not src.is_dir() or not tests.is_dir():
        return findings

    # Is this a SQLAlchemy repo?
    has_sqlalchemy = False
    for py_file in src.rglob("*.py"):
        try:
            if "sqlalchemy" in py_file.read_text().lower():
                has_sqlalchemy = True
                break
        except Exception:
            continue
    if not has_sqlalchemy:
        return findings

    conftest_files = list(tests.rglob("conftest.py"))
    if not conftest_files:
        findings.append(
            _finding(
                "TEST-009",
                "ERROR",
                "test_coverage",
                "SQLAlchemy repo has no conftest.py with DB test fixtures.",
                "Add a conftest.py with DATABASE_URL override, in-memory engine, or "
                "transaction rollback fixture.",
            )
        )
        return findings

    combined = "\n".join(f.read_text() for f in conftest_files if f.exists())
    has_fixture_pattern = (
        "DATABASE_URL" in combined
        or "sqlite:///:memory:" in combined
        or ("rollback" in combined.lower() and "fixture" in combined.lower())
    )
    if not has_fixture_pattern:
        findings.append(
            _finding(
                "TEST-009",
                "ERROR",
                "test_coverage",
                "conftest.py has no DB test fixture pattern (DATABASE_URL override, in-memory SQLite, or rollback fixture).",
                "Add one of: DATABASE_URL override, in-memory SQLite engine, or rollback fixture.",
            )
        )
    return findings


def check_route_contract_tests(repo_path: Path) -> list[Finding]:
    """TEST-010: Each FastAPI route has a contract test."""
    CHECK_ID = "TEST-010"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    tests = repo_path / "tests"
    if not src.is_dir() or not tests.is_dir():
        return findings

    route_paths: set[str] = set()
    route_attrs = {"get", "post", "put", "delete", "patch"}
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in route_attrs:
                    continue
                if (
                    dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)
                ):
                    route_paths.add(dec.args[0].value)

    if not route_paths:
        return findings

    test_text = ""
    for test_file in tests.rglob("test_*.py"):
        try:
            test_text += "\n" + test_file.read_text()
        except Exception:
            continue

    untested = [r for r in route_paths if r not in test_text]
    if untested:
        sample = ", ".join(sorted(untested)[:5])
        suffix = " (and others)" if len(untested) > 5 else ""
        findings.append(
            _finding(
                "TEST-010",
                "ERROR",
                "test_coverage",
                f"{len(untested)} route(s) have no contract test referencing them: {sample}{suffix}.",
                "Add tests that exercise each /v1/ route and assert the response shape.",
            )
        )
    return findings


def check_mock_assertions(repo_path: Path) -> list[Finding]:
    """TEST-011: Mocks have corresponding assertions."""
    CHECK_ID = "TEST-011"
    import re

    findings: list[Finding] = []
    tests = repo_path / "tests"
    if not tests.is_dir():
        return findings

    for test_file in tests.rglob("test_*.py"):
        try:
            text = test_file.read_text()
        except Exception:
            continue
        rel = test_file.relative_to(repo_path)
        # Split into test functions
        for m in re.finditer(r"def (test_\w+)\([^)]*\):([\s\S]*?)(?=\ndef |\Z)", text):
            fn_name, body = m.group(1), m.group(2)
            creates_mock = bool(
                re.search(r"\b(MagicMock|AsyncMock|patch|mock_\w+)\b", body)
            )
            if not creates_mock:
                continue
            # Build pattern without mock assert_* literals — pygrep-hooks
            # python-check-mock-methods flags those sequences in non-mock contexts.
            _assert_prefix = chr(97) + "ssert_"
            _mock_assert_re = re.compile(
                "|".join(
                    rf"\.{_assert_prefix}{tail}"
                    for tail in (
                        "called",
                        "called_once",
                        "called_with",
                        "called_once_with",
                    )
                )
            )
            has_assert = bool(_mock_assert_re.search(body))
            if not has_assert:
                findings.append(
                    _finding(
                        "TEST-011",
                        "ERROR",
                        "test_coverage",
                        f"{rel}::{fn_name}: creates mocks but has no assert_called* assertion.",
                        "Add assert_called_once_with(...) or equivalent to verify the mock "
                        "was exercised as expected.",
                    )
                )
    return findings


def check_test_gap_critical_paths(repo_path: Path) -> list[Finding]:
    """TEST-GAP-001: Track presence of TEST-001..004 critical-path tests."""
    CHECK_ID = "TEST-GAP-001"
    findings: list[Finding] = []
    tests = repo_path / "tests"
    if not tests.is_dir():
        return findings

    test_text = ""
    for test_file in tests.rglob("test_*.py"):
        try:
            test_text += "\n" + test_file.read_text()
        except Exception:
            continue

    # Heuristic markers for each critical-path category
    critical_markers = {
        "TEST-001 (normalization)": ("normalize", "normalise", "normalization"),
        "TEST-002 (deduplication)": ("dedup", "deduplication"),
        "TEST-003 (persistence)": ("persist", "upsert", "session.commit"),
        "TEST-004 (archival)": ("archive", "archival", "move"),
    }
    missing = []
    for label, markers in critical_markers.items():
        if not any(m in test_text.lower() for m in markers):
            missing.append(label)

    if missing:
        findings.append(
            _finding(
                "TEST-GAP-001",
                "INFO",
                "test_coverage",
                f"Missing critical-path tests: {', '.join(missing)}.",
                "Add tests for the missing categories so each pipeline stage has coverage.",
            )
        )
    return findings


def check_retry_logic(repo_path: Path) -> list[Finding]:
    """PIPE-007: Retry logic on external API calls."""
    CHECK_ID = "PIPE-007"
    import ast

    findings = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        text = py_file.read_text()
        if "DriveFacade" in text or "LLMClient" in text:
            continue
        try:
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            task_decorator = None
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Name) and dec.id == "task") or (
                    isinstance(dec, ast.Call)
                    and (
                        (isinstance(dec.func, ast.Name) and dec.func.id == "task")
                        or (
                            isinstance(dec.func, ast.Attribute)
                            and dec.func.attr == "task"
                        )
                    )
                ):
                    task_decorator = dec
            if task_decorator is None:
                continue

            body_text = ast.get_source_segment(text, node) or ""
            external_signal = any(
                s in body_text for s in ("httpx.", "anthropic", "drive", "sheets")
            )
            if not external_signal:
                continue

            has_retries = isinstance(task_decorator, ast.Call) and any(
                kw.arg == "retries" for kw in task_decorator.keywords
            )
            if not has_retries:
                findings.append(
                    _finding(
                        "PIPE-007",
                        "WARN",
                        "pipeline_consistency",
                        f"External-calling task missing retries= in {py_file.relative_to(repo_path)}::{node.name}.",
                        "Add retries= to @task decorators that call external APIs.",
                    )
                )
    return findings


def check_no_retired_trigger_patterns(repo_path: Path) -> list[Finding]:
    """PIPE-008: Narrowed retired GitHub / GAS / gh CLI trigger patterns (2026-04).

    Fires only when: (1) a workflow uses ``repository_dispatch`` together with
    app-invoking steps, (2) Python/JS source actively POSTs to GitHub
    ``/dispatches``, (3) the retired ``google-app-script-trigger`` string appears,
    or (4) ``gh workflow run`` is invoked (shell or argv list form). Bare URL
    literals are intentionally ignored — see LLM rule PIPE-014 for consistency
    reasoning across input types.
    """
    CHECK_ID = "PIPE-008"
    findings: list[Finding] = []
    wf_dir = repo_path / ".github" / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.rglob("*.yml")) + sorted(wf_dir.rglob("*.yaml")):
            try:
                text = wf.read_text()
            except OSError:
                continue
            low = text.lower()
            if "repository_dispatch" not in low:
                continue
            relay = any(
                k in low
                for k in (
                    "prefect deployment run",
                    "prefect deploy",
                    "run_deployment(",
                    "npx prefect",
                )
            ) or (
                "/dispatches" in text
                and any(k in low for k in ("curl ", "httpx.", "requests."))
            )
            pure_ci = ("pytest" in low or "ruff" in low) and not relay
            if relay and not pure_ci:
                findings.append(
                    _finding(
                        "PIPE-008",
                        "WARN",
                        "structural_conformance",
                        f"repository_dispatch in GitHub workflow with app-triggering steps ({wf.relative_to(repo_path)}).",
                        "Use watcher-cog + Prefect instead of GHA repository_dispatch relays.",
                    )
                )

    code_exts = {".py", ".ts", ".tsx", ".js"}
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        if "tests/" in str(path).replace("\\", "/"):
            continue
        if path.suffix.lower() not in code_exts:
            continue
        if ".github/workflows/" in str(path).replace("\\", "/"):
            continue
        try:
            body = path.read_text()
        except OSError:
            continue
        low = body.lower()
        if re.search(
            r"(httpx|requests)\.(post|put)\([^\)]*api\.github\.com/[^\"'\)]+/dispatches",
            body,
            re.I,
        ):
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"Active HTTP client call to GitHub dispatches API ({path.relative_to(repo_path)}).",
                    "Use watcher-cog + Prefect instead of repository_dispatch HTTP relays.",
                )
            )
        if "google-app-script-trigger" in body:
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"Retired google-app-script-trigger reference in {path.relative_to(repo_path)}.",
                    "Use watcher-cog + Prefect; remove legacy Apps Script trigger hooks.",
                )
            )
        if re.search(r"\bgh\s+workflow\s+run\b", low):
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"gh workflow run invocation in {path.relative_to(repo_path)}.",
                    "Use watcher-cog + Prefect instead of driving workflows via gh CLI.",
                )
            )
        if re.search(
            r"\[\s*['\"]gh['\"]\s*,\s*['\"]workflow['\"]\s*,\s*['\"]run['\"]",
            body,
        ):
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"gh workflow run argv-style invocation in {path.relative_to(repo_path)}.",
                    "Use watcher-cog + Prefect instead of subprocess gh workflow relays.",
                )
            )
    return findings


def check_evaluation_step(repo_path: Path) -> list[Finding]:
    """PIPE-009: AI evaluation step as final pipeline task."""
    CHECK_ID = "PIPE-009"
    findings = []
    if repo_path.name == "evaluator-cog":
        return findings
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    text = "\n".join(f.read_text() for f in src.rglob("*.py"))
    signals = (
        "pipeline_eval",
        "post_evaluation",
        "/v1/evaluations",
        "evaluate_pipeline_run",
    )
    if not any(s in text for s in signals):
        findings.append(
            _finding(
                "PIPE-009",
                "WARN",
                "pipeline_consistency",
                "Pipeline cog source has no clear evaluation-step signal.",
                "Add or document an evaluation step that posts findings to /v1/evaluations.",
            )
        )
    return findings


def check_respx_for_http_mocking(repo_path: Path) -> list[Finding]:
    """TEST-007: respx/httpx for HTTP mocking — no real external calls."""
    CHECK_ID = "TEST-007"
    findings = []
    pyproject = repo_path / "pyproject.toml"
    py_text = pyproject.read_text().lower() if pyproject.exists() else ""
    if "respx" not in py_text:
        findings.append(
            _finding(
                "TEST-007",
                "ERROR",
                "testing_coverage",
                "respx is absent from development dependencies.",
                "Add respx to dev dependencies for HTTP mocking in tests.",
            )
        )

    tests_dir = repo_path / "tests"
    if not tests_dir.is_dir():
        return findings
    for test_file in tests_dir.rglob("test_*.py"):
        text = test_file.read_text()
        has_raw_http = any(
            token in text
            for token in (
                "httpx.get(",
                "httpx.post(",
                "requests.get(",
                "requests.post(",
            )
        )
        if has_raw_http and "respx.mock" not in text:
            findings.append(
                _finding(
                    "TEST-007",
                    "ERROR",
                    "testing_coverage",
                    f"Raw HTTP calls found without respx.mock in {test_file.relative_to(repo_path)}.",
                    "Wrap HTTP interactions in respx.mock() and avoid real external network calls.",
                )
            )
            break
    return findings


def check_mypy_in_ci(repo_path: Path) -> list[Finding]:
    """TEST-012: mypy must run in CI if [tool.mypy] is declared."""
    CHECK_ID = "TEST-012"
    findings = []
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        return findings
    content = pyproject.read_text()
    if "[tool.mypy]" not in content:
        return findings

    workflows = repo_path / ".github" / "workflows"
    combined = ""
    if workflows.is_dir():
        for yml in list(workflows.rglob("*.yml")) + list(workflows.rglob("*.yaml")):
            combined += "\n" + yml.read_text().lower()
    if "mypy" not in combined:
        findings.append(
            _finding(
                "TEST-012",
                "WARN",
                "testing_coverage",
                "[tool.mypy] is configured but mypy is not run in CI workflows.",
                "Add a mypy step to CI when [tool.mypy] is present.",
            )
        )
    return findings


def check_shared_library_used(
    repo_path: Path,
    language: str = "python",
    workspace_package_json_text: str | None = None,
) -> list[Finding]:
    """XSTACK-001: Shared library dependency must be declared (2026-04 narrow).

    Hand-rolled logger/auth/response reimplementation heuristics moved to LLM
    rule XSTACK-005; this check only verifies the dependency is present in
    ``pyproject.toml`` / workspace ``package.json`` (MONO-001).
    """
    CHECK_ID = "XSTACK-001"
    findings: list[Finding] = []
    if language == "python":
        pyproject = repo_path / "pyproject.toml"
        py_text = pyproject.read_text().lower() if pyproject.exists() else ""
        if "common-python-utils" not in py_text:
            findings.append(
                _finding(
                    "XSTACK-001",
                    "ERROR",
                    "cross_repo_coherence",
                    "common-python-utils is not declared for this Python service.",
                    "Depend on common-python-utils and consume shared behaviors from it.",
                )
            )
    else:
        pkg = repo_path / "package.json"
        per_app_text = pkg.read_text().lower() if pkg.exists() else ""
        pkg_text = per_app_text + (workspace_package_json_text or "").lower()
        if "common-typescript-utils" not in pkg_text:
            findings.append(
                _finding(
                    "XSTACK-001",
                    "ERROR",
                    "cross_repo_coherence",
                    "common-typescript-utils is not declared for this TypeScript service.",
                    "Depend on common-typescript-utils to avoid re-implementing shared utilities.",
                )
            )
    return findings


def check_standards_freshness(repo_path: Path) -> list[Finding]:
    """PRIN-009: Standards are a living document.

    Checks the timestamp of the most recent commit on main in the
    ecosystem-standards repo via the GitHub API. Flags if more than
    90 days have elapsed. Degrades gracefully on fetch failure
    (rate limit, network, etc.) — returns empty rather than a false
    positive, matching the pre-existing behavior of this check.
    """
    CHECK_ID = "PRIN-009"
    import datetime

    findings: list[Finding] = []
    try:
        import httpx

        url = "https://api.github.com/repos/mini-app-polis/ecosystem-standards/commits/main"
        r = httpx.get(
            url,
            timeout=20.0,
            headers={"Accept": "application/vnd.github+json"},
        )
        r.raise_for_status()
        data = r.json() or {}
        committer = (data.get("commit") or {}).get("committer") or {}
        date_str = str(committer.get("date") or "").strip()
        if not date_str:
            return findings

        # GitHub returns ISO-8601 with trailing 'Z' (e.g. "2026-04-18T14:23:01Z").
        # Python's fromisoformat handles 'Z' natively as of 3.11.
        try:
            commit_dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return findings

        now = datetime.datetime.now(datetime.UTC)
        age_days = (now - commit_dt).days
        if age_days > 90:
            findings.append(
                _finding(
                    "PRIN-009",
                    "WARN",
                    "standards_currency",
                    f"Standards repo appears stale ({age_days} days since last commit).",
                    "Review ecosystem-standards — if no recent changes are warranted, commit an explicit review-attestation entry.",
                )
            )
    except Exception:
        return findings
    return findings


# -- META checks (standards-repo type only) -----------------------------------


def check_meta_release_pipeline_wired(repo_path: Path) -> list[Finding]:
    """META-001: Release automation for the standards repo is wired end-to-end."""
    CHECK_ID = "META-001"
    findings: list[Finding] = []
    workflows_dir = repo_path / ".github" / "workflows"
    workflow_blob = ""
    if workflows_dir.is_dir():
        for wf in list(workflows_dir.rglob("*.yml")) + list(
            workflows_dir.rglob("*.yaml")
        ):
            try:
                workflow_blob += "\n" + wf.read_text().lower()
            except OSError:
                continue

    has_sem_rel_wf = any(
        s in workflow_blob
        for s in (
            "semantic-release",
            "npx semantic-release",
            "semantic_release",
        )
    )
    has_rel_hook = (
        (repo_path / ".releaserc.json").exists()
        or (repo_path / ".releaserc.cjs").exists()
        or (repo_path / ".releaserc.yaml").exists()
    )
    pkg = repo_path / "package.json"
    pkg_ok = False
    if pkg.exists():
        try:
            import json as _json

            pdata = _json.loads(pkg.read_text())
            scripts = pdata.get("scripts") or {}
            dev = pdata.get("devDependencies") or {}
            deps = pdata.get("dependencies") or {}
            scripts_blob = str(scripts).lower()
            pkg_ok = "semantic-release" in scripts_blob or any(
                "semantic-release" in str(k).lower() for k in {**dev, **deps}
            )
        except Exception:
            pkg_ok = False

    push_to_main = "push:" in workflow_blob and "main" in workflow_blob

    if not has_sem_rel_wf:
        findings.append(
            _finding(
                "META-001",
                "WARN",
                "structural_conformance",
                "No GitHub Actions workflow references semantic-release.",
                "Add a workflow that executes semantic-release on the mainline branch.",
            )
        )
    if not has_rel_hook:
        findings.append(
            _finding(
                "META-001",
                "WARN",
                "structural_conformance",
                "Missing .releaserc.* configuration alongside semantic-release.",
                "Add .releaserc.json (or .releaserc.cjs / .yaml) describing branches and plugins.",
            )
        )
    if not pkg_ok:
        findings.append(
            _finding(
                "META-001",
                "WARN",
                "structural_conformance",
                "package.json lacks semantic-release wiring (script or dependency).",
                "Declare semantic-release in devDependencies and expose an npm script if required by the catalog.",
            )
        )
    if not push_to_main:
        findings.append(
            _finding(
                "META-001",
                "WARN",
                "structural_conformance",
                "No workflow appears to trigger on push to main.",
                "Ensure release automation runs when main updates (push trigger with main branch).",
            )
        )
    return findings


def check_meta_no_scattered_metadata(repo_path: Path) -> list[Finding]:
    """META-002: Version metadata is not scattered outside canonical files."""
    CHECK_ID = "META-002"
    findings: list[Finding] = []
    index_path = repo_path / "index.yaml"
    if index_path.exists():
        try:
            text = index_path.read_text()
            if re.search(r"(?m)^version\s*:", text):
                findings.append(
                    _finding(
                        "META-002",
                        "WARN",
                        "structural_conformance",
                        "index.yaml still declares a top-level version: field.",
                        "Remove version from index.yaml — package.json is the single version of record.",
                    )
                )
            if re.search(r"(?m)^updated\s*:", text):
                findings.append(
                    _finding(
                        "META-002",
                        "WARN",
                        "structural_conformance",
                        "index.yaml still declares a top-level updated: field.",
                        "Remove updated metadata from index.yaml; rely on git history and package.json.",
                    )
                )
        except OSError as exc:
            findings.append(
                _finding(
                    "META-002",
                    "WARN",
                    "structural_conformance",
                    f"index.yaml could not be read: {exc}",
                    "Fix permissions/encoding so META-002 can scan for scattered metadata.",
                )
            )

    for stray in ("VERSION.txt", "VERSION", "version.txt"):
        candidate = repo_path / stray
        if candidate.is_file():
            findings.append(
                _finding(
                    "META-002",
                    "WARN",
                    "structural_conformance",
                    f"Stray plaintext version file exists at repo root ({stray}).",
                    "Delete ad-hoc version files — package.json must remain canonical.",
                )
            )
            break
    return findings


_CANONICAL_ENUM_KEYS = (
    "repo_types",
    "traits",
    "dod_types",
    "service_statuses",
    "rule_severities",
)


def check_meta_canonical_enums_are_dicts(repo_path: Path) -> list[Finding]:
    """META-003: Schema enumerations are dict maps, not YAML lists."""
    CHECK_ID = "META-003"
    findings: list[Finding] = []
    index_path = repo_path / "index.yaml"
    if not index_path.exists():
        return findings
    try:
        import yaml as _yaml

        data = _yaml.safe_load(index_path.read_text()) or {}
    except Exception:
        findings.append(
            _finding(
                "META-003",
                "WARN",
                "structural_conformance",
                "index.yaml is not parseable YAML — cannot validate canonical enum dict shapes.",
                "Fix YAML syntax errors reported by the standards CI job.",
            )
        )
        return findings

    schema = data.get("schema") or {}
    for key in _CANONICAL_ENUM_KEYS:
        val = schema.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            findings.append(
                _finding(
                    "META-003",
                    "WARN",
                    "structural_conformance",
                    f"schema.{key} is a YAML list — canonical enums must be dict maps.",
                    "Convert the enumeration to a mapping keyed by stable identifiers.",
                )
            )
    return findings


# -- Test checks --------------------------------------------------------------


def check_pipeline_cog_tests(
    repo_path: Path,
    exceptions: frozenset[str] | None = None,
) -> list[Finding]:
    """TEST-001, TEST-002, TEST-004: pipeline cog critical path tests."""
    CHECK_ID = "TEST-001"
    _exc = exceptions or frozenset()
    findings = []
    tests_dir = repo_path / "tests"
    if not tests_dir.is_dir():
        return findings
    test_files = list(tests_dir.rglob("test_*.py"))
    all_content = "\n".join(f.read_text() for f in test_files)
    checks = [
        (
            "TEST-001",
            "WARN",
            "testing_coverage",
            "normalization",
            "No normalization test found.",
            "Add a test covering normalization or cleaning logic.",
        ),
        (
            "TEST-002",
            "WARN",
            "testing_coverage",
            "dedup",
            "No deduplication test found.",
            "Add a test covering duplicate detection logic.",
        ),
        (
            "TEST-004",
            "WARN",
            "testing_coverage",
            "shape",
            "No output shape test found.",
            "Add a test asserting output structure or schema.",
        ),
    ]
    for rule_id, severity, dimension, keyword, finding_text, suggestion in checks:
        if rule_id not in _exc and keyword not in all_content.lower():
            findings.append(
                _finding(rule_id, severity, dimension, finding_text, suggestion)
            )
    return findings


# Substrings in test sources that indicate a failure-path test (TEST-003).
FAILURE_PATH_SIGNALS = (
    "malform",
    "failure",
    "fail",
    "error",
    "exception",
    "invalid",
    "raises",
    "bad_input",
    "boom",
    "continues",
    "loop_continues",
    "does_not_abort",
    "does_not_raise",
)


def check_test_structure(
    repo_path: Path,
    exceptions: frozenset[str] | None = None,
) -> list[Finding]:
    """TEST-003, TEST-005: test directory checks."""
    CHECK_ID = "TEST-003"
    _exc = exceptions or frozenset()
    findings = []
    tests_dir = repo_path / "tests"
    if not tests_dir.is_dir():
        if "TEST-003" not in _exc:
            findings.append(
                _finding(
                    "TEST-003",
                    "ERROR",
                    "testing_coverage",
                    "tests/ directory is absent.",
                    "Add a tests/ directory with critical path tests.",
                )
            )
        return findings

    test_files = list(tests_dir.rglob("test_*.py"))
    all_content = "\n".join(f.read_text() for f in test_files)

    lowered = all_content.lower()
    has_failure_path = any(signal in lowered for signal in FAILURE_PATH_SIGNALS)

    if "TEST-003" not in _exc and not has_failure_path:
        findings.append(
            _finding(
                "TEST-003",
                "ERROR",
                "testing_coverage",
                "No failure path test found.",
                "Add a failure-path test (e.g. invalid input or simulated error) asserting the cog handles it and continues.",
            )
        )

    pyproject = repo_path / "pyproject.toml"
    if (
        "TEST-005" not in _exc
        and pyproject.exists()
        and "[tool.pytest.ini_options]" not in pyproject.read_text()
    ):
        findings.append(
            _finding(
                "TEST-005",
                "WARN",
                "testing_coverage",
                "[tool.pytest.ini_options] absent from pyproject.toml.",
                "Add pytest configuration to pyproject.toml.",
            )
        )

    return findings


def check_prefect_serve_pattern(repo_path: Path) -> list[Finding]:
    """CD-015: Prefect serve() — no work pool."""
    CHECK_ID = "CD-015"
    findings = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    content = "\n".join(f.read_text() for f in src.rglob("*.py"))
    if "flow.deploy(" in content or "work_pool_name" in content:
        findings.append(
            _finding(
                "CD-015",
                "ERROR",
                "cd_readiness",
                "work pool pattern detected — flow.deploy() or work_pool_name found.",
                "Use prefect.serve() running in-process on Railway instead of work pool deployments.",
            )
        )
    prefect_yaml = repo_path / "prefect.yaml"
    if prefect_yaml.exists() and "work_pool" in prefect_yaml.read_text():
        findings.append(
            _finding(
                "CD-015",
                "ERROR",
                "cd_readiness",
                "work_pool configuration found in prefect.yaml.",
                "Remove work pool config and use prefect.serve() instead.",
            )
        )
    if "prefect.serve(" not in content and "flow.serve(" not in content:
        findings.append(
            _finding(
                "CD-015",
                "WARN",
                "cd_readiness",
                "No prefect.serve() call found in source — flow registration pattern missing or unverifiable.",
                "Ensure flows are registered via prefect.serve() at the cog entry point.",
            )
        )
    return findings


def check_releaserc_assets(
    repo_path: Path, monorepo_root: Path | None = None
) -> list[Finding]:
    """VER-008: .releaserc.json assets must include all version-managed files."""
    CHECK_ID = "VER-008"
    import json as _json

    findings = []
    releaserc = repo_path / ".releaserc.json"
    if not releaserc.exists() and monorepo_root:
        releaserc = monorepo_root / ".releaserc.json"
    if not releaserc.exists():
        return findings
    try:
        data = _json.loads(releaserc.read_text())
    except Exception:
        return findings

    plugins = data.get("plugins", [])
    prepare_cmd = ""
    git_assets: list[str] = []

    for plugin in plugins:
        if isinstance(plugin, list) and len(plugin) >= 2:
            name, config = plugin[0], plugin[1]
            if "@semantic-release/exec" in str(name):
                prepare_cmd = config.get("prepareCmd", "")
            if "@semantic-release/git" in str(name):
                git_assets = config.get("assets", [])

    # Detect files written by prepareCmd
    managed_files = []
    for candidate in ("pyproject.toml", "package.json", "index.yaml"):
        if candidate in prepare_cmd:
            managed_files.append(candidate)

    if "CHANGELOG.md" not in git_assets:
        findings.append(
            _finding(
                "VER-008",
                "ERROR",
                "cd_readiness",
                "CHANGELOG.md is absent from @semantic-release/git assets.",
                "Add CHANGELOG.md to the assets array in the @semantic-release/git plugin config.",
            )
        )

    for f in managed_files:
        if f not in git_assets:
            findings.append(
                _finding(
                    "VER-008",
                    "ERROR",
                    "cd_readiness",
                    f"{f} is written by prepareCmd but absent from @semantic-release/git assets.",
                    f"Add {f} to the assets array in the @semantic-release/git plugin config.",
                )
            )
    return findings


def check_pnpm_lockfile(
    repo_path: Path,
    monorepo_root: Path | None = None,
) -> list[Finding]:
    """XSTACK-003: pnpm for all TypeScript projects."""
    CHECK_ID = "XSTACK-003"
    findings = []
    check_root = monorepo_root or repo_path
    if (check_root / "package-lock.json").exists():
        findings.append(
            _finding(
                "XSTACK-003",
                "ERROR",
                "structural_conformance",
                "package-lock.json found — npm is not the approved package manager for TypeScript projects.",
                "Migrate to pnpm: remove package-lock.json, run pnpm install, commit pnpm-lock.yaml.",
            )
        )
    if (check_root / "yarn.lock").exists():
        findings.append(
            _finding(
                "XSTACK-003",
                "ERROR",
                "structural_conformance",
                "yarn.lock found — yarn is not the approved package manager for TypeScript projects.",
                "Migrate to pnpm: remove yarn.lock, run pnpm install, commit pnpm-lock.yaml.",
            )
        )
    if not (check_root / "pnpm-lock.yaml").exists():
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


def _type_to_dod(repo_type: str, language: str = "python") -> str | None:
    """Map new repo type taxonomy back to dod_type string for check_readme_running_locally."""
    mapping = {
        "pipeline-cog": "new_cog",
        "trigger-cog": "new_cog",
        "evaluator-service": "new_cog",
        "api-service": "new_fastapi_service"
        if language == "python"
        else "new_hono_service",
        # Libraries have no standardized "running locally" section like cogs — avoid
        # routing through the Python cog README path (uv sync, pytest, etc.).
        "shared-library": None,
        "static-site": "new_frontend_site",
        "react-app": "new_react_app",
        "standards-repo": None,
    }
    return mapping.get(repo_type)


# -- Runner -------------------------------------------------------------------


def run_all_checks(
    repo_path: Path,
    language: str = "python",
    service_type: str = "worker",
    cog_subtype: str | None = None,
    dod_type: str | None = None,
    check_exceptions: list[str] | None = None,
    exception_reasons: dict[str, str] | None = None,
    monorepo_root: Path | None = None,
    workspace_package_json_text: str | None = None,
    evaluator_config: EvaluatorConfig | None = None,
) -> CheckResult:
    """Run deterministic checks against a repo and return combined findings.

    When evaluator_config is provided (from the repo's evaluator.yaml), it
    takes precedence over the legacy dod_type/service_type/check_exceptions
    parameters for type-based branching and exception scoping.
    """
    # ── Resolve type-based flags ─────────────────────────────────────────────
    # Prefer evaluator_config (from evaluator.yaml) over legacy dod_type fields.
    if evaluator_config is not None:
        cfg = evaluator_config
        # Type says "could be Python" (e.g. shared-library); ecosystem language is authoritative.
        is_python = (language == "python") and cfg.is_python_service
        is_library = cfg.is_shared_library
        # Use the broader pipeline-style predicate so evaluator-service inherits
        # the same PIPE-* / CD-015 / pipeline-cog-shaped tests as pipeline-cog.
        is_pipeline_cog = cfg.is_pipeline_style
        is_fastapi = cfg.is_api_service and language == "python"
        # Language-agnostic api-service flag — for rules that apply to both
        # FastAPI (Python) and Hono (TypeScript) API services.
        is_api_service = cfg.is_api_service
        is_frontend = cfg.is_frontend
        _exceptions = cfg.all_skipped_ids
        _exception_reasons = {**cfg.exemption_reasons}
        # Add deferral reasons with a deferral marker prefix for finding output
        for rule_id, reason in cfg.deferral_reasons.items():
            if rule_id not in _exception_reasons:
                _exception_reasons[rule_id] = f"[DEFERRED] {reason}"
        # Deferrals: still run the check but mark findings as deferred
        _deferred_ids = frozenset(cfg.deferral_ids)
    else:
        # Legacy path — used during migration when evaluator.yaml is absent
        is_python = language == "python" or dod_type in (
            "new_cog",
            "new_fastapi_service",
        )
        is_library = service_type == "library" or dod_type is None
        is_pipeline_cog = (dod_type == "new_cog" and cog_subtype != "trigger") or (
            is_python and service_type == "worker" and cog_subtype == "pipeline"
        )
        is_fastapi = dod_type == "new_fastapi_service"
        is_api_service = dod_type in ("new_fastapi_service", "new_hono_service")
        is_frontend = dod_type in ("new_frontend_site", "new_react_app")
        _exceptions = frozenset(check_exceptions or [])
        _exception_reasons = exception_reasons or {}
        _deferred_ids: frozenset[str] = frozenset()

    # Also handle cog_subtype trigger for trigger-cog type
    is_trigger_cog = (
        evaluator_config is not None and evaluator_config.is_trigger_cog
    ) or cog_subtype == "trigger"

    checked_rule_ids: set[str] = set()

    def _mark_checked(*rule_ids: str) -> None:
        checked_rule_ids.update(rule_ids)

    def _run(check_fn, rule_id: str | None = None) -> None:
        if rule_id:
            checked_rule_ids.add(rule_id)
        if rule_id and rule_id in _exceptions:
            reason = _exception_reasons.get(rule_id, "")
            if reason:
                findings.append(
                    _finding(
                        rule_id,
                        "INFO",
                        "structural_conformance",
                        f"Skipped: {reason}",
                        "",
                    )
                )
            return
        try:
            new_findings = check_fn(repo_path)
            # If rule is deferred, downgrade severity to INFO
            if rule_id and rule_id in _deferred_ids:
                for f in new_findings:
                    f["severity"] = "INFO"
                    f["deferred"] = True
            findings.extend(new_findings)
        except Exception as exc:
            findings.append(
                _finding(
                    "CHECKER",
                    "WARN",
                    "structural_conformance",
                    f"Check {check_fn.__name__} raised an unexpected error: {exc}",
                    "Investigate the checker itself.",
                )
            )

    findings: list[Finding] = []

    _run(lambda p: check_readme(p, monorepo_root=monorepo_root), "DOC-001")
    _run(lambda p: check_readme_io(p, monorepo_root=monorepo_root), "DOC-002")
    _run(lambda p: check_changelog(p, monorepo_root=monorepo_root), "DOC-003")
    _run(lambda p: check_releaserc(p, monorepo_root=monorepo_root), "VER-003")
    _run(check_no_dead_code, "DOC-008")
    _run(check_split_package_identity, "DOC-009")

    if not is_library:
        _run(lambda p: check_env_example(p, monorepo_root=monorepo_root), "DOC-004")

    if is_python and not is_frontend:
        _run(check_pre_commit, "PY-008")
        _run(check_src_layout, "PY-005")
        _run(check_no_setup_py, "PY-007")
        _mark_checked("PY-001", "PY-002", "PY-003", "PY-009", "PY-010", "CD-002")
        try:
            findings.extend(check_pyproject(repo_path, exceptions=_exceptions))
        except Exception as exc:
            findings.append(
                _finding(
                    "CHECKER",
                    "WARN",
                    "structural_conformance",
                    f"check_pyproject raised an unexpected error: {exc}",
                    "",
                )
            )
        _run(check_no_print_statements, "CD-003")
        _run(check_naming_conventions, "PY-011")
        _run(check_failed_prefix, "PY-012")
        _run(check_duplicate_prefix, "PY-013")
        _run(check_finally_cleanup, "PY-014")

    if (is_python or is_fastapi) and not is_library and not is_frontend:
        _run(check_common_python_utils_dep, "PY-006")

    # Healthchecks only applies to trigger cogs
    _mark_checked("CD-007")
    if is_trigger_cog:
        findings.extend(
            check_healthchecks_integration(repo_path, cog_subtype="trigger")
        )

    _run(check_structured_logging, "CD-009")
    _run(check_no_hardcoded_secrets, "CD-011")
    _run(check_no_manual_changelog, "VER-004")

    _mark_checked("XSTACK-001")
    if "XSTACK-001" not in _exceptions:
        # Static sites are excluded from XSTACK-001 by type scoping
        if not is_frontend or (
            evaluator_config is not None and not evaluator_config.is_static_site
        ):
            findings.extend(
                check_shared_library_used(
                    repo_path,
                    language=language,
                    workspace_package_json_text=workspace_package_json_text,
                )
            )
    else:
        reason = _exception_reasons.get("XSTACK-001", "")
        if reason:
            findings.append(
                _finding(
                    "XSTACK-001",
                    "INFO",
                    "structural_conformance",
                    f"Skipped: {reason}",
                    "",
                )
            )

    # Standards freshness check only applies to standards-repo type
    if evaluator_config is not None:
        if evaluator_config.is_standards_repo:
            _run(check_standards_freshness, "PRIN-009")
            _run(check_meta_release_pipeline_wired, "META-001")
            _run(check_meta_no_scattered_metadata, "META-002")
            _run(check_meta_canonical_enums_are_dicts, "META-003")
    elif dod_type is None:
        _run(check_standards_freshness, "PRIN-009")

    _run(check_no_hardcoded_urls, "FE-007")

    _mark_checked("VER-003", "VER-005", "VER-006")
    try:
        findings.extend(
            check_ci(
                repo_path,
                exceptions=_exceptions,
                monorepo_root=monorepo_root,
            )
        )
    except Exception as exc:
        findings.append(
            _finding(
                "CHECKER",
                "WARN",
                "structural_conformance",
                f"check_ci raised an unexpected error: {exc}",
                "",
            )
        )

    if is_python:
        _run(check_pytest_coverage_in_ci, "TEST-006")
        _run(check_respx_for_http_mocking, "TEST-007")
        _run(check_mypy_in_ci, "TEST-012")

    if is_python:
        _mark_checked("TEST-003", "TEST-005")
        try:
            findings.extend(check_test_structure(repo_path, exceptions=_exceptions))
        except Exception as exc:
            findings.append(
                _finding(
                    "CHECKER",
                    "WARN",
                    "structural_conformance",
                    f"check_test_structure raised an unexpected error: {exc}",
                    "",
                )
            )

    # DOC-013 README running locally — use new type for dod_type hint
    if "DOC-013" not in _exceptions:
        _mark_checked("DOC-013")
        # Map type to dod_type string for check_readme_running_locally
        if evaluator_config is not None:
            _readme_dod = _type_to_dod(evaluator_config.repo_type, language)
        else:
            _readme_dod = dod_type
        findings.extend(check_readme_running_locally(repo_path, dod_type=_readme_dod))

    if is_frontend:
        _run(check_tailwind, "FE-003")
    # Static site specific
    if evaluator_config is not None:
        if evaluator_config.is_static_site:
            _run(check_astro_framework, "FE-001")
            _run(check_astro_pinned_versions, "FE-008")
            _run(check_astro_build_time_data, "FE-009")
            _run(check_astro_runtime_queries, "FE-010")
        elif evaluator_config.is_react_app:
            _run(check_vite_react_ts, "FE-002")
            _run(check_shadcn, "FE-004")
            _run(check_react_hook_form_zod, "FE-005")
    else:
        if dod_type == "new_frontend_site":
            _run(check_astro_framework, "FE-001")
            _run(check_astro_pinned_versions, "FE-008")
            _run(check_astro_build_time_data, "FE-009")
            _run(check_astro_runtime_queries, "FE-010")
        if dod_type == "new_react_app":
            _run(check_vite_react_ts, "FE-002")
            _run(check_shadcn, "FE-004")
            _run(check_react_hook_form_zod, "FE-005")

    if is_pipeline_cog:
        _mark_checked("TEST-001", "TEST-002", "TEST-004")
        try:
            findings.extend(check_pipeline_cog_tests(repo_path, exceptions=_exceptions))
        except Exception as exc:
            findings.append(
                _finding(
                    "CHECKER",
                    "WARN",
                    "structural_conformance",
                    f"check_pipeline_cog_tests raised an unexpected error: {exc}",
                    "",
                )
            )
        _run(check_retry_logic, "PIPE-007")
        _run(check_no_retired_trigger_patterns, "PIPE-008")
        _run(check_evaluation_step, "PIPE-009")
        _run(check_prefect_serve_pattern, "CD-015")
        _run(check_db_writes_use_upserts, "PIPE-002")
        _run(check_inputs_not_deleted, "PIPE-005")

    # PIPE-001 applies to both pipeline-cogs and trigger-cogs — Prefect is
    # required on both, with slightly different usage patterns (see the
    # check function for the pipeline-vs-trigger branch).
    if is_pipeline_cog or is_trigger_cog:
        _cog_subtype = "trigger" if is_trigger_cog else "pipeline"

        def _pipe_001_check(p: Path) -> list[Finding]:
            return check_prefect_present(p, cog_subtype=_cog_subtype)

        _run(_pipe_001_check, "PIPE-001")

        # CD-005 also covers pipeline-cogs and trigger-cogs. It overlaps with
        # PIPE-001's condition 1 by design (see the rule body) — a repo missing
        # prefect entirely will produce two findings, which is correct.
        def _cd_005_check(p: Path) -> list[Finding]:
            return check_prefect_cloud_observability(p, cog_subtype=_cog_subtype)

        _run(_cd_005_check, "CD-005")

    # API-001 / API-002 apply to api-service repos regardless of language.
    if is_api_service:

        def _api_001_check(p: Path) -> list[Finding]:
            return check_railway_hosted_api(p, language=language)

        def _api_002_check(p: Path) -> list[Finding]:
            return check_postgres_only_data_store(p, language=language)

        _run(_api_001_check, "API-001")
        _run(_api_002_check, "API-002")

    # CD-006 applies to pipeline-cogs, trigger-cogs, and api-services —
    # any repo type where GHA relaying would be a genuine anti-pattern.
    if is_pipeline_cog or is_trigger_cog or is_api_service:
        _run(check_gha_not_trigger_relay, "CD-006")

    # CD-012 (Clerk M2M JWT) applies to the same set — services that
    # make or receive internal calls should use JWT, not API keys.
    if is_pipeline_cog or is_trigger_cog or is_api_service:

        def _cd_012_check(p: Path) -> list[Finding]:
            return check_clerk_m2m_auth(p, language=language)

        _run(_cd_012_check, "CD-012")

    # XSTACK-002 (response shape parity) applies to api-service only per
    # the narrowed applies_to in the audit.
    if is_api_service:

        def _xstack_002_check(p: Path) -> list[Finding]:
            return check_response_shape_parity(p, language=language)

        _run(_xstack_002_check, "XSTACK-002")

    # DOC-005 (ADRs present) applies to pipeline-cogs, trigger-cogs,
    # api-services, shared-libraries, and standards-repo per the catalog.
    if (
        is_pipeline_cog
        or is_trigger_cog
        or is_api_service
        or is_library
        or (evaluator_config is not None and evaluator_config.is_standards_repo)
    ):
        _run(check_adrs_present, "DOC-005")

    _is_static = (
        evaluator_config is not None and evaluator_config.is_static_site
    ) or dod_type == "new_frontend_site"
    # Wave 9 — coverage sweep for 35 rules previously only in catalog
    # ==================================================================

    # API domain — api-service only
    if is_api_service:

        def _api_003(p: Path) -> list[Finding]:
            return check_orm_usage(p, language=language)

        def _api_004(p: Path) -> list[Finding]:
            return check_v1_route_prefix(p, language=language)

        def _api_007(p: Path) -> list[Finding]:
            return check_clerk_auth_dep(p, language=language)

        def _api_008(p: Path) -> list[Finding]:
            return check_unauthenticated_routes(p, language=language)

        def _api_009(p: Path) -> list[Finding]:
            return check_cors_config(p, language=language)

        def _api_010(p: Path) -> list[Finding]:
            return check_health_endpoint(p, language=language)

        _run(_api_003, "API-003")
        _run(_api_004, "API-004")
        _run(check_response_envelope_presence, "API-005")
        _run(_api_007, "API-007")
        _run(_api_008, "API-008")
        _run(_api_009, "API-009")
        _run(_api_010, "API-010")

        def _api_011(p: Path) -> list[Finding]:
            return check_migration_in_ci(
                p, language=language, monorepo_root=monorepo_root
            )

        _run(_api_011, "API-011")

        # API-006 and AUTH-002 — Python-only shape checks
        if language == "python":
            _run(check_owner_id_column, "API-006")
            _run(check_auth_header_parity, "AUTH-002")

    # CD-008 logger misuse — applies broadly; language-gated to Python.
    if language == "python":
        _run(check_logger_misuse, "CD-008")

    # CD-010 three-layer observability — applies to all runtime services.
    if is_pipeline_cog or is_trigger_cog or is_api_service:
        _cog_st_010 = (
            "trigger" if is_trigger_cog else ("pipeline" if is_pipeline_cog else None)
        )

        def _cd_010_check(p: Path) -> list[Finding]:
            return check_three_layer_observability(p, cog_subtype=_cog_st_010)

        _run(_cd_010_check, "CD-010")

    # CD-014 — static-site deploy target
    if _is_static:
        _run(check_cloudflare_pages_deploy, "CD-014")

    # DOC-006 / DOC-007 — Python docstring / Pydantic descriptions.
    if language == "python":
        _run(check_public_docstrings, "DOC-006")
    if language == "python" and (is_pipeline_cog or is_api_service):
        _run(check_pydantic_field_descriptions, "DOC-007")

    # DOC-010 / DOC-011 — FastAPI route docs + unauthenticated-route intent.
    if is_api_service and language == "python":
        _run(check_fastapi_route_docs, "DOC-010")
        _run(check_unauthenticated_routes_documented, "DOC-011")

    # FE-006 — fetch error handling on static sites + react apps.
    if (
        _is_static
        or (evaluator_config is not None and evaluator_config.is_react_app)
        or dod_type == "new_react_app"
    ):
        _run(check_fetch_error_handling, "FE-006")

    # MONO-001 / MONO-002 — monorepo shared-lib + root CI.
    if evaluator_config is not None and getattr(evaluator_config, "monorepo", None):

        def _mono_001(p: Path) -> list[Finding]:
            return check_monorepo_shared_lib_root(
                p,
                workspace_package_json_text=workspace_package_json_text,
                language=language,
            )

        def _mono_002(p: Path) -> list[Finding]:
            return check_monorepo_root_ci(p, monorepo_root=monorepo_root)

        _run(_mono_001, "MONO-001")
        _run(_mono_002, "MONO-002")

    # Pipeline rules — PIPE-003, PIPE-004, PIPE-006, PIPE-011, PIPE-012
    if is_pipeline_cog or is_trigger_cog:
        _cog_st_pipe = "trigger" if is_trigger_cog else "pipeline"

        def _pipe_011_check(p: Path) -> list[Finding]:
            return check_final_evaluation_task(p, cog_subtype=_cog_st_pipe)

        _run(check_per_item_vs_collection_tasks, "PIPE-003")
        _run(check_shared_resource_concurrency, "PIPE-004")
        _run(check_prefect_run_logger, "PIPE-006")
        _run(_pipe_011_check, "PIPE-011")
        _run(check_hardcoded_retry_delay, "PIPE-012")

    # Principles — PRIN-002, PRIN-005. Applies broadly.
    if is_pipeline_cog or is_trigger_cog:
        _run(check_per_item_error_handling, "PRIN-002")
    if is_pipeline_cog or is_trigger_cog or is_api_service:

        def _prin_005(p: Path) -> list[Finding]:
            return check_production_observability(p, language=language)

        _run(_prin_005, "PRIN-005")

    # Python — PY-004, PY-015
    if language == "python" and (is_pipeline_cog or is_api_service or is_library):
        _run(check_pydantic_for_external_data, "PY-004")
    if is_api_service and language == "python":
        _run(check_async_sqlalchemy, "PY-015")

    # Configuration — CFG-001, CFG-002
    if language == "python" and (is_pipeline_cog or is_api_service or is_library):
        _run(check_settings_field_consistency, "CFG-001")
        _run(check_env_example_settings_parity, "CFG-002")

    # Testing — TEST-008, TEST-009, TEST-010, TEST-011, TEST-013, TEST-GAP-001
    if is_api_service:
        _run(check_testclient_for_v1_routes, "TEST-008")
        if language == "python":
            _run(check_db_test_fixtures, "TEST-009")
            _run(check_route_contract_tests, "TEST-010")
    if is_pipeline_cog or is_api_service:
        _run(check_mock_assertions, "TEST-011")
        _run(check_test_gap_critical_paths, "TEST-GAP-001")

    def _test_013(p: Path) -> list[Finding]:
        return check_hardcoded_time_values(p, language=language)

    if (
        is_pipeline_cog
        or is_api_service
        or (evaluator_config is not None and evaluator_config.is_react_app)
        or dod_type == "new_react_app"
    ):
        _run(_test_013, "TEST-013")

    # XSTACK-004 — env var prefix. Frontend + api-service.
    if (
        _is_static
        or (evaluator_config is not None and evaluator_config.is_react_app)
        or dod_type == "new_react_app"
        or is_api_service
    ):
        _run(check_env_var_prefix, "XSTACK-004")

    # Git-history-dependent checks. Require the repo to be git-cloned
    # (not zipball-extracted). The check functions return [] silently if
    # no .git directory is present, so this wiring is safe even against
    # older code paths.
    from evaluator_cog.engine.git_history import (
        check_breaking_change_on_major_tags,
        check_conventional_commits,
        check_fix_commits_touch_tests,
    )

    if (
        is_pipeline_cog
        or is_trigger_cog
        or is_api_service
        or _is_static
        or (evaluator_config is not None and evaluator_config.is_react_app)
        or dod_type == "new_react_app"
    ):
        _run(check_conventional_commits, "VER-001")
        _run(check_breaking_change_on_major_tags, "VER-002")

    if is_pipeline_cog or is_trigger_cog or is_api_service or is_library:
        _run(check_fix_commits_touch_tests, "PRIN-008")

    _run(
        lambda p: check_releaserc_assets(p, monorepo_root=monorepo_root),
        "VER-008",
    )

    # XSTACK-003 pnpm — applies to api-service (TS) and react-app
    if evaluator_config is not None:
        _needs_pnpm = evaluator_config.is_react_app or (
            evaluator_config.is_api_service and language == "typescript"
        )
    else:
        _needs_pnpm = dod_type in ("new_hono_service", "new_react_app")

    if _needs_pnpm:

        def _pnpm_lock_check(p: Path) -> list[Finding]:
            return check_pnpm_lockfile(p, monorepo_root=monorepo_root)

        _run(_pnpm_lock_check, "XSTACK-003")

    # EVAL-008: check for evaluator.yaml presence
    _mark_checked("EVAL-008")
    if not (repo_path / "evaluator.yaml").exists():
        findings.append(
            _finding(
                "EVAL-008",
                "WARN",
                "structural_conformance",
                "evaluator.yaml is absent from repo root.",
                "Add evaluator.yaml declaring type, traits, exemptions, and deferrals.",
            )
        )

    findings = _deduplicate_same_repo_findings(findings)
    return CheckResult(findings=findings, checked_rule_ids=checked_rule_ids)
