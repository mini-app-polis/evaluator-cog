"""Smoke tests for the deterministic conformance engine."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from evaluator_cog.engine.deterministic import (
    Finding,
    _deduplicate_same_repo_findings,
    _type_to_dod,
    check_astro_framework,
    check_changelog,
    check_ci,
    check_common_python_utils_dep,
    check_dedup_handling_present,
    check_env_example,
    check_eval_003,
    check_eval_007,
    check_evaluation_step,
    check_failed_prefix,
    check_healthchecks_integration,
    check_mono_003,
    check_mypy_in_ci,
    check_naming_conventions,
    check_no_hardcoded_secrets,
    check_no_hardcoded_urls,
    check_no_manual_changelog,
    check_no_print_statements,
    check_no_retired_trigger_patterns,
    check_no_setup_py,
    check_pnpm_lockfile,
    check_pre_commit,
    check_prefect_serve_pattern,
    check_pyproject,
    check_pytest_config,
    check_pytest_coverage_in_ci,
    check_react_hook_form_zod,
    check_readme,
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
    check_vite_react_ts,
    run_all_checks,
)
from evaluator_cog.engine.evaluator_config import EvaluatorConfig
from evaluator_cog.flows.conformance import _deduplicate_sibling_findings

_TEST_RULE_CATALOG: dict[str, dict] = {
    "DOC-001": {"applies_to": ["all"], "modifies": [], "status": "requirement"},
    "CD-009": {
        "applies_to": ["pipeline-cog", "trigger-cog", "api-service", "react-app"],
        "modifies": [],
        "status": "requirement",
    },
    "CD-002": {
        "applies_to": ["pipeline-cog", "trigger-cog", "api-service", "react-app"],
        "modifies": [],
        "status": "requirement",
    },
    "CD-015": {
        "applies_to": ["pipeline-cog", "trigger-cog"],
        "modifies": [],
        "status": "requirement",
    },
    "TEST-001": {
        "applies_to": ["pipeline-cog", "api-service"],
        "modifies": [],
        "status": "requirement",
    },
    "PY-006": {
        "applies_to": ["pipeline-cog", "api-service", "shared-library"],
        "modifies": [],
        "status": "requirement",
    },
    "PIPE-008": {
        "applies_to": ["pipeline-cog"],
        "modifies": [],
        "status": "requirement",
    },
    "PIPE-009": {
        "applies_to": ["pipeline-cog"],
        "modifies": [],
        "status": "requirement",
    },
    "PIPE-011": {
        "applies_to": ["pipeline-cog"],
        "modifies": [],
        "status": "requirement",
    },
}

_TEST_CATALOG_SCHEMA: dict = {
    "traits": {
        "logger-primitive": {
            "description": "is the logger",
            "exempts": ["CD-009"],
            "downgrades": [],
        },
        "pipeline-cog-evaluator": {
            "description": "self evaluator",
            "exempts": ["PIPE-011"],
            "downgrades": [],
        },
    }
}


def _attach_catalog(cfg: EvaluatorConfig) -> EvaluatorConfig:
    cfg.rule_catalog = _TEST_RULE_CATALOG
    cfg.catalog_schema = _TEST_CATALOG_SCHEMA
    return cfg


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


def test_run_all_checks_structured_exception_emits_no_finding() -> None:
    """Structured check_exceptions (legacy path, no catalog) should
    suppress the check entirely — no INFO 'Skipped:' finding is
    emitted. A rule that does not apply to this repo is simply absent
    from the report. INFO remains reserved for genuine weak-signal
    findings from checks that actually ran.
    """
    repo = _make_repo({})
    result = run_all_checks(
        repo,
        check_exceptions=["DOC-001"],
        exception_reasons={"DOC-001": "standards repo — no service entry point"},
        dod_type="new_cog",
    )
    findings = result.findings
    doc_001_findings = [f for f in findings if f["rule_id"] == "DOC-001"]
    assert doc_001_findings == [], (
        f"DOC-001 should be fully suppressed by check_exceptions, got: "
        f"{doc_001_findings}"
    )


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
    assert "TEST-001" not in pipeline_rule_ids


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


def test_check_dedup_handling_present_passes_when_signal_exists() -> None:
    """Repo source containing any dedup signal passes PY-013."""
    # File-level pattern (deejay-cog style).
    file_level = _make_repo(
        {
            "src/my_pkg/archive.py": (
                "def archive(path):\n"
                "    # rename with possible_duplicate_ prefix on collision\n"
                "    return path\n"
            )
        }
    )
    assert check_dedup_handling_present(file_level) == []

    # Row-level pattern (transcription-cog/process-transcript style;
    # historically called the notes-ingest-cog pattern pre-merge).
    row_level = _make_repo(
        {
            "src/my_pkg/ingest.py": (
                "def ingest(row):\n"
                "    # server rejects via unique constraint; we skip\n"
                "    try:\n"
                "        store(row)\n"
                "    except IntegrityError:\n"
                "        return 'already processed'\n"
            )
        }
    )
    assert check_dedup_handling_present(row_level) == []


def test_check_dedup_handling_present_flags_when_no_signal() -> None:
    """Repo source containing no dedup signals fires PY-013."""
    repo = _make_repo(
        {"src/my_pkg/transform.py": ("def transform(x):\n    return x * 2\n")}
    )
    findings = check_dedup_handling_present(repo)
    assert any(f["rule_id"] == "PY-013" for f in findings)


def test_check_dedup_handling_present_no_src_dir() -> None:
    """Repo without a src/ directory emits no findings."""
    repo = _make_repo({"README.md": "# empty"})
    assert check_dedup_handling_present(repo) == []


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


def test_check_healthchecks_integration_passes_evaluator_suffix() -> None:
    repo = _make_repo(
        {
            ".env.example": "HEALTHCHECKS_URL_EVALUATOR=\n",
            "src/my_pkg/heartbeat.py": 'url = os.getenv("HEALTHCHECKS_URL_EVALUATOR", "")\n',
        }
    )
    assert check_healthchecks_integration(repo, cog_subtype="trigger") == []


def test_check_healthchecks_integration_passes_watcher_suffix() -> None:
    repo = _make_repo(
        {
            ".env.example": "HEALTHCHECKS_URL_WATCHER=\n",
            "src/my_pkg/heartbeat.py": 'url = os.getenv("HEALTHCHECKS_URL_WATCHER", "")\n',
        }
    )
    assert check_healthchecks_integration(repo, cog_subtype="trigger") == []


def test_check_healthchecks_integration_passes_healthchecks_in_src() -> None:
    """src reference via 'healthchecks' keyword (case-insensitive) is sufficient."""
    repo = _make_repo(
        {
            ".env.example": "HEALTHCHECKS_URL_CUSTOM=\n",
            "src/my_pkg/heartbeat.py": "import healthchecks\n",
        }
    )
    assert check_healthchecks_integration(repo, cog_subtype="trigger") == []


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


def test_check_shared_library_ts_hand_rolled_ok_when_dep_declared() -> None:
    """Narrowed XSTACK-001: hand-rolled helpers no longer fire — only missing deps."""
    repo = _make_repo(
        {
            "package.json": '{"name":"x","dependencies":{"common-typescript-utils":"1.0.0"}}\n',
            "src/index.ts": "function createLogger(){ return console }\n",
        }
    )
    assert check_shared_library_used(repo, language="typescript") == []


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


def test_check_pytest_config_flags_missing(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    findings = check_pytest_config(tmp_path)
    assert any(f["rule_id"] == "TEST-005" for f in findings)


def test_check_pytest_config_passes_when_present(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n[tool.pytest.ini_options]\n"
    )
    assert check_pytest_config(tmp_path) == []


def test_check_pytest_config_no_pyproject_is_silent(tmp_path: Path) -> None:
    assert check_pytest_config(tmp_path) == []


def test_check_no_retired_trigger_patterns_flags_repository_dispatch(
    tmp_path: Path,
) -> None:
    # Under the narrowed PIPE-008 spec (post-2026-04 audit), a bare URL
    # string no longer trips the check — it must be an active HTTP call
    # to the retired dispatches endpoint.
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "trigger.py").write_text(
        "import httpx\n"
        "def trigger_remote():\n"
        "    httpx.post('https://api.github.com/repos/org/repo/dispatches', json={})\n"
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


# ── Evaluator.yaml / EvaluatorConfig integration ────────────────────────────


def _write_evaluator_yaml(root: Path, content: str = "type: pipeline-cog\n") -> None:
    (root / "evaluator.yaml").write_text(content)


def test_eval008_when_evaluator_yaml_absent(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# x\n")
    result = run_all_checks(tmp_path, language="python", dod_type="new_cog")
    assert any(f.get("rule_id") == "EVAL-008" for f in result.findings)


def test_eval008_not_emitted_when_evaluator_yaml_present(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# x\n")
    _write_evaluator_yaml(tmp_path)
    result = run_all_checks(tmp_path, language="python", dod_type="new_cog")
    assert not any(f.get("rule_id") == "EVAL-008" for f in result.findings)


def test_deferred_finding_has_info_and_flag(tmp_path: Path) -> None:
    """Deferrals downgrade severity to INFO and set deferred=True on findings."""
    cfg = _attach_catalog(
        EvaluatorConfig(
            repo_type="pipeline-cog",
            deferral_ids=["DOC-001"],
            deferral_reasons={"DOC-001": "scheduled"},
        )
    )
    # No README → DOC-001 would normally ERROR/WARN from check_readme
    result = run_all_checks(
        tmp_path,
        language="python",
        evaluator_config=cfg,
    )
    doc = [f for f in result.findings if f.get("rule_id") == "DOC-001"]
    assert doc, "expected a DOC-001 finding"
    assert all(f.get("severity") == "INFO" for f in doc)
    assert all(f.get("deferred") is True for f in doc)


def test_evaluator_config_trigger_cog_skips_pipeline_rules(tmp_path: Path) -> None:
    cfg = _attach_catalog(EvaluatorConfig(repo_type="trigger-cog"))
    src = tmp_path / "src" / "watcher"
    src.mkdir(parents=True)
    (src / "main.py").write_text("repository_dispatch = True\n")
    _write_evaluator_yaml(tmp_path)
    result = run_all_checks(
        tmp_path,
        language="python",
        evaluator_config=cfg,
    )
    pipe = {"PIPE-008", "PIPE-009", "PIPE-011"}
    hit = {f["rule_id"] for f in result.findings} & pipe
    assert not hit, f"pipeline rules should not fire for trigger-cog: {hit}"


def test_evaluator_config_shared_library_no_cd002_test001_py006_violations(
    tmp_path: Path,
) -> None:
    """Catalog-derived skips prevent WARN/ERROR on rules not applicable to shared-library."""
    cfg = _attach_catalog(EvaluatorConfig(repo_type="shared-library"))
    _write_evaluator_yaml(tmp_path)
    (tmp_path / "README.md").write_text("# lib\n")
    (tmp_path / "CHANGELOG.md").write_text("# c\n")
    (tmp_path / ".releaserc.json").write_text("{}")
    src = tmp_path / "src" / "mylib"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    # Deliberately omit sentry-sdk — would be CD-002 without the exception
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0.1.0"\nrequires-python=">=3.11"\n'
        'dependencies = []\n[build-system]\nrequires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n[tool.uv]\n[tool.ruff]\nline-length = 88\n'
    )
    result = run_all_checks(
        tmp_path,
        language="python",
        evaluator_config=cfg,
    )
    bad = {
        f["rule_id"]
        for f in result.findings
        if f["rule_id"] in ("CD-002", "TEST-001")
        and f.get("severity") in ("WARN", "ERROR")
    }
    assert not bad, f"unexpected violations: {bad}"


def test_evaluator_config_pipeline_logger_primitive_skips_cd009_violation(
    tmp_path: Path,
) -> None:
    cfg = _attach_catalog(
        EvaluatorConfig(
            repo_type="pipeline-cog",
            traits=["logger-primitive"],
        )
    )
    _write_evaluator_yaml(tmp_path)
    (tmp_path / "README.md").write_text("# p\ninput\noutput\n")
    (tmp_path / "CHANGELOG.md").write_text("# c\n")
    (tmp_path / ".releaserc.json").write_text("{}")
    src = tmp_path / "src" / "pipe"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        "import logging\nlogging.getLogger(__name__).info('x')\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0.1.0"\nrequires-python=">=3.11"\n'
        'dependencies = ["sentry-sdk"]\n[build-system]\nrequires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n[tool.uv]\n[tool.ruff]\nline-length = 88\n'
    )
    result = run_all_checks(
        tmp_path,
        language="python",
        evaluator_config=cfg,
    )
    cd009_bad = [
        f
        for f in result.findings
        if f.get("rule_id") == "CD-009" and f.get("severity") in ("WARN", "ERROR")
    ]
    assert not cd009_bad


def test_check_eval_003_flags_untagged_finding() -> None:
    """A finding with no rule_id field flags EVAL-003.

    Ground truth for "is this finding attributed to a rule?" is the
    rule_id column, not a regex match on the finding text. A finding
    emitted without rule_id set (or with the CHECKER sentinel) is an
    orphan — no rule owns it.
    """
    fake_response = {
        "data": [
            {
                "id": 1,
                "finding": "Something is wrong but we won't say what rule",
                "severity": "WARN",
                "suggestion": "A sufficiently long remediation string that passes the length check easily.",
                "standards_version": "4.0.0",
                "run_id": "x",
                # rule_id intentionally absent
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_eval_003()

    assert any(
        f["rule_id"] == "EVAL-003" and "not tagged with a rule_id" in f["finding"]
        for f in findings
    )


def test_check_eval_003_accepts_tagged_finding_without_rule_id_in_text() -> None:
    """A finding with rule_id set on the row does NOT flag EVAL-003,
    even if the finding text does not repeat the rule ID inline.

    Regression guard for the 2026-04 triage: EVAL-003 was previously
    regex-matching the finding text for a rule-ID pattern, which fired
    on correctly-tagged findings like
    "docs/decisions/ exists but no ADR-NNN-*.md files were found."
    (rule_id="DOC-005"). The check now looks at the rule_id field
    directly, so these pass.
    """
    fake_response = {
        "data": [
            {
                "id": 100,
                "rule_id": "DOC-005",
                "finding": "docs/decisions/ exists but no ADR-NNN-*.md files were found.",
                "severity": "WARN",
                "suggestion": "Create a first ADR or remove the empty docs/decisions directory.",
                "standards_version": "4.1.0",
                "run_id": "x",
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_eval_003()

    assert findings == []


def test_check_eval_003_skips_checker_infrastructure_errors() -> None:
    """CHECKER-sentinel findings (emitted when a check itself raised) are
    infrastructure errors with no owning rule by design. EVAL-003 should
    not grade them as orphans."""
    fake_response = {
        "data": [
            {
                "id": 200,
                "rule_id": "CHECKER",
                "finding": "Check check_foo raised an unexpected error: boom",
                "severity": "WARN",
                "suggestion": "Investigate the checker itself.",
                "standards_version": "4.1.0",
                "run_id": "x",
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_eval_003()

    assert findings == []


def test_check_eval_003_accepts_short_but_clear_finding() -> None:
    """Short-but-actionable findings should not be flagged for length alone.

    The legacy ≤60-char length check was removed in 2026-04. A concise
    finding like "PY-001 bad" still isn't flagged for length (even though
    it's arguably too terse to be useful — we accept that trade-off to
    avoid false positives on legitimate short findings like "Layer 1
    missing: no HEALTHCHECKS_URL env var or healthchecks.io ping").
    Remediation coverage (below) still catches under-specified findings.
    """
    fake_response = {
        "data": [
            {
                "id": 2,
                "finding": "PY-001 bad",  # has rule id
                "severity": "WARN",
                "suggestion": "A sufficiently long remediation string here.",
                "standards_version": "4.0.0",
                "run_id": "x",
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_eval_003()

    assert not any("too short" in f["finding"] for f in findings)
    assert not any("finding_text too short" in f["finding"] for f in findings)


def test_check_eval_003_skips_info_severity_dispatcher_findings() -> None:
    """INFO-severity rows (dispatcher meta) are not graded.

    Skipped: / deferred / downgraded notes emitted by the deterministic
    dispatcher carry severity INFO and are intentionally short,
    rule-ID-free, and remediation-free. Regression guard for the
    2026-04 Introspection noise spike.
    """
    fake_response = {
        "data": [
            {
                "id": 10,
                "finding": "Skipped: Exempted by trait: logger-primitive",
                "severity": "INFO",
                "suggestion": "",
                "standards_version": "4.1.0",
                "run_id": "x",
            },
            {
                "id": 11,
                "finding": "Run completed successfully.",
                "severity": "SUCCESS",
                "suggestion": "",
                "standards_version": "4.1.0",
                "run_id": "x",
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_eval_003()

    assert findings == []


def test_check_eval_003_skips_skipped_prefix_even_at_warn() -> None:
    """Belt-and-suspenders: "Skipped:"-prefixed rows are dropped even if
    severity somehow arrives as WARN (defensive — catches future
    dispatcher bugs where a skip note leaks through with the wrong
    severity)."""
    fake_response = {
        "data": [
            {
                "id": 20,
                "finding": "Skipped: Evaluator false positive — check_foo mismatch",
                "severity": "WARN",
                "suggestion": "",
                "standards_version": "4.1.0",
                "run_id": "x",
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_eval_003()

    assert findings == []


def test_check_eval_003_does_not_grade_its_own_prior_emissions() -> None:
    """EVAL-003 must not recursively grade its own stored findings."""
    fake_response = {
        "data": [
            {
                "id": 30,
                "rule_id": "EVAL-003",
                "finding": "Finding xyz violates EVAL-003: no rule ID reference",
                "severity": "WARN",
                "suggestion": "",
                "standards_version": "4.1.0",
                "run_id": "x",
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_eval_003()

    assert findings == []


def test_check_eval_003_passes_well_formed_finding() -> None:
    fake_response = {
        "data": [
            {
                "id": 3,
                "rule_id": "PY-011",
                "finding": "PY-011: Module names in the snake_case directory use camelCase — inconsistent with PEP 8 and the repo's convention.",
                "severity": "WARN",
                "suggestion": "Rename the affected modules to snake_case per PY-011. Update imports across the codebase.",
                "standards_version": "4.0.0",
                "run_id": "x",
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_eval_003()

    assert findings == []


def test_check_eval_003_accepts_concise_but_actionable_remediation() -> None:
    """Long findings with concise actionable remediation should pass.

    Regression guard for false positives where EVAL-003 required
    remediation length to be >=50% of finding length.
    """
    fake_response = {
        "data": [
            {
                "id": "cc10cd52-ff5e-4808-9ef2-440bb0a1e17c",
                "rule_id": "EVAL-007",
                "finding": (
                    "Rule CD-004 is a deterministic checkable rule in the catalog "
                    "but is not registered in evaluator-cog's deterministic package "
                    "(no CHECK_ID constant, _run() call, or _mark_checked() "
                    "reference found in any module under "
                    "evaluator_cog/engine/deterministic/)."
                ),
                "severity": "WARN",
                "suggestion": (
                    "Add a check function for CD-004 and register it via "
                    '_run(check_fn, "CD-004") or declare CHECK_ID = "CD-004".'
                ),
                "standards_version": "4.1.0",
                "run_id": "x",
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_eval_003()

    assert findings == []


def test_check_eval_003_flags_trivially_short_remediation() -> None:
    """Very short remediation text should still be flagged."""
    fake_response = {
        "data": [
            {
                "id": "short-remediation-row",
                "rule_id": "API-004",
                "finding": "API routes are missing the required /v1 prefix in several endpoints.",
                "severity": "WARN",
                "suggestion": "Fix this.",
                "standards_version": "4.1.0",
                "run_id": "x",
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_eval_003()

    assert any("remediation too short" in f["finding"] for f in findings)


def test_check_mono_003_flags_duplicate_sibling_findings() -> None:
    ecosystem = {
        "services": [
            {"id": "deejaytools-com-api", "monorepo": "deejaytools-com"},
            {"id": "deejaytools-com-app", "monorepo": "deejaytools-com"},
        ]
    }

    fake_response = {
        "data": [
            {
                "repo": "deejaytools-com-api",
                "rule_id": "XSTACK-001",
                "finding": "common-typescript-utils not declared",
                "standards_version": "4.0.0",
                "run_id": "r1",
            },
            {
                "repo": "deejaytools-com-app",
                "rule_id": "XSTACK-001",
                "finding": "common-typescript-utils not declared",
                "standards_version": "4.0.0",
                "run_id": "r1",
            },
        ],
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_mono_003(ecosystem=ecosystem)

    assert any(f["rule_id"] == "MONO-003" for f in findings)
    assert any("2 duplicate" in f["finding"] for f in findings)


def test_check_mono_003_ignores_single_sibling_findings() -> None:
    ecosystem = {
        "services": [
            {"id": "deejaytools-com-api", "monorepo": "deejaytools-com"},
        ]
    }
    fake_response = {
        "data": [
            {
                "repo": "deejaytools-com-api",
                "rule_id": "XSTACK-001",
                "finding": "x",
                "standards_version": "4.0.0",
                "run_id": "r1",
            }
        ]
    }

    class FakeApi:
        @staticmethod
        def from_env(machine_name=None):  # noqa: ARG004 - matches the real signature
            return FakeApi()

        def get(self, _path: str):
            return fake_response

    with patch("mini_app_polis.api.KaianoApiClient", FakeApi):
        findings = check_mono_003(ecosystem=ecosystem)

    assert findings == []


def test_check_eval_007_flags_unimplemented_rules() -> None:
    """A rule in the catalog with no matching CHECK_ID constant fires EVAL-007."""
    catalog = {
        "MADE-UP-999": {"applies_to": ["all"], "modifies": [], "status": "requirement"},
    }
    findings = check_eval_007(rule_catalog=catalog)
    assert any(
        f["rule_id"] == "EVAL-007"
        and "MADE-UP-999" in f["finding"]
        and "not registered" in f["finding"]
        for f in findings
    )


def test_check_eval_007_skips_llm_routed_rules() -> None:
    """LLM-routed rules are handled in engine/llm.py and must not be
    flagged as unimplemented by EVAL-007 just because they have no
    deterministic CHECK_ID constant."""
    catalog = {
        "LLM-ONLY-001": {
            "applies_to": ["all"],
            "modifies": [],
            "status": "requirement",
            "check_mode": "llm",
        },
        "DET-MISSING-002": {
            "applies_to": ["all"],
            "modifies": [],
            "status": "requirement",
            "check_mode": "deterministic",
        },
    }
    findings = check_eval_007(rule_catalog=catalog)
    # LLM rule must not appear as unimplemented
    assert not any("LLM-ONLY-001" in f["finding"] for f in findings)
    # Deterministic rule without CHECK_ID must still be flagged
    assert any("DET-MISSING-002" in f["finding"] for f in findings)


def test_check_eval_007_no_catalog_returns_empty() -> None:
    assert check_eval_007(rule_catalog=None) == []
    assert check_eval_007(rule_catalog={}) == []


def test_check_eval_007_flags_major_version_skew() -> None:
    catalog = {
        "DOC-001": {"applies_to": ["all"], "modifies": [], "status": "requirement"}
    }
    findings = check_eval_007(
        rule_catalog=catalog,
        current_standards_version="5.0.0",
        evaluator_standards_version="4.3.1",
    )
    assert any("major version skew" in f["finding"] for f in findings)


def test_check_eval_007_scans_whole_deterministic_package() -> None:
    """Regression test for the post-split scan bug.

    Prior to the deterministic.py → package split, check_eval_007 called
    inspect.getsource on its own module, which read only introspection.py.
    After the split, this collected just the three CHECK_IDs that live in
    introspection.py (EVAL-003, MONO-003, EVAL-007) and falsely flagged
    every rule implemented elsewhere in the package as unimplemented —
    producing ~100 false-positive drift findings per run.

    This test seeds the catalog with rules registered via each of the
    three implementation patterns, across multiple files, and asserts
    none are flagged as unimplemented:
      - CHECK_ID constant in python.py (PY-001, PY-008)
      - CHECK_ID constant in api.py (API-001)
      - _run() call in runner.py (API-004)
      - _mark_checked() call in runner.py (PY-002, VER-005, CD-002)
    """
    catalog = {
        rid: {
            "applies_to": ["all"],
            "modifies": [],
            "status": "requirement",
            "check_mode": "deterministic",
        }
        for rid in (
            "PY-001",
            "PY-008",
            "PY-002",
            "API-001",
            "API-004",
            "VER-005",
            "CD-002",
        )
    }
    findings = check_eval_007(rule_catalog=catalog)
    unimplemented = [
        f for f in findings if "not registered" in str(f.get("finding", ""))
    ]
    assert unimplemented == [], (
        "Expected all seeded rules to be detected as implemented "
        "across the deterministic package; got false drift for: "
        f"{[f['finding'][:80] for f in unimplemented]}"
    )


@pytest.mark.parametrize(
    ("repo_type", "language", "expected"),
    [
        ("pipeline-cog", "python", "new_cog"),
        ("trigger-cog", "python", "new_cog"),
        ("api-service", "python", "new_fastapi_service"),
        ("api-service", "typescript", "new_hono_service"),
        ("shared-library", "python", None),
        ("static-site", "typescript", "new_frontend_site"),
        ("react-app", "typescript", "new_react_app"),
        ("standards-repo", "python", None),
    ],
)
def test_type_to_dod_mapping(
    repo_type: str,
    language: str,
    expected: str | None,
) -> None:
    assert _type_to_dod(repo_type, language) == expected


def test_cd015_accepts_serve_with_retry(tmp_path: Path) -> None:
    """Clause (c) of the rule, which the check never implemented.

    CD-016 requires registration through serve_with_retry; CD-015 was
    only looking for prefect.serve. A cog that took CD-016's advice
    collected a CD-015 WARN for it — the two rules contradicted each
    other in practice.
    """
    src = tmp_path / "src/pkg"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        "from mini_app_polis.serve_resilience import serve_with_retry\n"
        "\n"
        "def main():\n"
        "    serve_with_retry(thing, repo='pkg')\n"
    )
    assert check_prefect_serve_pattern(tmp_path) == []


def test_cd015_does_not_pass_on_a_comment_about_serve(tmp_path: Path) -> None:
    """A docstring explaining why the repo does NOT call it is not a call.

    evaluator-cog and deejay-cog were both passing on the string
    "prefect.serve(" appearing in prose, while a repo that registered
    correctly through serve_with_retry failed.
    """
    src = tmp_path / "src/pkg"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        '"""This cog does not use prefect.serve() — it fires runs via\n'
        "get_client() instead. See prefect.serve( for the shape we avoid.\n"
        '"""\n'
        "\n"
        "def main():\n"
        "    return None\n"
    )
    findings = check_prefect_serve_pattern(tmp_path)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "CD-015"


def test_cd015_bare_serve_needs_the_prefect_import(tmp_path: Path) -> None:
    """A bare serve() only counts where serve came from prefect."""
    src = tmp_path / "src/pkg"
    src.mkdir(parents=True)
    (src / "other.py").write_text("from prefect import serve\n")
    (src / "main.py").write_text(
        "from .helpers import serve\n\ndef main():\n    serve(thing)\n"
    )
    assert check_prefect_serve_pattern(tmp_path) != []


# --------------------------------------------------------------------------
# SEC-007 — automated dependency updates
# --------------------------------------------------------------------------


def _sec007_repo(tmp_path, *, files: dict[str, str]):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


_GOOD = """version: 2
updates:
  - package-ecosystem: "uv"
    directory: "/"
    schedule: {interval: "weekly"}
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule: {interval: "weekly"}
"""


def test_sec_007_passes_when_every_used_ecosystem_is_covered(tmp_path) -> None:
    from evaluator_cog.engine.deterministic.security import check_sec_007

    repo = _sec007_repo(
        tmp_path,
        files={
            "pyproject.toml": "[project]\nname='x'\n",
            "uv.lock": "",
            ".github/workflows/ci.yml": "name: CI\n",
            ".github/dependabot.yml": _GOOD,
        },
    )
    assert check_sec_007(repo) == []


def test_sec_007_reports_a_missing_config(tmp_path) -> None:
    from evaluator_cog.engine.deterministic.security import check_sec_007

    repo = _sec007_repo(
        tmp_path,
        files={"pyproject.toml": "[project]\nname='x'\n", "uv.lock": ""},
    )
    findings = check_sec_007(repo)
    assert len(findings) == 1
    assert "No .github/dependabot.yml" in findings[0]["finding"]


def test_sec_007_catches_the_half_configured_case(tmp_path) -> None:
    """Covering only Actions is the shape a half-finished config takes.

    Actions is the easiest ecosystem to declare, so a config that stops
    there is the common way this rule gets passed without being met —
    the repo's real exposure is its dependency tree, which stays manual.
    """
    from evaluator_cog.engine.deterministic.security import check_sec_007

    repo = _sec007_repo(
        tmp_path,
        files={
            "package.json": '{"name":"x"}',
            ".github/workflows/ci.yml": "name: CI\n",
            ".github/dependabot.yml": (
                "version: 2\n"
                "updates:\n"
                '  - package-ecosystem: "github-actions"\n'
                '    directory: "/"\n'
                '    schedule: {interval: "weekly"}\n'
            ),
        },
    )
    findings = check_sec_007(repo)
    assert len(findings) == 1
    assert "does not cover JavaScript" in findings[0]["finding"]
    assert "github-actions" in findings[0]["finding"]


def test_sec_007_accepts_any_tool_that_delivers_updates(tmp_path) -> None:
    """The rule is that updates arrive, not which tool delivers them."""
    from evaluator_cog.engine.deterministic.security import check_sec_007

    for eco in ("npm", "pnpm", "yarn"):
        repo = _sec007_repo(
            tmp_path / eco,
            files={
                "package.json": '{"name":"x"}',
                ".github/dependabot.yml": (
                    f'version: 2\nupdates:\n  - package-ecosystem: "{eco}"\n'
                    '    directory: "/"\n'
                ),
            },
        )
        assert check_sec_007(repo) == [], eco


def test_sec_007_reports_an_unparseable_config(tmp_path) -> None:
    """A config GitHub cannot parse is inert — nothing fails, updates
    simply never arrive, which is worse than not having one."""
    from evaluator_cog.engine.deterministic.security import check_sec_007

    repo = _sec007_repo(
        tmp_path,
        files={
            "pyproject.toml": "[project]\nname='x'\n",
            ".github/dependabot.yml": "version: 2\nupdates: [oops\n",
        },
    )
    findings = check_sec_007(repo)
    assert len(findings) == 1
    assert "could not be parsed" in findings[0]["finding"]


def test_sec_007_is_silent_when_there_is_nothing_to_update(tmp_path) -> None:
    from evaluator_cog.engine.deterministic.security import check_sec_007

    assert check_sec_007(_sec007_repo(tmp_path, files={"README.md": "# x"})) == []
