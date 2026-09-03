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


def test_pipe001_trigger_warns_without_any_client_signal() -> None:
    """A trigger cog with @flow but no client signals fires PIPE-001."""
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    src = root / "src" / "app"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        "from prefect import flow\n@flow\ndef x():\n    pass\n"
    )
    f = check_prefect_present(root, cog_subtype="trigger")
    assert len(f) == 1
    assert f[0]["rule_id"] == "PIPE-001"
    finding_text = f[0]["finding"].lower()
    assert any(
        signal in finding_text
        for signal in (
            "run_deployment",
            "prefectclient",
            "create_flow_run_from_deployment",
        )
    )


def test_pipe001_trigger_passes_with_prefect_client() -> None:
    """A trigger cog using PrefectClient get_client() satisfies PIPE-001."""
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    src = root / "src" / "app"
    src.mkdir(parents=True)
    (src / "trigger.py").write_text(
        "from prefect import get_client\n"
        "async def fire(dep_id):\n"
        "    async with get_client() as client:\n"
        "        await client.create_flow_run_from_deployment(deployment_id=dep_id)\n"
    )
    assert check_prefect_present(root, cog_subtype="trigger") == []


def test_pipe001_trigger_passes_with_create_flow_run_only() -> None:
    """The create_flow_run_from_deployment substring alone satisfies PIPE-001."""
    root = _root({})
    _minimal_pyproject_with_prefect(root)
    src = root / "src" / "app"
    src.mkdir(parents=True)
    (src / "trigger.py").write_text(
        "def fire(client, dep_id):\n"
        "    return client.create_flow_run_from_deployment(deployment_id=dep_id)\n"
    )
    assert check_prefect_present(root, cog_subtype="trigger") == []


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


# --- TEST-011 detection fixes from the 2026-09-03 fleet run ------------------


def _mock_repo(tmp_path: Path, body: str) -> Path:
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_x.py").write_text(body, encoding="utf-8")
    return tmp_path


def test_test011_does_not_read_the_word_patch_in_prose_as_a_mock(
    tmp_path: Path,
) -> None:
    """`patch` the English noun is not `patch()` the mock.

    A docstring reading "an un-taken patch release is not staleness"
    made TEST-011 fire on a test that verifies its result with a plain
    assert. `patch` counts only where it is called or attribute-accessed.
    """
    from evaluator_cog.engine.deterministic import check_mock_assertions

    _mock_repo(
        tmp_path,
        "def _helper():\n"
        "    return []\n"
        "\n"
        "def test_patch_behind_is_not_flagged() -> None:\n"
        '    """Compare minors only — an un-taken patch release is not stale."""\n'
        "    assert _helper() == []\n",
    )
    assert check_mock_assertions(tmp_path) == []


def test_test011_ignores_mock_machinery_quoted_as_fixture_text(
    tmp_path: Path,
) -> None:
    """A checker's fixtures quote the patterns it hunts for.

    `_write(tmp_path, "t.py", "from unittest.mock import patch\\n...")`
    hands a string to the code under test; it does not create a mock.
    This is the CD-012 self-scan defect in another costume.
    """
    from evaluator_cog.engine.deterministic import check_mock_assertions

    fixture = "\n".join(
        [
            "def test_checker_flags_the_fixture(tmp_path) -> None:",
            "    source = (",
            '        "from unittest.mock import patch" "\\n"',
            '        "with patch(x):" "\\n"',
            "    )",
            '    (tmp_path / "f.py").write_text(source)',
            "    assert source",
            "",
        ]
    )
    _mock_repo(tmp_path, fixture)
    assert check_mock_assertions(tmp_path) == []


def test_test011_still_flags_a_real_mock_with_no_assertion(
    tmp_path: Path,
) -> None:
    """The true positive must survive both fixes."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    _mock_repo(
        tmp_path,
        "from unittest.mock import MagicMock\n"
        "\n"
        "def test_does_something() -> None:\n"
        "    thing = MagicMock()\n"
        "    do_work(thing)\n",
    )
    findings = check_mock_assertions(tmp_path)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "TEST-011"


# --- slow-check reporting ----------------------------------------------------


def test_is_inside_string_literal_is_linear_not_quadratic() -> None:
    """The regression test for a 129-second check.

    `_is_inside_string_literal` asked "is this constant a dict key?" per
    constant, and each answer re-walked the whole tree. CD-005
    concatenates every file under src/ into one string and asked twice,
    so the cost grew with the square of the repo. evaluator-cog's own
    source doubling turned it into 129 seconds of a 138-second run.

    Timing is a poor test, so this measures shape instead: doubling the
    input must not much more than double the work. A quadratic
    implementation lands near 4x; the bound here is deliberately loose
    so a slow machine cannot fail it, while still catching n^2.
    """
    import time

    from evaluator_cog.engine.deterministic._shared import _is_inside_string_literal

    unit = 'X = {"k": "needle"}\nY = "needle"\nZ = [1, 2, 3]\n'

    def elapsed(repeats: int) -> float:
        source = unit * repeats
        start = time.perf_counter()
        _is_inside_string_literal(source, "needle")
        return time.perf_counter() - start

    elapsed(200)  # warm any import-time cost
    small = elapsed(400)
    large = elapsed(800)
    assert large < small * 3 + 0.05, (
        f"doubling the input multiplied the work by {large / max(small, 1e-9):.1f}x "
        "— _is_inside_string_literal looks quadratic again"
    )


def test_dict_key_constants_are_still_excluded() -> None:
    """The optimisation must not change the answer.

    A dict key is real usage, not a quoted pattern, so a needle that
    appears only as a key is NOT 'inside a string literal'.
    """
    from evaluator_cog.engine.deterministic._shared import _is_inside_string_literal

    assert _is_inside_string_literal('PATTERN = "needle"\n', "needle") is True
    assert _is_inside_string_literal('H = {"needle": "x"}\n', "needle") is False


def test_run_all_checks_reports_a_slow_check(tmp_path: Path) -> None:
    """A check slower than the threshold names itself in the progress sink."""
    import time as _time

    from evaluator_cog.engine.deterministic import runner as runner_mod

    notes: list[str] = []
    (tmp_path / "src").mkdir()

    original = runner_mod.check_readme

    def slow(*args, **kwargs):
        _time.sleep(runner_mod.SLOW_CHECK_SECONDS + 0.05)
        return []

    runner_mod.check_readme = slow
    try:
        runner_mod.run_all_checks(tmp_path, progress=notes.append)
    finally:
        runner_mod.check_readme = original

    assert any("took" in n for n in notes), f"no slow-check note emitted: {notes}"


# --- production_python_text --------------------------------------------------


def test_production_python_text_excludes_the_checker_tree(tmp_path: Path) -> None:
    """A checker's own patterns are not the target repo's source.

    Six checks concatenated every file under src/ and matched patterns
    over the result. Run against evaluator-cog that includes the
    deterministic checkers, so each pattern they hunt for is present as a
    literal — CD-005 finds pipeline.py's own "apscheduler" every time.
    """
    from evaluator_cog.engine.deterministic._shared import production_python_text

    app = tmp_path / "src" / "myapp"
    app.mkdir(parents=True)
    (app / "service.py").write_text("REAL = 'application code'\n")

    checker = tmp_path / "src" / "evaluator_cog" / "engine" / "deterministic"
    checker.mkdir(parents=True)
    (checker / "pipeline.py").write_text("PATTERNS = ('apscheduler', 'celery')\n")

    text = production_python_text(tmp_path)
    assert "application code" in text
    assert "apscheduler" not in text, "checker source leaked into the scanned text"


def test_production_python_text_is_empty_without_a_src_tree(tmp_path: Path) -> None:
    from evaluator_cog.engine.deterministic._shared import production_python_text

    assert production_python_text(tmp_path) == ""


def test_production_python_text_reflects_later_writes(tmp_path: Path) -> None:
    """Not memoised — a second read must see what the first did not.

    Caching on the directory path saved 0.7 ms per repo and would have
    returned stale text to any caller that reads, writes, then reads.
    """
    from evaluator_cog.engine.deterministic._shared import production_python_text

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("A = 1\n")
    assert "B = 2" not in production_python_text(tmp_path)
    (tmp_path / "src" / "b.py").write_text("B = 2\n")
    assert "B = 2" in production_python_text(tmp_path)


def test_cd005_does_not_flag_its_own_apscheduler_pattern(tmp_path: Path) -> None:
    """The self-scan CD-005 has been paying for, made explicit."""
    from evaluator_cog.engine.deterministic import check_prefect_cloud_observability

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["prefect>=3.0"]\n'
    )
    (tmp_path / ".env.example").write_text(
        "PREFECT_API_URL=https://api.prefect.cloud\n"
    )
    checker = tmp_path / "src" / "evaluator_cog" / "engine" / "deterministic"
    checker.mkdir(parents=True)
    (checker / "pipeline.py").write_text('SIGNALS = ("apscheduler",)\n')

    assert check_prefect_cloud_observability(tmp_path) == []


def test_doc006_ignores_functions_nested_in_functions(tmp_path: Path) -> None:
    """A closure is not public API.

    `ast.walk` reaches every node, so a four-line recursive helper
    defined inside another function was reported as a public function
    missing documentation. It cannot be imported and no consumer can
    call it.
    """
    from evaluator_cog.engine.deterministic import check_public_docstrings

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(
        "def enclosing(tree):\n"
        '    """Documented, public, fine."""\n'
        "\n"
        "    def walk(node, depth):\n"
        "        return depth\n"
        "\n"
        "    return walk(tree, 0)\n"
    )
    assert check_public_docstrings(tmp_path) == []


def test_doc006_still_flags_module_level_and_methods(tmp_path: Path) -> None:
    """Module-level functions and a class's methods remain public."""
    from evaluator_cog.engine.deterministic import check_public_docstrings

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(
        "def loose():\n"
        "    return 1\n"
        "\n"
        "class Thing:\n"
        '    """Documented."""\n'
        "\n"
        "    def method(self):\n"
        "        return 2\n"
    )
    names = {
        f["finding"].split("::")[1].split(":")[0]
        for f in check_public_docstrings(tmp_path)
    }
    assert names == {"loose", "Thing.method"} or names == {"loose", "method"}


def test_doc006_skips_a_class_defined_inside_a_function(tmp_path: Path) -> None:
    """A class nested in a function exposes nothing, nor do its methods."""
    from evaluator_cog.engine.deterministic import check_public_docstrings

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(
        "def factory():\n"
        '    """Documented."""\n'
        "\n"
        "    class Local:\n"
        "        def method(self):\n"
        "            return 1\n"
        "\n"
        "    return Local\n"
    )
    assert check_public_docstrings(tmp_path) == []
