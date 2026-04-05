"""Smoke tests for the deterministic conformance engine."""

import tempfile
from pathlib import Path

from evaluator_cog.engine.deterministic import (
    Finding,
    _deduplicate_same_repo_findings,
    check_astro_framework,
    check_changelog,
    check_ci,
    check_common_python_utils_dep,
    check_duplicate_prefix,
    check_env_example,
    check_evaluation_step,
    check_failed_prefix,
    check_healthchecks_integration,
    check_mypy_in_ci,
    check_naming_conventions,
    check_no_dead_code,
    check_no_hardcoded_secrets,
    check_no_hardcoded_urls,
    check_no_manual_changelog,
    check_no_print_statements,
    check_no_retired_trigger_patterns,
    check_no_setup_py,
    check_pipeline_cog_tests,
    check_pnpm_lockfile,
    check_pre_commit,
    check_prefect_serve_pattern,
    check_pyproject,
    check_pytest_coverage_in_ci,
    check_react_hook_form_zod,
    check_readme,
    check_readme_io,
    check_readme_running_locally,
    check_releaserc,
    check_releaserc_assets,
    check_respx_for_http_mocking,
    check_retry_logic,
    check_shadcn,
    check_shared_library_used,
    check_split_package_identity,
    check_src_layout,
    check_structured_logging,
    check_tailwind,
    check_test_structure,
    check_vite_react_ts,
    run_all_checks,
)
from evaluator_cog.flows.conformance import _deduplicate_sibling_findings


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


# ── VER-006 / pnpm add pattern ────────────────────────────────────────────────


def test_check_ci_accepts_pnpm_add_before_semantic_release(tmp_path: Path) -> None:
    """VER-006 must not fire when pnpm add --save-dev installs plugins before release."""
    ci = tmp_path / ".github" / "workflows"
    ci.mkdir(parents=True)
    (ci / "ci.yml").write_text(
        "semantic-release\n"
        "fetch-depth: 0\n"
        "pnpm add --save-dev @semantic-release/changelog @semantic-release/git\n"
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
    result = run_all_checks(repo)
    findings = result.findings
    rule_ids = [f["rule_id"] for f in findings]
    assert "DOC-001" in rule_ids
    assert "PY-005" in rule_ids


def test_run_all_checks_never_raises() -> None:
    # Should not raise even on a completely empty path
    repo = Path(tempfile.mkdtemp())
    result = run_all_checks(repo)
    assert isinstance(result.findings, list)
    assert isinstance(result.checked_rule_ids, set)


def test_run_all_checks_structured_exception_emits_info() -> None:
    """Structured check_exceptions with reasons should emit INFO findings."""
    repo = _make_repo({})
    result = run_all_checks(
        repo,
        check_exceptions=["DOC-001"],
        exception_reasons={"DOC-001": "standards repo — no service entry point"},
        dod_type="new_cog",
    )
    findings = result.findings
    assert "DOC-001" not in [f["rule_id"] for f in findings if f["severity"] != "INFO"]
    info_findings = [
        f for f in findings if f["rule_id"] == "DOC-001" and f["severity"] == "INFO"
    ]
    assert len(info_findings) == 1
    assert "standards repo" in info_findings[0]["finding"]


def test_run_all_checks_non_python_skips_python_rules() -> None:
    repo = _make_repo({})
    result = run_all_checks(repo, language="typescript", service_type="site")
    findings = result.findings
    rule_ids = [f["rule_id"] for f in findings]
    assert "PY-005" not in rule_ids
    assert "DOC-001" in rule_ids


def test_run_all_checks_library_skips_env_example() -> None:
    repo = _make_repo({})
    result = run_all_checks(repo, service_type="library")
    findings = result.findings
    rule_ids = [f["rule_id"] for f in findings]
    assert "DOC-004" not in rule_ids


def test_run_all_checks_pipeline_tests_require_pipeline_subtype() -> None:
    repo = _make_repo({"tests/test_basic.py": "def test_ok():\n    assert True\n"})
    result = run_all_checks(repo, service_type="worker", language="python")
    findings = result.findings
    rule_ids = [f["rule_id"] for f in findings]
    assert "TEST-001" not in rule_ids

    result_pipeline = run_all_checks(
        repo,
        service_type="worker",
        language="python",
        cog_subtype="pipeline",
        dod_type="new_cog",
    )
    pipeline_rule_ids = [f["rule_id"] for f in result_pipeline.findings]
    assert "TEST-001" in pipeline_rule_ids


def test_run_all_checks_skips_python_checks_for_frontend() -> None:
    """Frontend dod_type should not trigger Python-specific checks."""
    repo = _make_repo({})
    result = run_all_checks(
        repo,
        language="typescript",
        service_type="site",
        dod_type="new_frontend_site",
    )
    findings = result.findings
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


def test_check_naming_conventions_flags_camel_case_module() -> None:
    repo = _make_repo(
        {
            "pyproject.toml": '[project]\nname = "my-package"\n',
            "src/myPackage/ProcessFiles.py": "x = 1\n",
        }
    )
    findings = check_naming_conventions(repo)
    assert any(f["rule_id"] == "PY-011" for f in findings)


def test_check_naming_conventions_passes_snake_case(tmp_path: Path) -> None:
    repo = tmp_path / "my-package"
    (repo / "src" / "my_package").mkdir(parents=True)
    (repo / "src" / "my_package" / "process_files.py").write_text("x = 1\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "my-package"\n')
    findings = check_naming_conventions(repo)
    assert findings == []


def test_check_failed_prefix_flags_missing() -> None:
    repo = _make_repo(
        {
            "src/my_pkg/worker.py": (
                "import shutil\nfrom pathlib import Path\n"
                "def go(p: Path):\n"
                "    try:\n"
                "        shutil.move(str(p), 'ok.csv')\n"
                "    except Exception:\n"
                "        shutil.move(str(p), 'failed.csv')\n"
            )
        }
    )
    findings = check_failed_prefix(repo)
    assert any(f["rule_id"] == "PY-012" for f in findings)


def test_check_failed_prefix_passes_when_present() -> None:
    repo = _make_repo(
        {
            "src/my_pkg/worker.py": (
                "import shutil\nfrom pathlib import Path\n"
                "def go(p: Path):\n"
                "    try:\n"
                "        shutil.move(str(p), 'ok.csv')\n"
                "    except Exception:\n"
                "        shutil.move(str(p), f'FAILED_{p.name}')\n"
            )
        }
    )
    findings = check_failed_prefix(repo)
    assert findings == []


def test_check_duplicate_prefix_flags_and_passes() -> None:
    failing = _make_repo(
        {"src/my_pkg/dedup.py": "def x():\n    # duplicate file\n    return 'dup'\n"}
    )
    assert any(f["rule_id"] == "PY-013" for f in check_duplicate_prefix(failing))
    passing = _make_repo(
        {
            "src/my_pkg/dedup.py": "possible_duplicate_ = True\ndef x():\n    return 'duplicate'\n"
        }
    )
    assert check_duplicate_prefix(passing) == []


def test_check_readme_io_flags_and_passes() -> None:
    bad = _make_repo({"README.md": "# Title\nminimal\n"})
    assert any(f["rule_id"] == "DOC-002" for f in check_readme_io(bad))
    good = _make_repo(
        {
            "README.md": "# Service\nInput from source folder.\nProduces output to /v1/results endpoint.\n"
        }
    )
    assert check_readme_io(good) == []


def test_check_no_manual_changelog_flags_prose_section() -> None:
    repo = _make_repo(
        {"CHANGELOG.md": "# Changelog\n## 1.0.0 — 2026-03\nmanual notes\n"}
    )
    findings = check_no_manual_changelog(repo)
    assert any(f["rule_id"] == "VER-004" for f in findings)


def test_check_no_manual_changelog_passes_sr_format() -> None:
    repo = _make_repo(
        {"CHANGELOG.md": "# Changelog\n## [1.0.0](https://example.com) (2026-03-01)\n"}
    )
    findings = check_no_manual_changelog(repo)
    assert findings == []


def test_check_healthchecks_integration_skips_pipeline_cog() -> None:
    repo = _make_repo({})
    assert check_healthchecks_integration(repo, cog_subtype="pipeline") == []


def test_check_healthchecks_integration_flags_trigger_cog_missing_url() -> None:
    repo = _make_repo({"src/my_pkg/main.py": "def run():\n    pass\n"})
    findings = check_healthchecks_integration(repo, cog_subtype="trigger")
    assert any(f["rule_id"] == "CD-007" for f in findings)


def test_check_structured_logging_flags_import_logging() -> None:
    repo = _make_repo(
        {"src/my_pkg/logs.py": "import logging\nlog = logging.getLogger(__name__)\n"}
    )
    findings = check_structured_logging(repo)
    assert any(f["rule_id"] == "CD-009" for f in findings)


def test_check_structured_logging_passes_shared_logger() -> None:
    repo = _make_repo(
        {
            "src/my_pkg/logs.py": "from mini_app_polis import logger as logger_mod\nlog = logger_mod.get_logger()\n"
        }
    )
    assert check_structured_logging(repo) == []


def test_check_no_hardcoded_secrets_flags_committed_env() -> None:
    repo = _make_repo({".env": "SECRET=abc\n"})
    findings = check_no_hardcoded_secrets(repo)
    assert any(f["rule_id"] == "CD-011" for f in findings)


def test_check_no_hardcoded_secrets_passes_clean_repo() -> None:
    repo = _make_repo({"src/my_pkg/main.py": "import os\nx = os.getenv('TOKEN')\n"})
    assert check_no_hardcoded_secrets(repo) == []


def test_check_respx_flags_missing_from_dev_deps() -> None:
    repo = _make_repo({"pyproject.toml": "[project]\nname='x'\n"})
    findings = check_respx_for_http_mocking(repo)
    assert any(f["rule_id"] == "TEST-007" for f in findings)


def test_check_respx_passes_when_present_and_mocked() -> None:
    repo = _make_repo(
        {
            "pyproject.toml": "[project]\nname='x'\n[tool.uv]\n[dependency-groups]\ndev=['respx']\n",
            "tests/test_http.py": "import httpx\nimport respx\n\ndef test_a():\n    with respx.mock:\n        httpx.get('https://x')\n",
        }
    )
    assert check_respx_for_http_mocking(repo) == []


def test_check_mypy_in_ci_skips_when_no_tool_mypy() -> None:
    repo = _make_repo({"pyproject.toml": "[project]\nname='x'\n"})
    assert check_mypy_in_ci(repo) == []


def test_check_mypy_in_ci_flags_missing_ci_step() -> None:
    repo = _make_repo(
        {
            "pyproject.toml": "[project]\nname='x'\n[tool.mypy]\npython_version='3.11'\n",
            ".github/workflows/ci.yml": "name: CI\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        }
    )
    findings = check_mypy_in_ci(repo)
    assert any(f["rule_id"] == "TEST-012" for f in findings)


def test_check_astro_framework_flags_missing_package() -> None:
    repo = _make_repo({"package.json": '{"name":"site"}\n'})
    findings = check_astro_framework(repo)
    assert any(f["rule_id"] == "FE-001" for f in findings)


def test_check_astro_framework_passes_when_present() -> None:
    repo = _make_repo(
        {
            "package.json": '{"dependencies":{"astro":"^4"}}\n',
            "astro.config.mjs": "export default {}\n",
        }
    )
    assert check_astro_framework(repo) == []


def test_check_vite_react_ts_flags_missing_typescript() -> None:
    repo = _make_repo(
        {
            "package.json": '{"dependencies":{"vite":"1","react":"18"}}\n',
            "tsconfig.json": "{}\n",
        }
    )
    findings = check_vite_react_ts(repo)
    assert any(f["rule_id"] == "FE-002" for f in findings)


def test_check_vite_react_ts_passes() -> None:
    repo = _make_repo(
        {
            "package.json": '{"dependencies":{"vite":"1","react":"18","typescript":"5"}}\n',
            "tsconfig.json": "{}\n",
        }
    )
    assert check_vite_react_ts(repo) == []


def test_check_tailwind_and_shadcn_and_forms() -> None:
    failing = _make_repo(
        {
            "package.json": '{"dependencies":{"react":"18"}}\n',
            "src/app/page.tsx": "export const A = () => <form></form>\n",
        }
    )
    assert any(f["rule_id"] == "FE-003" for f in check_tailwind(failing))
    assert any(f["rule_id"] == "FE-004" for f in check_shadcn(failing))
    assert any(f["rule_id"] == "FE-005" for f in check_react_hook_form_zod(failing))
    passing = _make_repo(
        {
            "package.json": '{"dependencies":{"tailwindcss":"3","@radix-ui/react-slot":"1","react-hook-form":"7","zod":"3"}}\n',
            "tailwind.config.ts": "export default {}\n",
            "src/components/ui/button.tsx": "export const Button = () => null\n",
            "src/app/page.tsx": "export const A = () => <form></form>\n",
        }
    )
    assert check_tailwind(passing) == []
    assert check_shadcn(passing) == []
    assert check_react_hook_form_zod(passing) == []


def test_check_retry_logic_flags_task_without_retries() -> None:
    repo = _make_repo(
        {
            "src/my_pkg/flow.py": "from prefect import task\nimport httpx\n@task\ndef x():\n    return httpx.get('https://x')\n"
        }
    )
    findings = check_retry_logic(repo)
    assert any(f["rule_id"] == "PIPE-007" for f in findings)


def test_check_retry_logic_passes_with_retries() -> None:
    repo = _make_repo(
        {
            "src/my_pkg/flow.py": "from prefect import task\nimport httpx\n@task(retries=2)\ndef x():\n    return httpx.get('https://x')\n"
        }
    )
    assert check_retry_logic(repo) == []


def test_check_evaluation_step_flags_missing() -> None:
    repo = _make_repo({"src/my_pkg/flow.py": "def run():\n    return 1\n"})
    findings = check_evaluation_step(repo)
    assert any(f["rule_id"] == "PIPE-009" for f in findings)


def test_check_evaluation_step_passes_when_signal_present() -> None:
    repo = _make_repo(
        {
            "src/my_pkg/flow.py": "def run():\n    url='/v1/evaluations'\n    return url\n"
        }
    )
    assert check_evaluation_step(repo) == []


def test_check_shared_library_python_flags_missing() -> None:
    repo = _make_repo({"pyproject.toml": "[project]\nname='x'\n"})
    findings = check_shared_library_used(repo, language="python")
    assert any(f["rule_id"] == "XSTACK-001" for f in findings)


def test_check_shared_library_python_passes_when_present() -> None:
    repo = _make_repo(
        {
            "pyproject.toml": "[project]\nname='x'\ndependencies=['common-python-utils']\n",
            "src/x/main.py": "from mini_app_polis import logger as logger_mod\n",
        }
    )
    assert check_shared_library_used(repo, language="python") == []


def test_check_shared_library_ts_flags_hand_rolled_logger() -> None:
    repo = _make_repo(
        {
            "package.json": '{"name":"x","dependencies":{}}\n',
            "src/index.ts": "function createLogger(){ return console }\n",
        }
    )
    findings = check_shared_library_used(repo, language="typescript")
    assert any(f["rule_id"] == "XSTACK-001" for f in findings)


# ── XSTACK-001 / common-typescript-utils ─────────────────────────────────────


def test_check_shared_library_ts_passes_with_common_typescript_utils() -> None:
    """XSTACK-001 must not fire when common-typescript-utils is declared."""
    repo = _make_repo(
        {
            "package.json": '{"name":"x","dependencies":{"common-typescript-utils":"1.0.0"}}\n',
            "src/index.ts": "import { createLogger } from 'common-typescript-utils'\n",
        }
    )
    assert check_shared_library_used(repo, language="typescript") == []


def test_check_shared_library_ts_flags_missing_common_typescript_utils() -> None:
    """XSTACK-001 must fire when common-typescript-utils is absent from package.json."""
    repo = _make_repo(
        {
            "package.json": '{"name":"x","dependencies":{}}\n',
            "src/index.ts": "export const x = 1\n",
        }
    )
    findings = check_shared_library_used(repo, language="typescript")
    assert any(f["rule_id"] == "XSTACK-001" for f in findings)


def test_run_all_checks_xstack001_honoured_via_check_exceptions() -> None:
    """XSTACK-001 in check_exceptions must suppress the finding in run_all_checks."""
    repo = _make_repo(
        {
            "package.json": '{"name":"x","dependencies":{}}\n',
            "src/index.ts": "export const x = 1\n",
        }
    )
    result = run_all_checks(
        repo,
        language="typescript",
        dod_type="new_hono_service",
        check_exceptions=["XSTACK-001"],
        exception_reasons={"XSTACK-001": "static site — no server-side logic"},
    )
    error_findings = [
        f
        for f in result.findings
        if f["rule_id"] == "XSTACK-001" and f["severity"] == "ERROR"
    ]
    assert error_findings == []


def test_run_all_checks_xstack001_suppressed_for_frontend_site() -> None:
    """XSTACK-001 must not fire for new_frontend_site regardless of check_exceptions."""
    repo = _make_repo(
        {
            "package.json": '{"dependencies":{"astro":"4"}}\n',
            "astro.config.mjs": "export default {}\n",
        }
    )
    result = run_all_checks(
        repo,
        language="typescript",
        service_type="site",
        dod_type="new_frontend_site",
    )
    error_findings = [
        f
        for f in result.findings
        if f["rule_id"] == "XSTACK-001" and f["severity"] == "ERROR"
    ]
    assert error_findings == []


def test_run_all_checks_astro_language_does_not_trigger_ts_shared_lib_check() -> None:
    """Astro repos passed with language='astro' must not trigger XSTACK-001.
    conformance.py normalises 'astro' -> 'typescript' before calling run_all_checks,
    and new_frontend_site is exempt from XSTACK-001 by design.
    """
    repo = _make_repo(
        {
            "package.json": '{"dependencies":{"astro":"4"}}\n',
            "astro.config.mjs": "export default {}\n",
        }
    )
    # Simulate what conformance.py does after normalisation
    result = run_all_checks(
        repo,
        language="typescript",
        service_type="site",
        dod_type="new_frontend_site",
    )
    error_findings = [
        f
        for f in result.findings
        if f["rule_id"] == "XSTACK-001" and f["severity"] == "ERROR"
    ]
    assert error_findings == []


def test_run_all_checks_frontend_wires_new_frontend_rules() -> None:
    repo = _make_repo({"package.json": '{"dependencies":{"react":"18"}}\n'})
    result = run_all_checks(
        repo, language="typescript", service_type="site", dod_type="new_frontend_site"
    )
    findings = result.findings
    rule_ids = [f["rule_id"] for f in findings]
    assert "FE-001" in rule_ids
    assert "FE-003" in rule_ids


def test_run_all_checks_returns_checked_rule_ids() -> None:
    """checked_rule_ids includes rules that passed, not just findings."""
    repo = _make_repo(
        {
            "README.md": "# My Repo",
            "CHANGELOG.md": "",
            ".releaserc.json": "{}",
        }
    )
    result = run_all_checks(repo, dod_type="new_cog")
    assert "DOC-001" in result.checked_rule_ids
    assert not any(f["rule_id"] == "DOC-001" for f in result.findings)


def test_checked_rule_ids_includes_pyproject_subrules() -> None:
    """Rule IDs checked inside check_pyproject appear in checked_rule_ids."""
    repo = _make_repo(
        {
            "pyproject.toml": (
                '[project]\nname = "my-cog"\n'
                "[tool.ruff]\nline-length = 88\n"
                "[tool.uv]\n"
                'requires-python = ">=3.11"\n'
            ),
            "uv.lock": "",
        }
    )
    result = run_all_checks(repo, language="python", dod_type="new_cog")
    assert "PY-001" in result.checked_rule_ids
    assert "PY-002" in result.checked_rule_ids


# ── CD-015: Prefect serve() ───────────────────────────────────────────────────


def test_check_prefect_serve_flags_work_pool(tmp_path: Path) -> None:
    (tmp_path / "src" / "my_cog").mkdir(parents=True)
    (tmp_path / "src" / "my_cog" / "main.py").write_text(
        "flow.deploy(name='x', work_pool_name='my-pool')\n"
    )
    findings = check_prefect_serve_pattern(tmp_path)
    assert any(f["rule_id"] == "CD-015" for f in findings)


def test_check_prefect_serve_passes_when_serve_present(tmp_path: Path) -> None:
    (tmp_path / "src" / "my_cog").mkdir(parents=True)
    (tmp_path / "src" / "my_cog" / "main.py").write_text(
        "from prefect import flow\n@flow\ndef my_flow(): pass\nprefect.serve(my_flow)\n"
    )
    findings = check_prefect_serve_pattern(tmp_path)
    assert not any(f["severity"] == "ERROR" for f in findings)


# ── VER-008: .releaserc.json assets ──────────────────────────────────────────


def test_check_releaserc_assets_flags_missing_changelog(tmp_path: Path) -> None:
    (tmp_path / ".releaserc.json").write_text(
        '{"plugins":[["@semantic-release/git",{"assets":["pyproject.toml"]}]]}'
    )
    findings = check_releaserc_assets(tmp_path)
    assert any(
        f["rule_id"] == "VER-008" and "CHANGELOG" in f["finding"] for f in findings
    )


def test_check_releaserc_assets_flags_missing_managed_file(tmp_path: Path) -> None:
    (tmp_path / ".releaserc.json").write_text(
        '{"plugins":[["@semantic-release/exec",{"prepareCmd":"uv version ${nextRelease.version} pyproject.toml"}],["@semantic-release/git",{"assets":["CHANGELOG.md"]}]]}'
    )
    findings = check_releaserc_assets(tmp_path)
    assert any(
        f["rule_id"] == "VER-008" and "pyproject.toml" in f["finding"] for f in findings
    )


def test_check_releaserc_assets_passes_when_complete(tmp_path: Path) -> None:
    (tmp_path / ".releaserc.json").write_text(
        '{"plugins":[["@semantic-release/exec",{"prepareCmd":"uv version ${nextRelease.version} pyproject.toml"}],["@semantic-release/git",{"assets":["CHANGELOG.md","pyproject.toml"]}]]}'
    )
    assert check_releaserc_assets(tmp_path) == []


# ── XSTACK-003: pnpm lockfile ─────────────────────────────────────────────────


def test_check_pnpm_lockfile_flags_package_lock(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("{}")
    findings = check_pnpm_lockfile(tmp_path)
    assert any(
        f["rule_id"] == "XSTACK-003" and "package-lock" in f["finding"]
        for f in findings
    )


def test_check_pnpm_lockfile_passes_with_pnpm_lock(tmp_path: Path) -> None:
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    assert check_pnpm_lockfile(tmp_path) == []


# ── Previously untested functions ─────────────────────────────────────────────


def test_check_no_print_statements_flags_print(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "main.py").write_text("print('hello')\n")
    findings = check_no_print_statements(tmp_path)
    assert any(f["rule_id"] == "CD-003" for f in findings)


def test_check_no_print_statements_passes_clean(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "main.py").write_text("x = 1\n")
    assert check_no_print_statements(tmp_path) == []


def test_check_no_setup_py_flags_requirements_txt(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests\n")
    findings = check_no_setup_py(tmp_path)
    assert any(f["rule_id"] == "PY-007" for f in findings)


def test_check_releaserc_flags_missing(tmp_path: Path) -> None:
    findings = check_releaserc(tmp_path)
    assert any(f["rule_id"] == "VER-003" for f in findings)


def test_check_releaserc_passes_when_present(tmp_path: Path) -> None:
    (tmp_path / ".releaserc.json").write_text("{}\n")
    assert check_releaserc(tmp_path) == []


def test_check_pre_commit_flags_missing(tmp_path: Path) -> None:
    findings = check_pre_commit(tmp_path)
    assert any(f["rule_id"] == "PY-008" for f in findings)


def test_check_pre_commit_passes_when_present(tmp_path: Path) -> None:
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    assert check_pre_commit(tmp_path) == []


def test_check_no_hardcoded_urls_flags_railway_url(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "client.py").write_text(
        'BASE = "https://my-service.up.railway.app"\n'
    )
    findings = check_no_hardcoded_urls(tmp_path)
    assert any(f["rule_id"] == "FE-007" for f in findings)


def test_check_pytest_coverage_in_ci_flags_missing(tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n"
    )
    findings = check_pytest_coverage_in_ci(tmp_path)
    assert any(f["rule_id"] == "TEST-006" for f in findings)


def test_check_pipeline_cog_tests_flags_missing_normalization(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_basic.py").write_text("def test_ok(): assert True\n")
    findings = check_pipeline_cog_tests(tmp_path)
    assert any(f["rule_id"] == "TEST-001" for f in findings)


def test_check_no_retired_trigger_patterns_flags_repository_dispatch(
    tmp_path: Path,
) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "trigger.py").write_text(
        'url = "https://api.github.com/repos/org/repo/dispatches"\n'
        'payload = {"event_type": "repository_dispatch"}\n'
    )
    findings = check_no_retired_trigger_patterns(tmp_path)
    assert any(f["rule_id"] == "PIPE-008" for f in findings)


# ── XSTACK-003 wiring coverage ────────────────────────────────────────────────


def test_run_all_checks_xstack003_fires_for_hono_service(tmp_path: Path) -> None:
    """XSTACK-003 must fire for new_hono_service missing pnpm-lock.yaml."""
    (tmp_path / "package.json").write_text('{"name":"api"}\n')
    (tmp_path / "package-lock.json").write_text("{}")
    result = run_all_checks(
        tmp_path,
        language="typescript",
        dod_type="new_hono_service",
    )
    rule_ids = [f["rule_id"] for f in result.findings]
    assert "XSTACK-003" in rule_ids


def test_run_all_checks_xstack003_fires_for_react_app(tmp_path: Path) -> None:
    """XSTACK-003 must fire for new_react_app missing pnpm-lock.yaml."""
    (tmp_path / "package.json").write_text('{"name":"app"}\n')
    (tmp_path / "package-lock.json").write_text("{}")
    result = run_all_checks(
        tmp_path,
        language="typescript",
        dod_type="new_react_app",
    )
    rule_ids = [f["rule_id"] for f in result.findings]
    assert "XSTACK-003" in rule_ids


def test_run_all_checks_xstack003_does_not_fire_for_frontend_site(
    tmp_path: Path,
) -> None:
    """XSTACK-003 must not fire for new_frontend_site — static sites are exempt."""
    (tmp_path / "package.json").write_text('{"dependencies":{"astro":"4"}}\n')
    (tmp_path / "astro.config.mjs").write_text("export default {}\n")
    (tmp_path / "package-lock.json").write_text("{}")
    result = run_all_checks(
        tmp_path,
        language="typescript",
        service_type="site",
        dod_type="new_frontend_site",
    )
    rule_ids = [f["rule_id"] for f in result.findings]
    assert "XSTACK-003" not in rule_ids


# ── Previously untested functions ─────────────────────────────────────────────


def test_check_no_dead_code_flags_commented_code(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "main.py").write_text(
        "x = 1\n"
        "# def old_function():\n"
        "#     return x + 1\n"
        "#     if x > 0:\n"
        "#         pass\n"
    )
    findings = check_no_dead_code(tmp_path)
    assert any(f["rule_id"] == "DOC-008" for f in findings)


def test_check_no_dead_code_passes_clean(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "main.py").write_text(
        "# This is a normal comment\nx = 1\n"
    )
    assert check_no_dead_code(tmp_path) == []


def test_check_readme_running_locally_flags_missing_uv_sync(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# My Cog\nRun with python main.py\n",
    )
    findings = check_readme_running_locally(tmp_path, dod_type="new_cog")
    assert any("uv sync" in f["finding"] for f in findings)


def test_check_readme_running_locally_passes_for_cog(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# My Cog\n"
        "## Running locally\n"
        "Prerequisites: Python 3.11, uv\n"
        "Install: uv sync --all-extras\n"
        "Pre-commit: uv run pre-commit install && uv run pre-commit run --all-files\n"
        "Run: uv run python -m my_cog\n"
        "Test: uv run pytest\n",
    )
    findings = check_readme_running_locally(tmp_path, dod_type="new_cog")
    assert findings == []


def test_check_split_package_identity_passes_when_names_match(tmp_path: Path) -> None:
    """No finding when install name and src package name match."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-cog"\n')
    (tmp_path / "src" / "my_cog").mkdir(parents=True)
    (tmp_path / "src" / "my_cog" / "__init__.py").write_text("")
    findings = check_split_package_identity(tmp_path)
    assert findings == []


def test_check_split_package_identity_flags_undocumented_split(tmp_path: Path) -> None:
    """Finding when install name differs from src package and neither doc mentions both."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "common-python-utils"\n'
    )
    (tmp_path / "src" / "mini_app_polis").mkdir(parents=True)
    (tmp_path / "src" / "mini_app_polis" / "__init__.py").write_text(
        '"""Package."""\n',
    )
    (tmp_path / "README.md").write_text(
        "# common-python-utils\nInstall this package.\n"
    )
    findings = check_split_package_identity(tmp_path)
    assert any(f["rule_id"] == "DOC-009" for f in findings)


# ── Monorepo-aware check tests ─────────────────────────────────────────────


def test_check_shared_library_used_ts_workspace_dep_satisfies(tmp_path: Path) -> None:
    """XSTACK-001 not flagged when dep is in workspace root package.json."""
    app_pkg = tmp_path / "package.json"
    app_pkg.write_text('{"name": "app", "dependencies": {}}')
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.ts").write_text(
        'import { createLogger } from "common-typescript-utils"'
    )

    workspace_text = '{"dependencies": {"common-typescript-utils": "^1.0.0"}}'

    findings = check_shared_library_used(
        tmp_path,
        language="typescript",
        workspace_package_json_text=workspace_text,
    )
    rule_ids = [f["rule_id"] for f in findings]
    assert "XSTACK-001" not in rule_ids, (
        "XSTACK-001 should not be flagged when dep is in workspace root (MONO-001)"
    )


def test_check_shared_library_used_ts_flags_when_absent_everywhere(
    tmp_path: Path,
) -> None:
    """XSTACK-001 flagged when dep absent from both app and workspace."""
    app_pkg = tmp_path / "package.json"
    app_pkg.write_text('{"name": "app", "dependencies": {}}')
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.ts").write_text("// no shared lib import")

    findings = check_shared_library_used(
        tmp_path,
        language="typescript",
        workspace_package_json_text='{"dependencies": {}}',
    )
    rule_ids = [f["rule_id"] for f in findings]
    assert "XSTACK-001" in rule_ids


def test_check_pnpm_lockfile_uses_monorepo_root(tmp_path: Path) -> None:
    """XSTACK-003 checks monorepo root when monorepo_root provided."""
    app_dir = tmp_path / "apps" / "api"
    app_dir.mkdir(parents=True)

    (tmp_path / "pnpm-lock.yaml").write_text("")

    findings = check_pnpm_lockfile(app_dir, monorepo_root=tmp_path)
    assert not any("pnpm-lock.yaml not found" in f.get("finding", "") for f in findings)


def test_deduplication_collapses_sibling_findings() -> None:
    """Identical findings across siblings are collapsed into primary."""
    shared_finding = {
        "rule_id": "XSTACK-001",
        "severity": "ERROR",
        "dimension": "cross_repo_coherence",
        "finding": "common-typescript-utils is not declared for this TypeScript service.",
        "suggestion": "Depend on common-typescript-utils.",
    }
    unique_finding = {
        "rule_id": "FE-002",
        "severity": "ERROR",
        "dimension": "structural_conformance",
        "finding": "TypeScript setup missing for React web app.",
        "suggestion": "Add TypeScript dependency.",
    }

    findings_by_service = {
        "deejaytools-com-api": [dict(shared_finding)],
        "deejaytools-com-app": [dict(shared_finding), dict(unique_finding)],
    }

    result = _deduplicate_sibling_findings(findings_by_service)

    primary = result["deejaytools-com-api"]
    assert len(primary) == 1
    assert "also affects deejaytools-com-app" in primary[0]["finding"]

    sibling = result["deejaytools-com-app"]
    assert len(sibling) == 1
    assert sibling[0]["rule_id"] == "FE-002"


# ── Category A: trigger cog exclusion ─────────────────────────────────────


def test_trigger_cog_skips_pipe008(tmp_path: Path) -> None:
    """PIPE-008 must not fire on trigger cogs (cog_subtype=trigger)."""
    src = tmp_path / "src" / "watcher_cog"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        "# uses repository_dispatch pattern\nrepository_dispatch = True\n"
    )

    result = run_all_checks(
        tmp_path,
        language="python",
        dod_type="new_cog",
        cog_subtype="trigger",
    )
    rule_ids = [f["rule_id"] for f in result.findings]
    assert "PIPE-008" not in rule_ids, "PIPE-008 must not fire on trigger cogs"


def test_trigger_cog_skips_pipe009_evaluation_step(tmp_path: Path) -> None:
    """PIPE-009 (evaluation step) must not fire on trigger cogs."""
    src = tmp_path / "src" / "watcher_cog"
    src.mkdir(parents=True)
    (src / "main.py").write_text("# trigger cog — no pipeline\n")

    result = run_all_checks(
        tmp_path,
        language="python",
        dod_type="new_cog",
        cog_subtype="trigger",
    )
    rule_ids = [f["rule_id"] for f in result.findings]
    assert "PIPE-009" not in rule_ids, "PIPE-009 must not fire on trigger cogs"


def test_retired_pattern_in_tests_not_flagged(tmp_path: Path) -> None:
    """Retired trigger patterns in tests/ must not trigger PIPE-008."""
    src = tmp_path / "src" / "my_cog"
    src.mkdir(parents=True)
    (src / "main.py").write_text("# clean source\n")

    tests = tmp_path / "tests" / "my_cog"
    tests.mkdir(parents=True)
    (tests / "test_trigger.py").write_text(
        'def test_retired():\n    pattern = "repository_dispatch"\n    assert pattern in old_config\n'
    )

    result = run_all_checks(
        tmp_path,
        language="python",
        dod_type="new_cog",
        cog_subtype="pipeline",
    )
    rule_ids = [f["rule_id"] for f in result.findings]
    assert "PIPE-008" not in rule_ids, (
        "PIPE-008 must not fire for retired patterns in test files"
    )


# ── Category B: same-repo deduplication ───────────────────────────────────


def test_cd010_supersedes_cd002_and_cd009() -> None:
    """When CD-010 fires, CD-002 and CD-009 findings should be dropped."""
    raw_findings: list[Finding] = [
        {
            "rule_id": "CD-010",
            "severity": "ERROR",
            "dimension": "cd_readiness",
            "finding": "Three-layer observability stack is absent.",
            "suggestion": "",
        },
        {
            "rule_id": "CD-002",
            "severity": "WARN",
            "dimension": "cd_readiness",
            "finding": "sentry-sdk not found in pyproject.toml.",
            "suggestion": "",
        },
        {
            "rule_id": "CD-009",
            "severity": "WARN",
            "dimension": "cd_readiness",
            "finding": "Hand-rolled logging detected.",
            "suggestion": "",
        },
        {
            "rule_id": "DOC-001",
            "severity": "ERROR",
            "dimension": "documentation_coverage",
            "finding": "README.md is absent.",
            "suggestion": "",
        },
    ]
    result = _deduplicate_same_repo_findings(raw_findings)
    rule_ids = [f["rule_id"] for f in result]
    assert "CD-010" in rule_ids, "CD-010 should be retained"
    assert "CD-002" not in rule_ids, "CD-002 should be dropped when CD-010 fires"
    assert "CD-009" not in rule_ids, "CD-009 should be dropped when CD-010 fires"
    assert "DOC-001" in rule_ids, "Unrelated findings should be retained"


def test_cd002_not_dropped_without_cd010() -> None:
    """CD-002 should NOT be dropped if CD-010 did not fire."""
    raw_findings: list[Finding] = [
        {
            "rule_id": "CD-002",
            "severity": "WARN",
            "dimension": "cd_readiness",
            "finding": "sentry-sdk not found.",
            "suggestion": "",
        },
    ]
    result = _deduplicate_same_repo_findings(raw_findings)
    assert len(result) == 1
    assert result[0]["rule_id"] == "CD-002"


# ── Category C: monorepo root file fallback ───────────────────────────────


def test_check_readme_finds_at_monorepo_root(tmp_path: Path) -> None:
    """check_readme should not flag absence if README exists at monorepo root."""
    app_dir = tmp_path / "apps" / "api"
    app_dir.mkdir(parents=True)
    monorepo_root = tmp_path
    (monorepo_root / "README.md").write_text("# Monorepo README")

    findings = check_readme(app_dir, monorepo_root=monorepo_root)
    assert not findings, "README at monorepo root should satisfy DOC-001"


def test_check_changelog_finds_at_monorepo_root(tmp_path: Path) -> None:
    """check_changelog should not flag absence if CHANGELOG exists at monorepo root."""
    app_dir = tmp_path / "apps" / "api"
    app_dir.mkdir(parents=True)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog")

    findings = check_changelog(app_dir, monorepo_root=tmp_path)
    assert not findings, "CHANGELOG at monorepo root should satisfy DOC-003"


def test_check_releaserc_finds_at_monorepo_root(tmp_path: Path) -> None:
    """check_releaserc should not flag absence if .releaserc.json exists at monorepo root."""
    app_dir = tmp_path / "apps" / "api"
    app_dir.mkdir(parents=True)
    (tmp_path / ".releaserc.json").write_text('{"branches": ["main"]}')

    findings = check_releaserc(app_dir, monorepo_root=tmp_path)
    assert not findings, ".releaserc.json at monorepo root should satisfy VER-003"


def test_check_readme_flags_absent_from_both(tmp_path: Path) -> None:
    """check_readme should flag if README absent from BOTH app dir and monorepo root."""
    app_dir = tmp_path / "apps" / "api"
    app_dir.mkdir(parents=True)
    monorepo_root = tmp_path

    findings = check_readme(app_dir, monorepo_root=monorepo_root)
    assert any(f["rule_id"] == "DOC-001" for f in findings)
