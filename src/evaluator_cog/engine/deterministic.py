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
    # Check root first, then common monorepo locations
    candidates = [
        repo_path / ".env.example",
        repo_path / "apps" / "api" / ".env.example",
        repo_path / "apps" / "app" / ".env.example",
        repo_path / "app" / ".env.example",
        repo_path / "backend" / ".env.example",
        repo_path / "server" / ".env.example",
    ]
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


def check_common_python_utils_dep(repo_path: Path) -> list[Finding]:
    """PY-006: common-python-utils declared as dependency."""
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
) -> list[Finding]:
    """
    Runs all CI checks in one pass.
    Covers: VER-003, VER-005, VER-006.
    """
    findings = []
    _exc = exceptions or frozenset()
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


def check_pipeline_cog_tests(
    repo_path: Path,
    exceptions: frozenset[str] | None = None,
) -> list[Finding]:
    """TEST-001, TEST-002, TEST-004: pipeline cog critical path tests."""
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


# -- Runner -------------------------------------------------------------------


def run_all_checks(
    repo_path: Path,
    language: str = "python",
    service_type: str = "worker",
    cog_subtype: str | None = None,
    dod_type: str | None = None,
    check_exceptions: list[str] | None = None,
    exception_reasons: dict[str, str] | None = None,
) -> list[Finding]:
    """Run deterministic checks against a repo and return combined findings."""
    is_python = language == "python" or dod_type in (
        "new_cog",
        "new_fastapi_service",
    )
    is_library = service_type == "library" or dod_type is None
    is_pipeline_cog = dod_type == "new_cog" or (
        is_python and service_type == "worker" and cog_subtype == "pipeline"
    )
    is_fastapi = dod_type == "new_fastapi_service"
    is_frontend = dod_type in ("new_frontend_site", "new_react_app")

    _exceptions = frozenset(check_exceptions or [])
    _exception_reasons = exception_reasons or {}

    def _run(check_fn, rule_id: str | None = None) -> None:
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
            findings.extend(check_fn(repo_path))
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

    _run(check_readme, "DOC-001")
    _run(check_changelog, "DOC-003")
    _run(check_releaserc, "VER-003")

    if not is_library:
        _run(check_env_example, "DOC-004")

    if is_python and not is_frontend:
        _run(check_pre_commit, "PY-008")
        _run(check_src_layout, "PY-005")
        _run(check_no_setup_py, "PY-007")
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

    if (is_python or is_fastapi) and not is_library and not is_frontend:
        _run(check_common_python_utils_dep, "PY-006")

    _run(check_no_hardcoded_urls, "FE-007")

    try:
        findings.extend(check_ci(repo_path, exceptions=_exceptions))
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

    if is_python:
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

    if is_pipeline_cog:
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

    return findings
