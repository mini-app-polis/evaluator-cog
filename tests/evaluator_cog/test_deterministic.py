"""Smoke tests for the deterministic conformance engine."""

import tempfile
from pathlib import Path

from evaluator_cog.engine.deterministic import (
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
    check_no_hardcoded_secrets,
    check_no_manual_changelog,
    check_pyproject,
    check_react_hook_form_zod,
    check_readme,
    check_readme_io,
    check_respx_for_http_mocking,
    check_retry_logic,
    check_shadcn,
    check_shared_library_used,
    check_src_layout,
    check_structured_logging,
    check_tailwind,
    check_test_structure,
    check_vite_react_ts,
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


def test_check_shared_library_ts_passes_with_dependency_and_import() -> None:
    repo = _make_repo(
        {
            "package.json": '{"name":"x","dependencies":{"kaiano-ts-utils":"1.0.0"}}\n',
            "src/index.ts": "import { createLogger } from 'kaiano-ts-utils'\n",
        }
    )
    assert check_shared_library_used(repo, language="typescript") == []


def test_run_all_checks_frontend_wires_new_frontend_rules() -> None:
    repo = _make_repo({"package.json": '{"dependencies":{"react":"18"}}\n'})
    findings = run_all_checks(
        repo, language="typescript", service_type="site", dod_type="new_frontend_site"
    )
    rule_ids = [f["rule_id"] for f in findings]
    assert "FE-001" in rule_ids
    assert "FE-003" in rule_ids
