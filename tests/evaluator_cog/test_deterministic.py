"""Smoke tests for the deterministic conformance engine."""

import tempfile
from pathlib import Path

from evaluator_cog.engine.deterministic import (
    check_changelog,
    check_ci,
    check_common_python_utils_dep,
    check_pyproject,
    check_readme,
    check_src_layout,
    run_all_checks,
)


def _make_repo(files: dict[str, str]) -> Path:
    """Create a temporary repo directory with the given files."""
    tmp = Path(tempfile.mkdtemp())
    for rel_path, content in files.items():
        full = tmp / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return tmp


def test_check_readme_missing() -> None:
    repo = _make_repo({})
    findings = check_readme(repo)
    assert any(f["rule_id"] == "DOC-001" for f in findings)


def test_check_readme_present() -> None:
    repo = _make_repo({"README.md": "# My Repo\nSome content."})
    findings = check_readme(repo)
    assert findings == []


def test_check_changelog_missing() -> None:
    repo = _make_repo({})
    findings = check_changelog(repo)
    assert any(f["rule_id"] == "DOC-003" for f in findings)


def test_check_src_layout_missing() -> None:
    repo = _make_repo({"mypackage/__init__.py": ""})
    findings = check_src_layout(repo)
    assert any(f["rule_id"] == "PY-005" for f in findings)


def test_check_src_layout_present() -> None:
    repo = _make_repo({"src/mypackage/__init__.py": ""})
    findings = check_src_layout(repo)
    assert findings == []


def test_check_common_python_utils_dep_missing() -> None:
    repo = _make_repo(
        {
            "pyproject.toml": '[project]\nname = "my-cog"\n',
            "uv.lock": "",
        }
    )
    findings = check_common_python_utils_dep(repo)
    assert any(f["rule_id"] == "PY-006" for f in findings)


def test_check_pyproject_missing_common_utils_not_emitted_by_check_pyproject() -> None:
    repo = _make_repo(
        {
            "pyproject.toml": '[project]\nname = "my-cog"\n',
            "uv.lock": "",
        }
    )
    findings = check_pyproject(repo)
    assert not any(f["rule_id"] == "PY-006" for f in findings)


def test_check_ci_missing_semantic_release() -> None:
    repo = _make_repo(
        {
            ".github/workflows/ci.yml": "name: CI\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        }
    )
    findings = check_ci(repo)
    assert any(f["rule_id"] == "VER-003" for f in findings)


def test_run_all_checks_empty_repo() -> None:
    repo = _make_repo({})
    findings = run_all_checks(repo)
    rule_ids = [f["rule_id"] for f in findings]
    assert "DOC-001" in rule_ids
    assert "PY-005" in rule_ids


def test_run_all_checks_never_raises() -> None:
    # Should not raise even on a completely empty path
    repo = Path(tempfile.mkdtemp())
    findings = run_all_checks(repo)
    assert isinstance(findings, list)


def test_run_all_checks_respects_check_exceptions() -> None:
    repo = _make_repo({})
    findings = run_all_checks(repo, check_exceptions=["DOC-001"])
    rule_ids = [f["rule_id"] for f in findings]
    assert "DOC-001" not in rule_ids
    assert "DOC-003" in rule_ids


def test_run_all_checks_non_python_skips_python_rules() -> None:
    repo = _make_repo({})
    findings = run_all_checks(repo, language="typescript", service_type="site")
    rule_ids = [f["rule_id"] for f in findings]
    assert "PY-005" not in rule_ids
    assert "DOC-001" in rule_ids


def test_run_all_checks_library_skips_env_example() -> None:
    repo = _make_repo({})
    findings = run_all_checks(repo, service_type="library")
    rule_ids = [f["rule_id"] for f in findings]
    assert "DOC-004" not in rule_ids
