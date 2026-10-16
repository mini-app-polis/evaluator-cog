"""Deterministic operational-readiness checks: OPS-001..OPS-006.

Every staleness assertion pins the evaluation date with an explicit
``now=``. A test that measured against ``date.today()`` would pass today
and start failing on an arbitrary morning months from now, which is a
worse outcome than having no test at all — so the dated pair (OPS-001,
OPS-006) never touches the real clock.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from evaluator_cog.engine.deterministic.operations import (
    check_ops_002,
)

NOW = date(2026, 6, 1)


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _ids(findings: list[dict]) -> set[str]:
    return {f["rule_id"] for f in findings}


def _text(findings: list[dict]) -> str:
    return " || ".join(f["finding"] for f in findings)


# --- OPS-001 -----------------------------------------------------------------

RESTORE_REL = "docs/operations/restore-evidence.md"


def _restore_doc(restored_on: str, rpo: str = "15m", rto: str = "2h") -> str:
    lines = ["---", f"restored_on: {restored_on}"]
    if rpo is not None:
        lines.append(f"rpo: {rpo}")
    if rto is not None:
        lines.append(f"rto: {rto}")
    lines += ["---", "", "# Restore evidence", ""]
    return "\n".join(lines)


# --- OPS-006 -----------------------------------------------------------------

ROTATION_REL = "docs/operations/rotation-log.md"


# --- OPS-002 -----------------------------------------------------------------

_TWO_SETTINGS = (
    "from pydantic_settings import BaseSettings\n"
    "\n"
    "class Settings(BaseSettings):\n"
    "    database_url: str = ''\n"
    "    database_url_migrations: str = ''\n"
    "\n"
    "settings = Settings()\n"
)

_ALEMBIC_ENV = (
    "from app.config import settings\n"
    "\n"
    "config.set_main_option('sqlalchemy.url', settings.database_url_migrations)\n"
)


def test_ops002_returns_nothing_without_a_src_tree(tmp_path: Path) -> None:
    assert check_ops_002(tmp_path) == []


def test_ops002_silent_when_no_database_setting_is_declared(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/app/config.py",
        "class Settings:\n    log_level: str = 'INFO'\n",
    )
    assert check_ops_002(tmp_path) == []


def test_ops002_flags_a_single_connection_setting(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/app/config.py",
        "class Settings:\n    database_url: str = ''\n",
    )
    findings = check_ops_002(tmp_path)
    assert len(findings) == 1
    assert findings[0]["severity"] == "ERROR"
    assert findings[0]["dimension"] == "operational_readiness"
    assert "database_url" in findings[0]["finding"]
    assert len(findings[0]["suggestion"]) >= 40


def test_ops002_flags_absent_migration_entry_point(tmp_path: Path) -> None:
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    findings = check_ops_002(tmp_path)
    assert len(findings) == 1
    assert "no migration entry point" in findings[0]["finding"]


def test_ops002_flags_entry_point_that_references_no_setting(tmp_path: Path) -> None:
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "alembic/env.py", "config.set_main_option('sqlalchemy.url', '')\n")
    findings = check_ops_002(tmp_path)
    assert len(findings) == 1
    assert (
        "does not" in findings[0]["finding"]
        or "No migration entry point references" in findings[0]["finding"]
    )


def test_ops002_passes_when_migration_setting_stays_out_of_app_source(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "alembic/env.py", _ALEMBIC_ENV)
    _write(
        tmp_path,
        "src/app/db.py",
        "from app.config import settings\n"
        "\n"
        "def engine():\n"
        "    return create_engine(settings.database_url)\n",
    )
    assert check_ops_002(tmp_path) == []


def test_ops002_flags_app_module_reading_the_migration_setting(tmp_path: Path) -> None:
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "alembic/env.py", _ALEMBIC_ENV)
    _write(
        tmp_path,
        "src/app/admin.py",
        "from app.config import settings\n"
        "\n"
        "def rebuild():\n"
        "    return create_engine(settings.database_url_migrations)\n",
    )
    findings = check_ops_002(tmp_path)
    assert len(findings) == 1
    assert "admin.py" in findings[0]["finding"]
    assert "database_url_migrations" in findings[0]["finding"]


def test_ops002_flags_env_var_read_of_the_migration_setting(tmp_path: Path) -> None:
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "alembic/env.py", _ALEMBIC_ENV)
    _write(
        tmp_path,
        "src/app/tasks.py",
        "import os\n\nURL = os.environ['DATABASE_URL_MIGRATIONS']\n",
    )
    findings = check_ops_002(tmp_path)
    assert len(findings) == 1
    assert "tasks.py" in findings[0]["finding"]


def test_ops002_ignores_the_setting_name_in_comments_and_strings(
    tmp_path: Path,
) -> None:
    """Step (3) is AST-based: a mention is not a read."""
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "alembic/env.py", _ALEMBIC_ENV)
    _write(
        tmp_path,
        "src/app/notes.py",
        "# database_url_migrations must never be read here.\n"
        'DOC = """See DATABASE_URL_MIGRATIONS in the runbook."""\n',
    )
    assert check_ops_002(tmp_path) == []


def test_ops002_release_workflow_counts_as_a_migration_entry_point(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(
        tmp_path,
        ".github/workflows/release.yml",
        "jobs:\n"
        "  migrate:\n"
        "    steps:\n"
        "      - run: alembic upgrade head\n"
        "        env:\n"
        "          DATABASE_URL_MIGRATIONS: ${{ secrets.DATABASE_URL_MIGRATIONS }}\n",
    )
    assert check_ops_002(tmp_path) == []


_MIGRATION_SCRIPT = (
    "import os\n"
    "\n"
    "url = os.environ.get('DATABASE_URL_MIGRATIONS') or os.environ['DATABASE_URL']\n"
)


def test_ops002_dockerfile_cmd_counts_as_a_migration_entry_point(
    tmp_path: Path,
) -> None:
    """MINI-78: the start command lives in the image's CMD, not railway.json."""
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "scripts/apply_migrations.py", _MIGRATION_SCRIPT)
    _write(
        tmp_path,
        "Dockerfile",
        "FROM python:3.11-slim\n"
        'CMD ["/bin/sh", "-c", "python scripts/apply_migrations.py '
        '&& exec uvicorn app.main:app --port ${PORT:-8080}"]\n',
    )
    assert check_ops_002(tmp_path) == []


def test_ops002_dockerfile_shell_form_cmd_counts(tmp_path: Path) -> None:
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "scripts/apply_migrations.py", _MIGRATION_SCRIPT)
    _write(
        tmp_path,
        "Dockerfile",
        "FROM python:3.11-slim\n"
        "CMD python scripts/apply_migrations.py && exec uvicorn app.main:app\n",
    )
    assert check_ops_002(tmp_path) == []


def test_ops002_dockerfile_cmd_running_a_script_that_reads_neither_setting(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "scripts/apply_migrations.py", "print('migrating')\n")
    _write(
        tmp_path,
        "Dockerfile",
        "FROM python:3.11-slim\n"
        'CMD ["/bin/sh", "-c", "python scripts/apply_migrations.py && uvicorn x"]\n',
    )
    findings = check_ops_002(tmp_path)
    assert len(findings) == 1
    assert "No migration entry point references" in findings[0]["finding"]
    assert "scripts/apply_migrations.py" in findings[0]["finding"]


def test_ops002_only_the_final_dockerfile_stage_counts(tmp_path: Path) -> None:
    """A CMD in a builder stage never runs, so it names no entry point."""
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "scripts/apply_migrations.py", _MIGRATION_SCRIPT)
    _write(
        tmp_path,
        "Dockerfile",
        "FROM python:3.11 AS builder\n"
        "CMD python scripts/apply_migrations.py\n"
        "FROM python:3.11-slim\n"
        'CMD ["uvicorn", "app.main:app"]\n',
    )
    findings = check_ops_002(tmp_path)
    assert len(findings) == 1
    assert "no migration entry point" in findings[0]["finding"]


def test_ops002_railway_json_start_command_still_counts(tmp_path: Path) -> None:
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "scripts/apply_migrations.py", _MIGRATION_SCRIPT)
    _write(
        tmp_path,
        "railway.json",
        '{"deploy": {"startCommand": '
        '"python scripts/apply_migrations.py && uvicorn app.main:app"}}\n',
    )
    assert check_ops_002(tmp_path) == []


def test_ops002_railway_iac_start_command_counts(tmp_path: Path) -> None:
    _write(tmp_path, "src/app/config.py", _TWO_SETTINGS)
    _write(tmp_path, "scripts/apply_migrations.py", _MIGRATION_SCRIPT)
    _write(
        tmp_path,
        ".railway/railway.ts",
        "export default defineRailway({\n"
        '  deploy: { startCommand: "python scripts/apply_migrations.py && uvicorn x" },\n'
        "});\n",
    )
    assert check_ops_002(tmp_path) == []


# --- OPS-003 -----------------------------------------------------------------

_GOOD_SLO = (
    "objectives:\n"
    "  - name: availability\n"
    "    indicator: http_success_ratio\n"
    "    target: 99.9\n"
    "    window: 30d\n"
)


# --- OPS-004 -----------------------------------------------------------------

_MIGRATION_SQL = (
    "CREATE TABLE users (id uuid primary key);\n"
    "CREATE TABLE IF NOT EXISTS public.notes (id uuid primary key);\n"
)


def _classification(body: str) -> str:
    return "tables:\n" + body


# --- OPS-005 -----------------------------------------------------------------
