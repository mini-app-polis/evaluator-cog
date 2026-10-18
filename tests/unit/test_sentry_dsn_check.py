"""CD-038: a production service with no Sentry DSN says so.

Each case is a throwaway repo under ``tmp_path``. The Settings-field case
is a trimmed copy of the FastAPI lifespan both Python APIs use, which
skips Sentry on an empty DSN and logs nothing above INFO.
"""

from __future__ import annotations

from pathlib import Path

from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.deterministic.delivery import (
    check_sentry_dsn_absence_reported,
)


def _repo(tmp_path: Path, body: str, *, pyproject: bool = True) -> Path:
    if pyproject:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "svc"\n', encoding="utf-8"
        )
    path = tmp_path / "src" / "svc" / "main.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return tmp_path


def _cd038(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["rule_id"] == "CD-038"]


# --- skipped ------------------------------------------------------------------


def test_no_pyproject_is_skipped(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        'import os\nimport sentry_sdk\nsentry_sdk.init(dsn=os.getenv("X"))\n',
        pyproject=False,
    )
    assert check_sentry_dsn_absence_reported(repo) == []


def test_shared_helper_only_has_no_finding(tmp_path: Path) -> None:
    """init_sentry owns the empty-DSN branch; there is no sentry_sdk.init."""
    repo = _repo(
        tmp_path,
        "import sentry_sdk\n"
        "from mini_app_polis.sentry import init_sentry\n"
        "\n"
        'init_sentry("svc")\n'
        "sentry_sdk.capture_message('hi')\n",
    )
    assert check_sentry_dsn_absence_reported(repo) == []


# --- fails --------------------------------------------------------------------


def test_unguarded_getenv_fails_naming_module_and_variable(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        'import os\nimport sentry_sdk\n\nsentry_sdk.init(dsn=os.getenv("X"))\n',
    )
    findings = check_sentry_dsn_absence_reported(repo)
    assert len(findings) == 1
    f = findings[0]
    assert f["rule_id"] == "CD-038"
    assert f["severity"] == "WARN"
    assert f["dimension"] == "cd_readiness"
    assert "src/svc/main.py" in f["finding"]
    assert "X" in f["finding"]
    assert "nothing checks it for emptiness" in f["finding"]


def test_guard_that_only_returns_fails(tmp_path: Path) -> None:
    """db-ops' shape before the fix: the no-op written out."""
    repo = _repo(
        tmp_path,
        "import os\n"
        "import sentry_sdk\n"
        "\n"
        'SENTRY_DSN_VAR = "SENTRY_DSN_COGS"\n'
        "\n"
        "def _init_sentry() -> None:\n"
        '    dsn = os.getenv(SENTRY_DSN_VAR, "").strip()\n'
        "    if not dsn:\n"
        "        return\n"
        "    sentry_sdk.init(dsn=dsn)\n",
    )
    findings = check_sentry_dsn_absence_reported(repo)
    assert len(findings) == 1
    assert "SENTRY_DSN_COGS" in findings[0]["finding"]
    assert "`dsn`" in findings[0]["finding"]
    assert "src/svc/main.py" in findings[0]["finding"]


def test_settings_field_guard_with_no_else_fails(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        "import sentry_sdk\n"
        "from .config import get_settings\n"
        "\n"
        "async def lifespan(_app):\n"
        "    settings = get_settings()\n"
        "    if settings.SENTRY_DSN_APIS:\n"
        "        sentry_sdk.init(dsn=settings.SENTRY_DSN_APIS)\n"
        "    yield\n",
    )
    findings = check_sentry_dsn_absence_reported(repo)
    assert len(findings) == 1
    assert "Settings field SENTRY_DSN_APIS" in findings[0]["finding"]


def test_else_branch_logging_info_fails(tmp_path: Path) -> None:
    """An INFO line saying sentry=off is still not an ERROR."""
    repo = _repo(
        tmp_path,
        "import sentry_sdk\n"
        "\n"
        "def start(settings, logger):\n"
        "    if settings.SENTRY_DSN_APIS:\n"
        "        sentry_sdk.init(dsn=settings.SENTRY_DSN_APIS)\n"
        "    else:\n"
        '        logger.info("sentry=off")\n',
    )
    assert len(check_sentry_dsn_absence_reported(repo)) == 1


def test_error_log_behind_non_production_condition_fails(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        "import os\n"
        "import sentry_sdk\n"
        "\n"
        "def init(log, verbose):\n"
        '    dsn = os.environ.get("SENTRY_DSN")\n'
        "    if not dsn:\n"
        "        if verbose:\n"
        '            log.error("no dsn")\n'
        "        return\n"
        "    sentry_sdk.init(dsn=dsn)\n",
    )
    assert len(check_sentry_dsn_absence_reported(repo)) == 1


# --- passes -------------------------------------------------------------------


def test_production_gated_error_log_passes(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        "import os\n"
        "import sentry_sdk\n"
        "from mini_app_polis.environment import is_production\n"
        "\n"
        "def init(log):\n"
        '    dsn = os.getenv("SENTRY_DSN_COGS")\n'
        "    if not dsn:\n"
        "        if is_production():\n"
        '            log.error("SENTRY_DSN_COGS is empty in production")\n'
        "        return\n"
        "    sentry_sdk.init(dsn=dsn)\n",
    )
    assert check_sentry_dsn_absence_reported(repo) == []


def test_current_environment_gate_in_else_passes(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        "import sentry_sdk\n"
        "from mini_app_polis.environment import Environment, current_environment\n"
        "\n"
        "def start(settings, logger):\n"
        "    if settings.SENTRY_DSN_APIS:\n"
        "        sentry_sdk.init(dsn=settings.SENTRY_DSN_APIS)\n"
        "    elif current_environment() is Environment.PRODUCTION:\n"
        '        logger.error("SENTRY_DSN_APIS is empty")\n',
    )
    assert check_sentry_dsn_absence_reported(repo) == []


def test_guard_that_raises_passes(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        "import os\n"
        "import sentry_sdk\n"
        "\n"
        "def init():\n"
        '    dsn = os.getenv("SENTRY_DSN")\n'
        "    if not dsn:\n"
        '        raise RuntimeError("SENTRY_DSN is required")\n'
        "    sentry_sdk.init(dsn=dsn)\n",
    )
    assert check_sentry_dsn_absence_reported(repo) == []


def test_untraceable_dsn_is_not_reported(tmp_path: Path) -> None:
    """A parameter's origin is outside the module: no guess, no finding."""
    repo = _repo(
        tmp_path,
        "import sentry_sdk\n\ndef init(dsn):\n    sentry_sdk.init(dsn=dsn)\n",
    )
    assert check_sentry_dsn_absence_reported(repo) == []


# --- registration -------------------------------------------------------------


def test_runner_checks_cd038_for_runtime_services(tmp_path: Path) -> None:
    """Registered under its catalog ID, so EVAL-007 stops reporting it."""
    repo = _repo(
        tmp_path,
        'import os\nimport sentry_sdk\nsentry_sdk.init(dsn=os.getenv("X"))\n',
    )
    result = run_all_checks(repo, dod_type="new_cog", cog_subtype="pipeline")
    assert "CD-038" in result.checked_rule_ids
    assert _cd038(result.findings)


def test_runner_does_not_check_cd038_for_libraries(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "x = 1\n")
    result = run_all_checks(repo, dod_type=None, service_type="library")
    assert "CD-038" not in result.checked_rule_ids
