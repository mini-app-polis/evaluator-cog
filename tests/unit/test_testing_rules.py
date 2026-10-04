"""TEST-009 (local test-database guard) and TEST-019 (coverage floor).

The TEST-009 passing fixture is trimmed from api-deejaytools' CI job and
conftest, the shape the rule was written from. The failing SQLite fixture
is api-kaianolevine-com's, which the rule now reports.
"""

from __future__ import annotations

from pathlib import Path

from evaluator_cog.engine.deterministic import (
    check_coverage_floor,
    check_test_database_guard,
    run_all_checks,
)


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _text(findings: list[dict]) -> str:
    return " || ".join(f["finding"] for f in findings)


# --- TEST-009 -----------------------------------------------------------------

_CI_LOCAL = """\
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
    env:
      TEST_DATABASE_URL: postgresql://postgres:postgres@localhost:5432/svc_test
    steps:
      - run: uv run pytest --cov=src
"""

_CONFTEST_GUARD = """\
import os
from urllib.parse import urlparse

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/svc_test"
)


def _guard(url: str) -> None:
    parsed = urlparse(url)
    if parsed.hostname not in {"localhost", "127.0.0.1"} or not parsed.path.endswith(
        "_test"
    ):
        raise RuntimeError("TEST_DATABASE_URL must be local and named *_test.")


_guard(TEST_DATABASE_URL)

from svc.main import app  # noqa: E402
"""


def _db_repo(
    tmp_path: Path, *, ci: str = _CI_LOCAL, conftest: str = _CONFTEST_GUARD
) -> Path:
    _write(tmp_path, "src/svc/__init__.py", "")
    _write(
        tmp_path, "src/svc/models.py", "from sqlalchemy.orm import DeclarativeBase\n"
    )
    _write(tmp_path, ".github/workflows/ci.yml", ci)
    _write(tmp_path, "tests/conftest.py", conftest)
    return tmp_path


def test_009_guarded_local_test_database_passes(tmp_path: Path) -> None:
    assert check_test_database_guard(_db_repo(tmp_path)) == []


def test_009_guard_in_pytest_configure_passes(tmp_path: Path) -> None:
    conftest = _CONFTEST_GUARD.replace(
        "_guard(TEST_DATABASE_URL)\n\nfrom svc.main import app  # noqa: E402\n",
        "def pytest_configure(config):\n    _guard(TEST_DATABASE_URL)\n",
    )
    assert check_test_database_guard(_db_repo(tmp_path, conftest=conftest)) == []


def test_009_repo_without_a_database_has_no_subject(tmp_path: Path) -> None:
    _write(tmp_path, "src/svc/__init__.py", "")
    _write(
        tmp_path, "src/svc/main.py", "from fastapi import FastAPI\napp = FastAPI()\n"
    )
    assert check_test_database_guard(tmp_path) == []


def test_009_in_memory_sqlite_fails_both_ci_and_guard(tmp_path: Path) -> None:
    """api-kaianolevine-com's shape: SQLite in memory, no guard."""
    repo = _db_repo(
        tmp_path,
        ci="jobs:\n  test:\n    steps:\n      - run: uv run pytest --cov=src\n",
        conftest='import os\nTEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"\n'
        'os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)\n',
    )
    findings = check_test_database_guard(repo)
    assert len(findings) == 2
    assert "CI test job" in _text(findings)
    assert "refuses" in _text(findings)


def test_009_ci_database_not_named_test_fails(tmp_path: Path) -> None:
    ci = _CI_LOCAL.replace("/svc_test", "/svc")
    findings = check_test_database_guard(_db_repo(tmp_path, ci=ci))
    assert len(findings) == 1
    assert "localhost:5432/svc" in _text(findings)


def test_009_ci_database_on_a_remote_host_fails(tmp_path: Path) -> None:
    ci = _CI_LOCAL.replace("@localhost:5432", "@db.example.com:5432")
    assert len(check_test_database_guard(_db_repo(tmp_path, ci=ci))) == 1


def test_009_step_level_env_counts(tmp_path: Path) -> None:
    ci = (
        "jobs:\n  test:\n    steps:\n      - run: uv run pytest\n        env:\n"
        "          DATABASE_URL: postgresql://u:p@127.0.0.1:5432/x_test\n"
    )
    assert check_test_database_guard(_db_repo(tmp_path, ci=ci)) == []


def test_009_guard_after_the_app_import_fails(tmp_path: Path) -> None:
    """Importing the app builds its engine; the guard has to come first."""
    conftest = _CONFTEST_GUARD.replace(
        "_guard(TEST_DATABASE_URL)\n\nfrom svc.main import app  # noqa: E402\n",
        "from svc.main import app  # noqa: E402\n\n_guard(TEST_DATABASE_URL)\n",
    )
    findings = check_test_database_guard(_db_repo(tmp_path, conftest=conftest))
    assert len(findings) == 1
    assert "before the application is imported" in _text(findings)


def test_009_guard_defined_but_never_called_fails(tmp_path: Path) -> None:
    conftest = _CONFTEST_GUARD.replace("_guard(TEST_DATABASE_URL)\n\n", "")
    assert len(check_test_database_guard(_db_repo(tmp_path, conftest=conftest))) == 1


def test_009_check_without_raise_is_not_a_guard(tmp_path: Path) -> None:
    conftest = _CONFTEST_GUARD.replace(
        '        raise RuntimeError("TEST_DATABASE_URL must be local and named *_test.")',
        "        print('_test')",
    )
    assert len(check_test_database_guard(_db_repo(tmp_path, conftest=conftest))) == 1


def test_009_remote_database_url_in_tests_fails(tmp_path: Path) -> None:
    repo = _db_repo(tmp_path)
    _write(
        repo,
        "tests/test_reports.py",
        'PROD = "postgresql://app:secret@prod-db.internal:5432/app"\n',
    )
    findings = check_test_database_guard(repo)
    assert len(findings) == 1
    assert "prod-db.internal" in _text(findings)


def test_009_single_label_hosts_are_not_reported(tmp_path: Path) -> None:
    """api-kaianolevine-com's URL-parsing fixtures use host `h`."""
    repo = _db_repo(tmp_path)
    _write(
        repo,
        "tests/test_urls.py",
        'A = "postgresql+asyncpg://u:p@h/db"\nB = "postgresql://u:p@postgres:5432/x"\n',
    )
    assert check_test_database_guard(repo) == []


def test_009_the_word_sqlalchemy_is_not_a_database(tmp_path: Path) -> None:
    _write(tmp_path, "src/svc/__init__.py", "")
    _write(tmp_path, "src/svc/notes.py", 'NOTE = "we do not use sqlalchemy"\n')
    assert check_test_database_guard(tmp_path) == []


def test_009_templated_urls_are_not_reported(tmp_path: Path) -> None:
    repo = _db_repo(tmp_path)
    _write(repo, "tests/test_x.py", 'URL = f"postgresql://u:p@{host}:5432/x_test"\n')
    assert check_test_database_guard(repo) == []


# --- TEST-019 -----------------------------------------------------------------

_PYPROJECT_FLOOR = "[project]\nname = 'x'\n\n[tool.coverage.report]\nfail_under = 82\n"
_CI_COV = "jobs:\n  test:\n    steps:\n      - run: uv run pytest --cov=src\n"
_SHARED_TEST_CI = (
    "on:\n  push:\njobs:\n  test:\n"
    "    uses: mini-app-polis/.github/.github/workflows/python-test.yml@v3\n"
)


def _py_repo(
    tmp_path: Path, *, pyproject: str = _PYPROJECT_FLOOR, ci: str = _CI_COV
) -> Path:
    _write(tmp_path, "pyproject.toml", pyproject)
    _write(tmp_path, ".github/workflows/ci.yml", ci)
    return tmp_path


def test_019_measured_with_a_floor_passes(tmp_path: Path) -> None:
    assert check_coverage_floor(_py_repo(tmp_path)) == []


def test_019_shared_test_workflow_counts_as_measured(tmp_path: Path) -> None:
    """Coverage runs inside python-test.yml; ci.yml need not say --cov."""
    assert check_coverage_floor(_py_repo(tmp_path, ci=_SHARED_TEST_CI)) == []


def test_019_shared_workflow_with_coverage_off_fails(tmp_path: Path) -> None:
    """Overriding pytest-args without --cov is the one way to turn it off."""
    ci = _SHARED_TEST_CI + "    with:\n      pytest-args: -q\n"
    findings = check_coverage_floor(_py_repo(tmp_path, ci=ci))
    assert len(findings) == 1
    assert "not measured" in _text(findings)


def test_019_pytest_without_cov_fails(tmp_path: Path) -> None:
    ci = "jobs:\n  test:\n    steps:\n      - run: uv run pytest\n"
    assert "not measured" in _text(check_coverage_floor(_py_repo(tmp_path, ci=ci)))


def test_019_no_floor_fails(tmp_path: Path) -> None:
    """Both FastAPIs' shape today: --cov in CI, no fail_under."""
    findings = check_coverage_floor(
        _py_repo(tmp_path, pyproject="[project]\nname = 'x'\n")
    )
    assert len(findings) == 1
    assert "fail_under" in _text(findings)


def test_019_commented_out_floor_does_not_count(tmp_path: Path) -> None:
    pyproject = "[project]\nname = 'x'\n\n[tool.coverage.report]\n# fail_under = 82\n"
    assert len(check_coverage_floor(_py_repo(tmp_path, pyproject=pyproject))) == 1


def test_019_zero_floor_does_not_count(tmp_path: Path) -> None:
    pyproject = _PYPROJECT_FLOOR.replace("82", "0")
    assert len(check_coverage_floor(_py_repo(tmp_path, pyproject=pyproject))) == 1


def test_019_neither_measured_nor_floored_reports_both(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path, pyproject="[project]\nname = 'x'\n", ci="jobs: {}\n")
    assert len(check_coverage_floor(repo)) == 2


def test_019_vitest_thresholds_and_coverage_script_pass(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"scripts": {"test": "vitest run --coverage"}}')
    _write(
        tmp_path,
        "vitest.config.ts",
        "export default { test: { coverage: { thresholds: { autoUpdate: true } } } }\n",
    )
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "jobs:\n  test:\n    steps:\n      - run: pnpm test\n",
    )
    assert check_coverage_floor(tmp_path) == []


def test_019_vitest_without_thresholds_fails(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"scripts": {"test": "vitest run"}}')
    _write(tmp_path, "vitest.config.ts", "export default { test: {} }\n")
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "jobs:\n  test:\n    steps:\n      - run: pnpm test:coverage\n",
    )
    findings = check_coverage_floor(tmp_path)
    assert len(findings) == 1
    assert "coverage.thresholds" in _text(findings)


def test_019_repo_without_a_project_file_passes(tmp_path: Path) -> None:
    assert check_coverage_floor(tmp_path) == []


# --- registration ---------------------------------------------------------------


def test_runner_registers_the_reconciled_rules(tmp_path: Path) -> None:
    _db_repo(tmp_path)
    _write(tmp_path, "pyproject.toml", _PYPROJECT_FLOOR)
    checked = run_all_checks(tmp_path, dod_type="new_fastapi_service").checked_rule_ids
    assert {"TEST-009", "TEST-019"} <= checked
    assert not checked & {"TEST-006", "TEST-010", "TEST-GAP-001"}


# --- TEST-021 -----------------------------------------------------------------

from evaluator_cog.engine.deterministic import check_tests_split_by_layer  # noqa: E402


def _layered(tmp_path: Path) -> Path:
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(tmp_path, "tests/unit/test_parse.py", "def test_x():\n    assert True\n")
    _write(
        tmp_path, "tests/integration/test_routes.py", "def test_y():\n    assert True\n"
    )
    _write(
        tmp_path,
        "tests/integration/conftest.py",
        'import asyncpg\nURL = "postgresql://u:p@localhost:5432/x_test"\n',
    )
    return tmp_path


def test_021_layered_repo_passes(tmp_path: Path) -> None:
    assert (
        check_tests_split_by_layer(_layered(tmp_path), require_integration=True) == []
    )


def test_021_nested_layout_inside_a_layer_passes(tmp_path: Path) -> None:
    """Mirroring the package inside a layer is fine."""
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(tmp_path, "tests/unit/pkg/api/test_client.py", "def test_x():\n    pass\n")
    assert check_tests_split_by_layer(tmp_path) == []


def test_021_named_suites_beside_the_layers_pass(tmp_path: Path) -> None:
    repo = _layered(tmp_path)
    _write(repo, "tests/evals/test_harness.py", "def test_e():\n    pass\n")
    _write(repo, "tests/contract/test_api.py", "def test_c():\n    pass\n")
    assert check_tests_split_by_layer(repo, require_integration=True) == []


def test_021_non_python_repo_passes(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", "{}")
    assert check_tests_split_by_layer(tmp_path, require_integration=True) == []


def test_021_flat_tests_fail(tmp_path: Path) -> None:
    """api-deejaytools' shape: every test loose in tests/, no layers."""
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    for name in ("test_songs.py", "test_events.py"):
        _write(tmp_path, f"tests/{name}", "def test_x():\n    pass\n")
    findings = check_tests_split_by_layer(tmp_path, require_integration=True)
    text = _text(findings)
    assert len(findings) == 3
    assert "tests/unit/" in text and "tests/integration/" in text
    assert "2 test file(s) sit directly in tests/" in text


def test_021_integration_optional_for_libraries(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(tmp_path, "tests/unit/test_x.py", "def test_x():\n    pass\n")
    assert check_tests_split_by_layer(tmp_path, require_integration=False) == []


def test_021_integration_required_for_apis_and_cogs(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(tmp_path, "tests/unit/test_x.py", "def test_x():\n    pass\n")
    findings = check_tests_split_by_layer(tmp_path, require_integration=True)
    assert len(findings) == 1
    assert "TEST-015" in _text(findings)


def test_021_empty_layer_directory_does_not_count(tmp_path: Path) -> None:
    repo = _layered(tmp_path)
    (repo / "tests/unit/test_parse.py").unlink()
    _write(repo, "tests/unit/helpers.py", "X = 1\n")
    assert len(check_tests_split_by_layer(repo, require_integration=True)) == 1


def test_021_database_in_the_root_conftest_fails(tmp_path: Path) -> None:
    repo = _layered(tmp_path)
    _write(
        repo,
        "tests/conftest.py",
        "from sqlalchemy.ext.asyncio import create_async_engine\n",
    )
    findings = check_tests_split_by_layer(repo, require_integration=True)
    assert len(findings) == 1
    assert "every test" in _text(findings)


def test_021_database_url_in_the_root_conftest_fails(tmp_path: Path) -> None:
    repo = _layered(tmp_path)
    _write(repo, "tests/conftest.py", 'URL = "sqlite+aiosqlite:///:memory:"\n')
    assert len(check_tests_split_by_layer(repo, require_integration=True)) == 1


def test_021_root_conftest_without_a_database_passes(tmp_path: Path) -> None:
    repo = _layered(tmp_path)
    _write(
        repo,
        "tests/conftest.py",
        "import pytest\n\n@pytest.fixture\ndef now():\n    return 0\n",
    )
    assert check_tests_split_by_layer(repo, require_integration=True) == []


def test_runner_requires_integration_by_type(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(tmp_path, "src/svc/__init__.py", "")
    _write(tmp_path, "tests/unit/test_x.py", "def test_x():\n    pass\n")
    api = run_all_checks(tmp_path, dod_type="new_fastapi_service")
    assert "TEST-021" in api.checked_rule_ids
    assert any(
        f["rule_id"] == "TEST-021" and "tests/integration/" in f["finding"]
        for f in api.findings
    )
