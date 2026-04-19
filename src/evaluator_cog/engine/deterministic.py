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
    """DOC-005: ADR trail for non-trivial repos (LOC heuristic under src/)."""
    CHECK_ID = "DOC-005"
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
    if loc < 50:
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
