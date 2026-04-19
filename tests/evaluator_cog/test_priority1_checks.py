"""Tests for API-001, API-002, PIPE-001, CD-005, FE-008 deterministic checks."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from evaluator_cog.engine.deterministic import (
    check_astro_pinned_versions,
    check_gha_not_trigger_relay,
    check_postgres_only_data_store,
    check_prefect_cloud_observability,
    check_prefect_present,
    check_railway_hosted_api,
)


def _root(files: dict[str, str]) -> Path:
    r = Path(tempfile.mkdtemp())
    for rel, body in files.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return r


def _write_pyproject(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(body)


def _write_package_json(root: Path, data: dict) -> None:
    (root / "package.json").write_text(json.dumps(data, indent=2))


# --- API-001 -----------------------------------------------------------------


def test_api001_pass_python_with_railway_and_fastapi() -> None:
    root = _root({"railway.toml": "[build]\n"})
    _write_pyproject(
        root, "[project]\nname=x\nversion=0.1.0\ndependencies=['fastapi']\n"
    )
    assert check_railway_hosted_api(root, language="python") == []


def test_api001_warns_missing_railway_config_python() -> None:
    root = _root({})
    _write_pyproject(
        root, "[project]\nname=x\nversion=0.1.0\ndependencies=['fastapi']\n"
    )
    f = check_railway_hosted_api(root, language="python")
    assert any("railway" in x["finding"].lower() for x in f)


def test_api001_warns_missing_fastapi_python() -> None:
    root = _root({"railway.toml": "x=1\n"})
    _write_pyproject(root, "[project]\nname=x\nversion=0.1.0\ndependencies=[]\n")
    f = check_railway_hosted_api(root, language="python")
    assert any("fastapi" in x["finding"].lower() for x in f)


def test_api001_pass_typescript_with_railway_and_hono() -> None:
    root = _root({"nixpacks.toml": "providers = []\n"})
    _write_package_json(root, {"dependencies": {"hono": "4.0.0"}})
    assert check_railway_hosted_api(root, language="typescript") == []


def test_api001_warns_missing_hono_typescript() -> None:
    root = _root({"railway.json": "{}\n"})
    _write_package_json(root, {"dependencies": {"express": "4"}})
    f = check_railway_hosted_api(root, language="typescript")
    assert any("hono" in x["finding"].lower() for x in f)


def test_api001_railway_json_satisfies_condition_one() -> None:
    root = _root({"railway.json": "{}\n"})
    _write_pyproject(root, "[project]\ndependencies=['fastapi']\n")
    assert not any(
        "railway deployment" in x["finding"].lower()
        for x in check_railway_hosted_api(root, language="python")
    )


def test_api001_fastapi_detected_in_requirements_instead_of_pyproject() -> None:
    root = _root({"railway.toml": "x=1\n", "requirements.txt": "fastapi\nprefect\n"})
    assert check_railway_hosted_api(root, language="python") == []


# --- API-002 (10 tests) -------------------------------------------------------


def test_api002_clean_python_postgres_only() -> None:
    root = _root({})
    _write_pyproject(root, "[project]\ndependencies=['asyncpg','sqlalchemy']\n")
    assert check_postgres_only_data_store(root, language="python") == []


def test_api002_flags_mysql_in_pyproject() -> None:
    root = _root({})
    _write_pyproject(root, "[project]\ndependencies=['mysql']\n")
    assert any(
        x["rule_id"] == "API-002"
        for x in check_postgres_only_data_store(root, language="python")
    )


def test_api002_flags_pymongo_in_pyproject() -> None:
    root = _root({})
    _write_pyproject(root, "[project]\ndependencies=['pymongo']\n")
    assert any(
        x["rule_id"] == "API-002"
        for x in check_postgres_only_data_store(root, language="python")
    )


def test_api002_flags_aiosqlite_in_pyproject() -> None:
    root = _root({})
    _write_pyproject(root, "[project]\ndependencies=['aiosqlite']\n")
    assert any(
        x["rule_id"] == "API-002"
        for x in check_postgres_only_data_store(root, language="python")
    )


def test_api002_flags_mysqlclient_in_requirements_txt() -> None:
    root = _root({"requirements.txt": "mysqlclient\n"})
    assert any(
        x["rule_id"] == "API-002"
        for x in check_postgres_only_data_store(root, language="python")
    )


def test_api002_flags_sqlalchemy_sqlite_extra() -> None:
    root = _root({})
    _write_pyproject(root, "[project]\ndependencies=['sqlalchemy[sqlite]']\n")
    assert any(
        x["rule_id"] == "API-002"
        for x in check_postgres_only_data_store(root, language="python")
    )


def test_api002_typescript_clean() -> None:
    root = _root({})
    _write_package_json(root, {"dependencies": {"postgres": "3.4.0"}})
    assert check_postgres_only_data_store(root, language="typescript") == []


def test_api002_typescript_flags_mysql2() -> None:
    root = _root({})
    _write_package_json(root, {"dependencies": {"mysql2": "3.0.0"}})
    assert any(
        x["rule_id"] == "API-002"
        for x in check_postgres_only_data_store(root, language="typescript")
    )


def test_api002_typescript_flags_redis_dependency() -> None:
    root = _root({})
    _write_package_json(root, {"dependencies": {"redis": "4.0.0"}})
    assert any(
        x["rule_id"] == "API-002"
        for x in check_postgres_only_data_store(root, language="typescript")
    )


def test_api002_typescript_no_package_json() -> None:
    root = _root({})
    assert check_postgres_only_data_store(root, language="typescript") == []


# --- PIPE-001 ----------------------------------------------------------------


def _minimal_pyproject_with_prefect(root: Path) -> None:
    _write_pyproject(root, "[project]\ndependencies=['prefect']\n")


def test_pipe001_pipeline_passes_with_flow_decorator() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    src = root / "src" / "app"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        "from prefect import flow\n\n@flow\ndef main():\n    pass\n"
    )
    assert check_prefect_present(root, cog_subtype="pipeline") == []


def test_pipe001_pipeline_warns_missing_flow() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    src = root / "src" / "app"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hi')\n")
    f = check_prefect_present(root, cog_subtype="pipeline")
    assert any("flow" in x["finding"].lower() for x in f)


def test_pipe001_trigger_passes_with_run_deployment() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    src = root / "src" / "app"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        "from prefect.deployments import run_deployment\nrun_deployment('x')\n"
    )
    assert check_prefect_present(root, cog_subtype="trigger") == []


def test_pipe001_trigger_warns_without_run_deployment() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    src = root / "src" / "app"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        "from prefect import flow\n@flow\ndef x():\n    pass\n"
    )
    f = check_prefect_present(root, cog_subtype="trigger")
    assert any("run_deployment" in x["finding"].lower() for x in f)


def test_pipe001_short_circuits_when_prefect_missing() -> None:
    root = _root({})
    _write_pyproject(root, "[project]\ndependencies=[]\n")
    src = root / "src" / "app"
    src.mkdir(parents=True)
    (src / "main.py").write_text("# no flow\n")
    f = check_prefect_present(root, cog_subtype="pipeline")
    assert len(f) == 1
    assert "dependency" in f[0]["finding"].lower()


def test_pipe001_prefect_only_in_requirements_txt() -> None:
    root = _root({"requirements.txt": "prefect\n"})
    src = root / "src" / "x"
    src.mkdir(parents=True)
    (src / "m.py").write_text("from prefect import flow\n@flow\ndef a():\n    pass\n")
    assert check_prefect_present(root, cog_subtype="pipeline") == []


def test_pipe001_missing_src_after_prefect_dep() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    f = check_prefect_present(root, cog_subtype="pipeline")
    assert any("src/" in x["finding"] for x in f)


def test_pipe001_trigger_accepts_run_deployment_substring_only() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    src = root / "src" / "t"
    src.mkdir(parents=True)
    (src / "w.py").write_text("def go():\n    run_deployment('x')\n")
    assert check_prefect_present(root, cog_subtype="trigger") == []


def test_pipe001_pipeline_finds_flow_in_nested_src_module() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    deep = root / "src" / "pkg" / "nested"
    deep.mkdir(parents=True)
    (deep / "flows.py").write_text(
        "from prefect import flow\n\n@flow\ndef nightly():\n    return 0\n"
    )
    assert check_prefect_present(root, cog_subtype="pipeline") == []


# --- CD-005 ------------------------------------------------------------------


def test_cd005_empty_when_prefect_not_declared() -> None:
    root = _root({})
    _write_pyproject(root, "[project]\ndependencies=[]\n")
    (root / ".env.example").write_text("FOO=1\n")
    assert check_prefect_cloud_observability(root) == []


def test_cd005_warns_when_prefect_declared_but_env_missing() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    (root / ".env.example").write_text("OTHER=1\n")
    f = check_prefect_cloud_observability(root)
    assert any("prefect" in x["finding"].lower() for x in f)


def test_cd005_passes_with_prefect_api_url() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    (root / ".env.example").write_text("PREFECT_API_URL=https://api.prefect.cloud/x\n")
    assert check_prefect_cloud_observability(root) == []


def test_cd005_passes_with_prefect_cloud_token() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    (root / ".env.example").write_text("PREFECT_CLOUD_API_KEY=secret\n")
    assert check_prefect_cloud_observability(root) == []


def test_cd005_passes_with_api_prefect_cloud_host() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    (root / ".env.example").write_text("X=https://api.prefect.cloud/foo\n")
    assert check_prefect_cloud_observability(root) == []


def test_cd005_info_when_apscheduler_in_source() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    (root / ".env.example").write_text("PREFECT_API_URL=https://api.prefect.cloud/x\n")
    src = root / "src" / "job"
    src.mkdir(parents=True)
    (src / "s.py").write_text(
        "from apscheduler.schedulers.background import BackgroundScheduler\n"
    )
    f = check_prefect_cloud_observability(root)
    assert any(
        x["severity"] == "INFO" and "apscheduler" in x["finding"].lower() for x in f
    )


def test_cd005_no_apscheduler_info_without_prefect_dep() -> None:
    root = _root({})
    _write_pyproject(root, "[project]\ndependencies=[]\n")
    src = root / "src" / "job"
    src.mkdir(parents=True)
    (src / "s.py").write_text(
        "from apscheduler.schedulers.background import BackgroundScheduler\n"
    )
    assert check_prefect_cloud_observability(root) == []


def test_cd005_prefect_in_requirements_triggers_env_scan() -> None:
    root = _root({"requirements.txt": "prefect\n"})
    src = root / "src" / "x"
    src.mkdir(parents=True)
    (src / "m.py").write_text("x=1\n")
    (root / ".env.example").write_text("")
    f = check_prefect_cloud_observability(root)
    assert any(x["rule_id"] == "CD-005" and x["severity"] == "WARN" for x in f)


def test_cd005_missing_env_example_still_warns() -> None:
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    f = check_prefect_cloud_observability(root)
    assert any(x["rule_id"] == "CD-005" for x in f)


# --- FE-008 ------------------------------------------------------------------


def test_fe008_passes_exact_pins_for_astro_packages() -> None:
    root = _root({})
    _write_package_json(
        root,
        {
            "dependencies": {"astro": "4.5.0"},
            "devDependencies": {"@astrojs/tailwind": "5.1.0"},
        },
    )
    assert check_astro_pinned_versions(root) == []


def test_fe008_flags_caret_range() -> None:
    root = _root({})
    _write_package_json(root, {"dependencies": {"astro": "^4.0.0"}})
    f = check_astro_pinned_versions(root)
    assert any(x["rule_id"] == "FE-008" for x in f)


def test_fe008_flags_tilde_range() -> None:
    root = _root({})
    _write_package_json(root, {"dependencies": {"astro": "~4.0.0"}})
    assert any(x["rule_id"] == "FE-008" for x in check_astro_pinned_versions(root))


def test_fe008_flags_x_range() -> None:
    root = _root({})
    _write_package_json(root, {"dependencies": {"astro": "4.x"}})
    f = check_astro_pinned_versions(root)
    assert any(x["rule_id"] == "FE-008" for x in f)


def test_fe008_flags_latest_token() -> None:
    root = _root({})
    _write_package_json(root, {"dependencies": {"astro": "latest"}})
    assert any(x["rule_id"] == "FE-008" for x in check_astro_pinned_versions(root))


def test_fe008_flags_gte() -> None:
    root = _root({})
    _write_package_json(root, {"devDependencies": {"@astrojs/check": ">=0.9.0"}})
    f = check_astro_pinned_versions(root)
    assert any(x["rule_id"] == "FE-008" for x in f)


def test_fe008_flags_star() -> None:
    root = _root({})
    _write_package_json(root, {"dependencies": {"astro": "*"}})
    assert any(x["rule_id"] == "FE-008" for x in check_astro_pinned_versions(root))


def test_fe008_invalid_json() -> None:
    root = _root({"package.json": "{not json"})
    f = check_astro_pinned_versions(root)
    assert any("json" in x["finding"].lower() for x in f)


def test_fe008_non_string_version_warns() -> None:
    root = _root({})
    _write_package_json(root, {"dependencies": {"astro": ["4.0.0"]}})
    f = check_astro_pinned_versions(root)
    assert any("string" in x["finding"].lower() for x in f)


def test_fe008_uppercase_x_range() -> None:
    root = _root({})
    _write_package_json(root, {"dependencies": {"@astrojs/starlight": "3.X"}})
    f = check_astro_pinned_versions(root)
    assert any(x["rule_id"] == "FE-008" for x in f)


# --- CD-006 ------------------------------------------------------------------


def test_cd006_clean_ci_workflow_passes() -> None:
    root = _root(
        {
            ".github/workflows/ci.yml": """name: CI
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
""",
        }
    )
    assert check_gha_not_trigger_relay(root) == []


def test_cd006_release_tag_workflow_passes() -> None:
    root = _root(
        {
            ".github/workflows/release.yml": """name: Release
on:
  push:
    tags: ['v*']
jobs:
  ship:
    runs-on: ubuntu-latest
    steps:
      - run: echo release
""",
        }
    )
    assert check_gha_not_trigger_relay(root) == []


def test_cd006_missing_workflows_dir_tolerated() -> None:
    root = _root({"src/x.py": "x = 1\n"})
    assert check_gha_not_trigger_relay(root) == []


def test_cd006_repository_dispatch_pure_ci_passes() -> None:
    root = _root(
        {
            ".github/workflows/dispatch.yml": """on:
  repository_dispatch:
    types: [drift]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
""",
        }
    )
    assert check_gha_not_trigger_relay(root) == []


def test_cd006_repository_dispatch_prefect_deployment_run_flagged() -> None:
    root = _root(
        {
            ".github/workflows/dispatch.yml": """on:
  repository_dispatch:
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: prefect deployment run my-flow/my-deployment
""",
        }
    )
    f = check_gha_not_trigger_relay(root)
    assert any(x["rule_id"] == "CD-006" for x in f)


def test_cd006_repository_dispatch_run_deployment_snippet_flagged() -> None:
    root = _root(
        {
            ".github/workflows/dispatch.yml": """on:
  repository_dispatch:
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: echo run_deployment(
""",
        }
    )
    f = check_gha_not_trigger_relay(root)
    assert any(x["rule_id"] == "CD-006" for x in f)


def test_cd006_scheduled_workflow_prefect_cloud_flagged() -> None:
    root = _root(
        {
            ".github/workflows/cron.yml": """on:
  schedule:
    - cron: '0 * * * *'
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: curl https://api.prefect.cloud/api/accounts/x/workspaces/y
""",
        }
    )
    f = check_gha_not_trigger_relay(root)
    assert any(x["rule_id"] == "CD-006" for x in f)


def test_cd006_scheduled_drift_pytest_only_passes() -> None:
    root = _root(
        {
            ".github/workflows/drift.yml": """on:
  schedule:
    - cron: '0 * * * *'
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/
""",
        }
    )
    assert check_gha_not_trigger_relay(root) == []


def test_cd006_workflow_posts_v1_trigger_flagged() -> None:
    root = _root(
        {
            ".github/workflows/t.yml": """jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: curl -X POST "/v1/trigger"
""",
        }
    )
    f = check_gha_not_trigger_relay(root)
    assert any(x["rule_id"] == "CD-006" for x in f)


def test_cd006_workflow_posts_v1_runs_flagged() -> None:
    root = _root(
        {
            ".github/workflows/t.yml": """jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: curl '/v1/runs'
""",
        }
    )
    f = check_gha_not_trigger_relay(root)
    assert any(x["rule_id"] == "CD-006" for x in f)


def test_cd006_dot_yaml_extension_scanned() -> None:
    root = _root(
        {
            ".github/workflows/up.yaml": """on:
  repository_dispatch:
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: prefect deployment run x
""",
        }
    )
    f = check_gha_not_trigger_relay(root)
    assert any(x["rule_id"] == "CD-006" for x in f)


def test_cd006_multi_violation_one_workflow_emits_multiple_findings() -> None:
    root = _root(
        {
            ".github/workflows/bad.yml": """on:
  repository_dispatch:
  schedule:
    - cron: '0 0 * * *'
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: prefect deployment run x
      - run: curl https://api.prefect.cloud/ping
""",
        }
    )
    f = check_gha_not_trigger_relay(root)
    assert sum(1 for x in f if x["rule_id"] == "CD-006") >= 2


def test_cd006_malformed_workflow_yaml_does_not_raise() -> None:
    root = _root(
        {
            ".github/workflows/broken.yml": "{{{ not yaml {{{\nrepository_dispatch: x\n",
        }
    )
    check_gha_not_trigger_relay(root)
