"""Smoke tests for the deterministic conformance engine."""

import tempfile
from pathlib import Path

from evaluator_cog.engine.deterministic import (
    check_changelog,
    check_ci,
    check_common_python_utils_dep,
    check_env_example,
    check_pyproject,
    check_readme,
    check_src_layout,
    check_test_structure,
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


def test_check_pyproject_respects_exceptions_for_subrules() -> None:
    repo = _make_repo({"pyproject.toml": '[project]\nname = "my-cog"\n'})
    findings = check_pyproject(repo, exceptions=frozenset({"CD-002", "PY-001"}))
    rule_ids = [f["rule_id"] for f in findings]
    assert "CD-002" not in rule_ids
    assert "PY-001" not in rule_ids
    assert "PY-002" in rule_ids


def test_check_ci_missing_semantic_release() -> None:
    repo = _make_repo(
        {
            ".github/workflows/ci.yml": "name: CI\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        }
    )
    findings = check_ci(repo)
    assert any(f["rule_id"] == "VER-003" for f in findings)


def test_check_ci_respects_exceptions_for_subrules() -> None:
    repo = _make_repo(
        {
            ".github/workflows/ci.yml": "name: CI\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        }
    )
    findings = check_ci(repo, exceptions=frozenset({"VER-005"}))
    rule_ids = [f["rule_id"] for f in findings]
    assert "VER-005" not in rule_ids
    assert "VER-003" in rule_ids
    assert "VER-006" in rule_ids


def test_check_ci_accepts_pnpm_exec_semantic_release(tmp_path: Path) -> None:
    """VER-006 should not fire when pnpm exec semantic-release is present."""
    ci = tmp_path / ".github" / "workflows"
    ci.mkdir(parents=True)
    (ci / "ci.yml").write_text(
        "semantic-release\nfetch-depth: 0\npnpm exec semantic-release\n"
    )
    findings = check_ci(tmp_path)
    rule_ids = [f["rule_id"] for f in findings]
    assert "VER-006" not in rule_ids


def test_check_env_example_finds_monorepo_location(tmp_path: Path) -> None:
    """DOC-004 should not fire when .env.example is in apps/api/."""
    (tmp_path / "apps" / "api").mkdir(parents=True)
    (tmp_path / "apps" / "api" / ".env.example").write_text("DATABASE_URL=\n")
    findings = check_env_example(tmp_path)
    assert findings == []


def test_check_env_example_fires_when_absent_everywhere(tmp_path: Path) -> None:
    """DOC-004 should fire when .env.example is absent in all locations."""
    findings = check_env_example(tmp_path)
    assert any(f["rule_id"] == "DOC-004" for f in findings)


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


def test_run_all_checks_structured_exception_emits_info() -> None:
    """Structured check_exceptions with reasons should emit INFO findings."""
    repo = _make_repo({})
    findings = run_all_checks(
        repo,
        check_exceptions=["DOC-001"],
        exception_reasons={"DOC-001": "standards repo — no service entry point"},
        dod_type="new_cog",
    )
    assert "DOC-001" not in [f["rule_id"] for f in findings if f["severity"] != "INFO"]
    info_findings = [
        f for f in findings if f["rule_id"] == "DOC-001" and f["severity"] == "INFO"
    ]
    assert len(info_findings) == 1
    assert "standards repo" in info_findings[0]["finding"]


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


def test_run_all_checks_pipeline_tests_require_pipeline_subtype() -> None:
    repo = _make_repo({"tests/test_basic.py": "def test_ok():\n    assert True\n"})
    findings = run_all_checks(repo, service_type="worker", language="python")
    rule_ids = [f["rule_id"] for f in findings]
    assert "TEST-001" not in rule_ids

    findings_pipeline = run_all_checks(
        repo,
        service_type="worker",
        language="python",
        cog_subtype="pipeline",
        dod_type="new_cog",
    )
    pipeline_rule_ids = [f["rule_id"] for f in findings_pipeline]
    assert "TEST-001" in pipeline_rule_ids


def test_run_all_checks_skips_python_checks_for_frontend() -> None:
    """Frontend dod_type should not trigger Python-specific checks."""
    repo = _make_repo({})
    findings = run_all_checks(
        repo,
        language="typescript",
        service_type="site",
        dod_type="new_frontend_site",
    )
    rule_ids = [f["rule_id"] for f in findings]
    assert "PY-005" not in rule_ids
    assert "PY-006" not in rule_ids
    assert "PY-008" not in rule_ids


def test_check_test_structure_no_test003_when_tests_mention_error(
    tmp_path: Path,
) -> None:
    """TEST-003 must not require the literal word 'malform'; 'error' is enough."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cog.py").write_text(
        "def test_poll_error_caught_and_loop_continues():\n"
        '    """First poll errors; loop logs the error and continues."""\n'
        "    assert True\n"
    )
    findings = check_test_structure(tmp_path)
    assert not any(f["rule_id"] == "TEST-003" for f in findings)


def test_check_test_structure_no_test003_when_tests_mention_exception(
    tmp_path: Path,
) -> None:
    """Tests that reference exception handling satisfy TEST-003."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cog.py").write_text(
        "def test_handles_exception():\n"
        "    try:\n"
        "        raise RuntimeError('x')\n"
        "    except Exception:\n"
        "        pass\n"
    )
    findings = check_test_structure(tmp_path)
    assert not any(f["rule_id"] == "TEST-003" for f in findings)


def test_check_test_structure_emits_test003_without_failure_signals(
    tmp_path: Path,
) -> None:
    """Happy-path-only tests must still trigger TEST-003."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cog.py").write_text(
        "def test_happy_path():\n    assert 1 + 1 == 2\n"
    )
    findings = check_test_structure(tmp_path)
    assert any(f["rule_id"] == "TEST-003" for f in findings)
