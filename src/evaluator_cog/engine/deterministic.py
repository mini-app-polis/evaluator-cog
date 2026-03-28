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

from pathlib import Path
from typing import Any

Finding = dict[str, Any]


def _finding(
    rule_id: str,
    severity: str,
    dimension: str,
    finding: str,
    suggestion: str = "",
) -> Finding:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "dimension": dimension,
        "finding": finding,
        "suggestion": suggestion,
    }


# -- File presence checks -----------------------------------------------------


def check_readme(repo_path: Path) -> list[Finding]:
    """DOC-001: README.md is mandatory."""
    findings = []
    if not (repo_path / "README.md").exists():
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


def check_changelog(repo_path: Path) -> list[Finding]:
    """DOC-003: CHANGELOG.md required."""
    findings = []
    if not (repo_path / "CHANGELOG.md").exists():
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


def check_env_example(repo_path: Path) -> list[Finding]:
    """DOC-004: .env.example is required."""
    findings = []
    if not (repo_path / ".env.example").exists():
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


def check_releaserc(repo_path: Path) -> list[Finding]:
    """VER-003: semantic-release on all repos."""
    findings = []
    if not (repo_path / ".releaserc.json").exists():
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


def check_pyproject(repo_path: Path) -> list[Finding]:
    """
    Runs all pyproject.toml checks in one pass.
    Covers: PY-001, PY-002, PY-003, PY-006, PY-009, PY-010, CD-002, XSTACK-001.
    """
    findings = []
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

    if (
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

    if "[tool.ruff]" not in content:
        findings.append(
            _finding(
                "PY-002",
                "WARN",
                "structural_conformance",
                "[tool.ruff] section absent from pyproject.toml.",
                "Add ruff configuration to pyproject.toml.",
            )
        )

    if 'requires-python = ">=3.11"' not in content and ">=3.12" not in content:
        findings.append(
            _finding(
                "PY-003",
                "WARN",
                "structural_conformance",
                "Python minimum version may be below 3.11.",
                'Set requires-python = ">=3.11" in pyproject.toml.',
            )
        )

    if "common-python-utils" not in content:
        findings.append(
            _finding(
                "PY-006",
                "ERROR",
                "structural_conformance",
                "common-python-utils not declared as a dependency.",
                "Add common-python-utils to [project].dependencies.",
            )
        )

    if "hatchling" not in content:
        findings.append(
            _finding(
                "PY-009",
                "INFO",
                "structural_conformance",
                "hatchling not found as build backend.",
                'Set build-backend = "hatchling.build" in [build-system].',
            )
        )

    if "line-length = 88" not in content:
        findings.append(
            _finding(
                "PY-010",
                "INFO",
                "structural_conformance",
                "ruff line-length is not explicitly set to 88.",
                "Add line-length = 88 under [tool.ruff].",
            )
        )

    if "sentry-sdk" not in content:
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


def check_ci(repo_path: Path) -> list[Finding]:
    """
    Runs all CI checks in one pass.
    Covers: VER-003, VER-005, VER-006, TEST-006.
    """
    findings = []
    ci = repo_path / ".github" / "workflows" / "ci.yml"
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

    if "semantic-release" not in content:
        findings.append(
            _finding(
                "VER-003",
                "ERROR",
                "cd_readiness",
                "semantic-release not found in ci.yml.",
                "Add a release job running npx semantic-release.",
            )
        )

    if "fetch-depth: 0" not in content:
        findings.append(
            _finding(
                "VER-005",
                "ERROR",
                "cd_readiness",
                "fetch-depth: 0 absent from ci.yml checkout step.",
                "Add fetch-depth: 0 to the actions/checkout step in the release job.",
            )
        )

    if "npm install --no-save" not in content:
        findings.append(
            _finding(
                "VER-006",
                "ERROR",
                "cd_readiness",
                "npm install --no-save step absent from release job.",
                "Add explicit npm install --no-save before npx semantic-release.",
            )
        )

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


# -- AST checks ---------------------------------------------------------------


def check_no_print_statements(repo_path: Path) -> list[Finding]:
    """CD-003: No print() statements in production code paths."""
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


# -- Test checks --------------------------------------------------------------


def check_test_structure(repo_path: Path) -> list[Finding]:
    """
    TEST-001, TEST-002, TEST-003, TEST-004, TEST-005: test directory checks.
    """
    findings = []
    tests_dir = repo_path / "tests"
    if not tests_dir.is_dir():
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
            "TEST-003",
            "ERROR",
            "testing_coverage",
            "malform",
            "No failure path test found.",
            "Add a test passing malformed input and asserting the cog continues.",
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
        if keyword not in all_content.lower():
            findings.append(
                _finding(rule_id, severity, dimension, finding_text, suggestion)
            )

    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists() and "[tool.pytest.ini_options]" not in pyproject.read_text():
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


# -- Runner -------------------------------------------------------------------


def run_all_checks(repo_path: Path) -> list[Finding]:
    """Run all deterministic checks against a repo and return combined findings."""
    checks = [
        check_readme,
        check_changelog,
        check_env_example,
        check_pre_commit,
        check_releaserc,
        check_src_layout,
        check_no_setup_py,
        check_pyproject,
        check_ci,
        check_no_print_statements,
        check_no_hardcoded_urls,
        check_test_structure,
    ]
    findings: list[Finding] = []
    for check in checks:
        try:
            findings.extend(check(repo_path))
        except Exception as exc:
            findings.append(
                _finding(
                    "CHECKER",
                    "WARN",
                    "structural_conformance",
                    f"Check {check.__name__} raised an unexpected error: {exc}",
                    "Investigate the checker itself.",
                )
            )
    return findings
