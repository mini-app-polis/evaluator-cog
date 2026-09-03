"""Deterministic operational-readiness checks: OPS-001..OPS-006.

Every staleness assertion pins the evaluation date with an explicit
``now=``. A test that measured against ``date.today()`` would pass today
and start failing on an arbitrary morning months from now, which is a
worse outcome than having no test at all — so the dated pair (OPS-001,
OPS-006) never touches the real clock.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from evaluator_cog.engine.deterministic.operations import (
    check_ops_001,
    check_ops_002,
    check_ops_003,
    check_ops_004,
    check_ops_005,
    check_ops_006,
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


def test_ops001_passes_with_recent_evidence_and_both_objectives(tmp_path: Path) -> None:
    _write(tmp_path, RESTORE_REL, _restore_doc((NOW - timedelta(days=10)).isoformat()))
    assert check_ops_001(tmp_path, now=NOW) == []


def test_ops001_flags_absent_file(tmp_path: Path) -> None:
    findings = check_ops_001(tmp_path, now=NOW)
    assert _ids(findings) == {"OPS-001"}
    assert RESTORE_REL in _text(findings)
    assert findings[0]["severity"] == "ERROR"
    assert findings[0]["dimension"] == "operational_readiness"
    assert len(findings[0]["suggestion"]) >= 40


def test_ops001_flags_missing_restored_on(tmp_path: Path) -> None:
    _write(tmp_path, RESTORE_REL, "---\nrpo: 15m\nrto: 2h\n---\n\n# Restore\n")
    findings = check_ops_001(tmp_path, now=NOW)
    assert len(findings) == 1
    assert "restored_on" in findings[0]["finding"]


def test_ops001_flags_absent_front_matter(tmp_path: Path) -> None:
    _write(tmp_path, RESTORE_REL, "# Restore evidence\n\nWe restored it once.\n")
    findings = check_ops_001(tmp_path, now=NOW)
    # No front matter at all: the date is unreadable and rpo/rto are absent.
    assert len(findings) == 3
    assert "front matter" in _text(findings)


def test_ops001_accepts_date_exactly_at_the_180_day_limit(tmp_path: Path) -> None:
    """180 days old is 'not more than 180 days before' — it passes."""
    _write(tmp_path, RESTORE_REL, _restore_doc((NOW - timedelta(days=180)).isoformat()))
    assert check_ops_001(tmp_path, now=NOW) == []


def test_ops001_flags_date_one_day_over_the_limit(tmp_path: Path) -> None:
    stale = NOW - timedelta(days=181)
    _write(tmp_path, RESTORE_REL, _restore_doc(stale.isoformat()))
    findings = check_ops_001(tmp_path, now=NOW)
    assert len(findings) == 1
    text = findings[0]["finding"]
    assert stale.isoformat() in text  # names the parsed date
    assert "181 days" in text  # names how stale it is
    assert "1 days past" in text  # and by how much it overruns


def test_ops001_parses_a_quoted_date_string(tmp_path: Path) -> None:
    """PyYAML leaves a quoted date as a str; the helper must still parse it."""
    stale = NOW - timedelta(days=400)
    _write(tmp_path, RESTORE_REL, _restore_doc(f'"{stale.isoformat()}"'))
    findings = check_ops_001(tmp_path, now=NOW)
    assert stale.isoformat() in _text(findings)


def test_ops001_flags_unparseable_date(tmp_path: Path) -> None:
    _write(tmp_path, RESTORE_REL, _restore_doc("last summer"))
    findings = check_ops_001(tmp_path, now=NOW)
    assert len(findings) == 1
    assert "ISO-8601" in findings[0]["finding"]


def test_ops001_flags_missing_rpo_and_rto(tmp_path: Path) -> None:
    _write(
        tmp_path,
        RESTORE_REL,
        f"---\nrestored_on: {(NOW - timedelta(days=1)).isoformat()}\n---\n",
    )
    findings = check_ops_001(tmp_path, now=NOW)
    assert len(findings) == 2
    assert "`rpo:`" in _text(findings)
    assert "`rto:`" in _text(findings)


def test_ops001_flags_null_rto(tmp_path: Path) -> None:
    _write(
        tmp_path,
        RESTORE_REL,
        f"---\nrestored_on: {(NOW - timedelta(days=1)).isoformat()}\nrpo: 15m\nrto:\n---\n",
    )
    findings = check_ops_001(tmp_path, now=NOW)
    assert len(findings) == 1
    assert "`rto:`" in findings[0]["finding"]


def test_ops001_does_not_judge_the_rpo_and_rto_values(tmp_path: Path) -> None:
    """check_notes forbids validating the values — an absurd RPO still passes."""
    _write(
        tmp_path,
        RESTORE_REL,
        _restore_doc((NOW - timedelta(days=1)).isoformat(), rpo="9 years", rto="never"),
    )
    assert check_ops_001(tmp_path, now=NOW) == []


# --- OPS-006 -----------------------------------------------------------------

ROTATION_REL = "docs/operations/rotation-log.md"


def test_ops006_passes_with_recent_rotation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ROTATION_REL,
        f"---\nlast_exercised: {(NOW - timedelta(days=30)).isoformat()}\n---\n",
    )
    assert check_ops_006(tmp_path, now=NOW) == []


def test_ops006_flags_absent_file(tmp_path: Path) -> None:
    findings = check_ops_006(tmp_path, now=NOW)
    assert _ids(findings) == {"OPS-006"}
    assert findings[0]["severity"] == "WARN"
    assert findings[0]["dimension"] == "operational_readiness"
    assert ROTATION_REL in findings[0]["finding"]
    assert len(findings[0]["suggestion"]) >= 40


def test_ops006_flags_missing_last_exercised(tmp_path: Path) -> None:
    _write(tmp_path, ROTATION_REL, "---\nowner: platform\n---\n\n# Rotation log\n")
    findings = check_ops_006(tmp_path, now=NOW)
    assert len(findings) == 1
    assert "last_exercised" in findings[0]["finding"]


def test_ops006_accepts_date_exactly_at_the_365_day_limit(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ROTATION_REL,
        f"---\nlast_exercised: {(NOW - timedelta(days=365)).isoformat()}\n---\n",
    )
    assert check_ops_006(tmp_path, now=NOW) == []


def test_ops006_flags_date_one_day_over_the_limit(tmp_path: Path) -> None:
    stale = NOW - timedelta(days=366)
    _write(tmp_path, ROTATION_REL, f"---\nlast_exercised: {stale.isoformat()}\n---\n")
    findings = check_ops_006(tmp_path, now=NOW)
    assert len(findings) == 1
    text = findings[0]["finding"]
    assert stale.isoformat() in text
    assert "366 days" in text
    assert "1 days past" in text


def test_ops006_shares_the_front_matter_parser_with_ops001(tmp_path: Path) -> None:
    """A datetime (not a date) in front matter is normalised for both rules."""
    stale = NOW - timedelta(days=500)
    _write(
        tmp_path,
        ROTATION_REL,
        f"---\nlast_exercised: {stale.isoformat()}T09:30:00Z\n---\n",
    )
    findings = check_ops_006(tmp_path, now=NOW)
    assert stale.isoformat() in _text(findings)


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


# --- OPS-003 -----------------------------------------------------------------

_GOOD_SLO = (
    "objectives:\n"
    "  - name: availability\n"
    "    indicator: http_success_ratio\n"
    "    target: 99.9\n"
    "    window: 30d\n"
)


def test_ops003_passes_with_a_well_formed_objective(tmp_path: Path) -> None:
    _write(tmp_path, "slo.yaml", _GOOD_SLO)
    assert check_ops_003(tmp_path) == []


def test_ops003_flags_absent_file(tmp_path: Path) -> None:
    findings = check_ops_003(tmp_path)
    assert _ids(findings) == {"OPS-003"}
    assert findings[0]["severity"] == "WARN"
    assert findings[0]["dimension"] == "operational_readiness"
    assert "slo.yaml" in findings[0]["finding"]
    assert len(findings[0]["suggestion"]) >= 40


def test_ops003_flags_unparseable_yaml(tmp_path: Path) -> None:
    _write(tmp_path, "slo.yaml", "objectives: [\n  - name: broken\n")
    findings = check_ops_003(tmp_path)
    assert len(findings) == 1
    assert "could not be parsed" in findings[0]["finding"]


def test_ops003_flags_missing_objectives_list(tmp_path: Path) -> None:
    _write(tmp_path, "slo.yaml", "service: api\n")
    findings = check_ops_003(tmp_path)
    assert len(findings) == 1
    assert "objectives" in findings[0]["finding"]


def test_ops003_flags_empty_objectives_list(tmp_path: Path) -> None:
    _write(tmp_path, "slo.yaml", "objectives: []\n")
    assert len(check_ops_003(tmp_path)) == 1


def test_ops003_flags_objective_missing_required_keys(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "slo.yaml",
        "objectives:\n  - name: availability\n    target: 99.9\n",
    )
    findings = check_ops_003(tmp_path)
    assert len(findings) == 1
    text = findings[0]["finding"]
    assert "availability" in text
    assert "indicator" in text and "window" in text


def test_ops003_one_complete_objective_is_enough(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "slo.yaml",
        "objectives:\n  - name: draft\n" + _GOOD_SLO.replace("objectives:\n", ""),
    )
    assert check_ops_003(tmp_path) == []


def test_ops003_does_not_evaluate_attainment(tmp_path: Path) -> None:
    """An objective declared as already breached is still well-formed."""
    _write(
        tmp_path,
        "slo.yaml",
        "objectives:\n"
        "  - name: availability\n"
        "    indicator: http_success_ratio\n"
        "    target: 99.9\n"
        "    window: 30d\n"
        "    current: 12.0\n",
    )
    assert check_ops_003(tmp_path) == []


# --- OPS-004 -----------------------------------------------------------------

_MIGRATION_SQL = (
    "CREATE TABLE users (id uuid primary key);\n"
    "CREATE TABLE IF NOT EXISTS public.notes (id uuid primary key);\n"
)


def _classification(body: str) -> str:
    return "tables:\n" + body


def test_ops004_passes_with_full_coverage(tmp_path: Path) -> None:
    _write(tmp_path, "migrations/0001_init.sql", _MIGRATION_SQL)
    _write(
        tmp_path,
        "data-classification.yaml",
        _classification(
            "  users:\n    class: personal\n    retention: 5y\n"
            "  notes:\n    class: internal\n    retention: 1y\n"
        ),
    )
    assert check_ops_004(tmp_path) == []


def test_ops004_flags_absent_file(tmp_path: Path) -> None:
    findings = check_ops_004(tmp_path)
    assert _ids(findings) == {"OPS-004"}
    assert findings[0]["severity"] == "WARN"
    assert findings[0]["dimension"] == "operational_readiness"
    assert "data-classification.yaml" in findings[0]["finding"]
    assert len(findings[0]["suggestion"]) >= 40


def test_ops004_flags_unparseable_yaml(tmp_path: Path) -> None:
    _write(tmp_path, "data-classification.yaml", "tables: [\n  users\n")
    findings = check_ops_004(tmp_path)
    assert len(findings) == 1
    assert "could not be parsed" in findings[0]["finding"]


def test_ops004_flags_table_absent_from_the_classification(tmp_path: Path) -> None:
    _write(tmp_path, "migrations/0001_init.sql", _MIGRATION_SQL)
    _write(
        tmp_path,
        "data-classification.yaml",
        _classification("  users:\n    class: personal\n    retention: 5y\n"),
    )
    findings = check_ops_004(tmp_path)
    assert len(findings) == 1
    assert "`notes`" in findings[0]["finding"]
    assert "no entry" in findings[0]["finding"]


def test_ops004_flags_entry_missing_class_or_retention(tmp_path: Path) -> None:
    _write(tmp_path, "migrations/0001_init.sql", "CREATE TABLE users (id uuid);\n")
    _write(
        tmp_path,
        "data-classification.yaml",
        _classification("  users:\n    class: personal\n"),
    )
    findings = check_ops_004(tmp_path)
    assert len(findings) == 1
    assert "missing retention" in findings[0]["finding"]


def test_ops004_reports_stale_entry_as_advisory_not_failure(tmp_path: Path) -> None:
    """Step (4): an entry for a dropped table is untidy, not a risk."""
    _write(tmp_path, "migrations/0001_init.sql", "CREATE TABLE users (id uuid);\n")
    _write(
        tmp_path,
        "data-classification.yaml",
        _classification(
            "  users:\n    class: personal\n    retention: 5y\n"
            "  legacy_sessions:\n    class: internal\n    retention: 30d\n"
        ),
    )
    findings = check_ops_004(tmp_path)
    assert len(findings) == 1
    text = findings[0]["finding"]
    assert "legacy_sessions" in text
    assert "Stale entry (advisory, not a conformance failure)" in text
    # Distinguishable from the uncovered-table wording.
    assert "no entry in" not in text


def test_ops004_ignores_a_table_dropped_by_a_later_migration(tmp_path: Path) -> None:
    _write(tmp_path, "migrations/0001_init.sql", "CREATE TABLE users (id uuid);\n")
    _write(tmp_path, "migrations/0002_drop.sql", "DROP TABLE IF EXISTS users;\n")
    _write(tmp_path, "data-classification.yaml", "tables: {}\n")
    assert check_ops_004(tmp_path) == []


def test_ops004_reads_alembic_python_revisions(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "alembic/versions/0001_init.py",
        "def upgrade():\n    op.create_table('audit_events', sa.Column('id'))\n",
    )
    _write(tmp_path, "data-classification.yaml", "tables: {}\n")
    findings = check_ops_004(tmp_path)
    assert len(findings) == 1
    assert "audit_events" in findings[0]["finding"]


def test_ops004_reads_an_orm_schema_module(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/app/models.py",
        "class Note(Base):\n    __tablename__ = 'notes'\n",
    )
    _write(tmp_path, "data-classification.yaml", "tables: {}\n")
    findings = check_ops_004(tmp_path)
    assert len(findings) == 1
    assert "`notes`" in findings[0]["finding"]


def test_ops004_accepts_a_list_shaped_classification_file(tmp_path: Path) -> None:
    _write(tmp_path, "migrations/0001_init.sql", "CREATE TABLE users (id uuid);\n")
    _write(
        tmp_path,
        "data-classification.yaml",
        "tables:\n  - table: users\n    class: personal\n    retention: 5y\n",
    )
    assert check_ops_004(tmp_path) == []


# --- OPS-005 -----------------------------------------------------------------


def test_ops005_passes_when_every_host_is_declared(tmp_path: Path) -> None:
    _write(tmp_path, "egress.yaml", "hosts:\n  - api.stripe.com\n")
    _write(
        tmp_path,
        "src/app/billing.py",
        'BASE = "https://api.stripe.com/v1"\n',
    )
    assert check_ops_005(tmp_path) == []


def test_ops005_flags_absent_file(tmp_path: Path) -> None:
    findings = check_ops_005(tmp_path)
    assert _ids(findings) == {"OPS-005"}
    assert findings[0]["severity"] == "INFO"
    assert findings[0]["dimension"] == "operational_readiness"
    assert "egress.yaml" in findings[0]["finding"]
    assert len(findings[0]["suggestion"]) >= 40


def test_ops005_flags_unparseable_yaml(tmp_path: Path) -> None:
    _write(tmp_path, "egress.yaml", "hosts: [\n  - a\n")
    findings = check_ops_005(tmp_path)
    assert len(findings) == 1
    assert "could not be parsed" in findings[0]["finding"]


def test_ops005_flags_an_undeclared_host(tmp_path: Path) -> None:
    _write(tmp_path, "egress.yaml", "hosts:\n  - api.stripe.com\n")
    _write(
        tmp_path,
        "src/app/mail.py",
        'SENDER = "https://api.postmarkapp.com/email"\n',
    )
    findings = check_ops_005(tmp_path)
    assert len(findings) == 1
    text = findings[0]["finding"]
    assert "api.postmarkapp.com" in text
    assert "mail.py" in text


def test_ops005_flags_a_bare_host_constant(tmp_path: Path) -> None:
    _write(tmp_path, "egress.yaml", "hosts: []\n")
    _write(tmp_path, "src/app/dns.py", 'RESOLVER_HOST = "dns.quad9.net"\n')
    findings = check_ops_005(tmp_path)
    assert len(findings) == 1
    assert "dns.quad9.net" in findings[0]["finding"]


def test_ops005_ignores_localhost(tmp_path: Path) -> None:
    _write(tmp_path, "egress.yaml", "hosts: []\n")
    _write(
        tmp_path,
        "src/app/dev.py",
        'DEV = "http://localhost:8000/health"\nALT = "http://127.0.0.1:5432"\n',
    )
    assert check_ops_005(tmp_path) == []


def test_ops005_ignores_ecosystem_hosts_from_the_fallback_list(tmp_path: Path) -> None:
    _write(tmp_path, "egress.yaml", "hosts: []\n")
    _write(
        tmp_path,
        "src/app/client.py",
        'API = "https://api.kaianolevine.com/v1"\n'
        'RAW = "https://raw.githubusercontent.com/mini-app-polis/x/main/a.yaml"\n',
    )
    assert check_ops_005(tmp_path) == []


def test_ops005_ignores_hosts_named_in_a_supplied_ecosystem(tmp_path: Path) -> None:
    _write(tmp_path, "egress.yaml", "hosts: []\n")
    _write(tmp_path, "src/app/client.py", 'API = "https://api.internal-corp.net/v1"\n')
    ecosystem = {"services": [{"id": "api", "url": "https://api.internal-corp.net"}]}
    assert check_ops_005(tmp_path, ecosystem=ecosystem) == []


def test_ops005_ignores_hosts_appearing_only_in_test_fixtures(tmp_path: Path) -> None:
    _write(tmp_path, "egress.yaml", "hosts: []\n")
    _write(tmp_path, "src/app/client.py", "TIMEOUT = 5\n")
    _write(
        tmp_path,
        "src/app/tests/fixtures/responses.py",
        'STUB = "https://fixture-only.example.com/api"\n',
    )
    assert check_ops_005(tmp_path) == []


def test_ops005_still_flags_a_host_used_in_source_and_tests(tmp_path: Path) -> None:
    _write(tmp_path, "egress.yaml", "hosts: []\n")
    _write(tmp_path, "src/app/client.py", 'API = "https://api.example.org/v1"\n')
    _write(
        tmp_path,
        "src/app/tests/test_client.py",
        'STUB = "https://api.example.org/v1"\n',
    )
    findings = check_ops_005(tmp_path)
    assert len(findings) == 1
    assert "api.example.org" in findings[0]["finding"]


def test_ops005_accepts_a_declaration_written_as_full_urls(tmp_path: Path) -> None:
    _write(tmp_path, "egress.yaml", "allowed:\n  - url: https://api.stripe.com/v1\n")
    _write(tmp_path, "src/app/billing.py", 'BASE = "https://api.stripe.com/v1"\n')
    assert check_ops_005(tmp_path) == []
