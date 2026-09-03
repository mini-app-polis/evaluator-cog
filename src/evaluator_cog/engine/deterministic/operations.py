"""Operational-readiness rule checks (OPS-001..006).

Six rules, each answering one question about whether a service can be
operated — not whether it is well written, but whether the facts an
operator needs at three in the morning are written down, current, and
machine-readable:

  - OPS-001 restore evidence is recent and states an RPO and an RTO;
  - OPS-002 migrations run under a role the request path cannot use;
  - OPS-003 the service declares a machine-readable SLO;
  - OPS-004 every table in the schema has a data classification;
  - OPS-005 the external egress surface is declared and complete;
  - OPS-006 credential rotation has been exercised within the year.

Two shapes recur. OPS-001 and OPS-006 are the *dated front matter* pair:
both read a markdown runbook, pull a single date out of its YAML front
matter, and compare it to a maximum age. They differ only in path, field
name and limit, so both call ``_front_matter_date`` and neither parses
front matter itself. OPS-003, OPS-004 and OPS-005 are the *declared
artifact* trio: each reads one YAML file at the repo root, fails when it
is absent, and then asks whether its contents are complete against
something else in the repo.

Every check that compares against "now" takes an explicit ``now``
keyword. The default is ``date.today()`` so production callers need not
care, but the keyword exists so that tests can pin the evaluation date:
a staleness test written against the real clock passes today and starts
failing on some arbitrary morning months from now, which is worse than
no test at all.

Nothing here raises. Missing files are findings; unreadable files and
unparseable YAML are findings that say so rather than exceptions that
abort the run. Scope filtering by ``applies_to`` is the dispatcher's
job, not ours — these functions do not ask what kind of repo they were
pointed at.
"""

from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)

_DIMENSION = "operational_readiness"

# --------------------------------------------------------------------------
# OPS-001 / OPS-006 — dated front matter
# --------------------------------------------------------------------------

# A front matter block is the YAML between a leading `---` line and the
# next `---` line. Anything before the opening fence (a BOM, blank
# lines) is tolerated; a file whose first non-blank line is not a fence
# has no front matter at all.


class _FrontMatterDate(NamedTuple):
    """Outcome of reading one date field out of a file's front matter.

    ``value`` is the parsed date when everything went well and ``None``
    otherwise; ``data`` is the whole front matter mapping so a caller
    that needs sibling keys (OPS-001 wants ``rpo:`` and ``rto:``) does
    not have to re-read and re-parse the file; ``problem`` is a
    human-readable sentence naming what went wrong, ready to drop into a
    finding, and is ``None`` exactly when ``value`` is set.
    """

    value: date | None
    data: dict[str, Any]
    problem: str | None


# --------------------------------------------------------------------------
# OPS-002 — migration role separation
# --------------------------------------------------------------------------

# A settings field is treated as a database connection setting when its
# name carries both a "which database" token and a "this is a
# connection string" token. Requiring both keeps ordinary fields like
# `db_pool_size` or `sentry_url` out of the set.
_DB_NAME_TOKENS = ("database", "db_", "_db", "postgres", "pg_")
_DB_CONN_TOKENS = ("url", "uri", "dsn", "conn")

# Where a migration entry point can live. check_notes names three:
# an alembic env, a drizzle config, and the release workflow's migrate
# step. Each is searched as text for a reference to a settings field.
_MIGRATION_ENTRYPOINT_GLOBS = (
    "alembic.ini",
    "alembic/env.py",
    "alembic/*.py",
    "migrations/env.py",
    "migrations/*.py",
    "drizzle.config.ts",
    "drizzle.config.js",
    "drizzle.config.mjs",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)


def _is_db_connection_field(name: str) -> bool:
    """True if a settings field name reads as a database connection string."""
    low = name.lower()
    return any(t in low for t in _DB_NAME_TOKENS) and any(
        t in low for t in _DB_CONN_TOKENS
    )


def _settings_modules(src: Path) -> dict[Path, set[str]]:
    """Map each module declaring a ``*Settings`` class to its field names.

    Mirrors the collection CFG-001 does: a class named ``Settings`` or
    ending in ``Settings``, with fields taken from annotated and plain
    assignments in the class body. The map is keyed by path rather than
    flattened because OPS-002 needs to know which files are the
    declaration site — declaring a migration setting is not the same as
    reading one, and the settings module must not be flagged by step (3)
    for containing its own definition.
    """
    modules: dict[Path, set[str]] = {}
    for py_file in sorted(src.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        fields: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not (node.name == "Settings" or node.name.endswith("Settings")):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    fields.add(stmt.target.id)
                elif isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            fields.add(target.id)
        if fields:
            modules[py_file] = fields
    return modules


def _reads_setting(tree: ast.AST, field: str) -> bool:
    """True if this module reads `field` as a settings value or env var.

    AST rather than substring matching, because ``DATABASE_URL`` appears
    in comments, docstrings and unrelated log messages all over a
    codebase and none of those reach the request path. The forms that do
    are: attribute access on a ``settings`` object, ``getattr(settings,
    "field")``, an ``os.getenv``/``os.environ`` lookup of the field's
    environment-variable spelling, and importing the name directly out
    of a settings module.
    """
    env_name = field.upper()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == field
            and isinstance(node.value, ast.Name)
            and node.value.id in ("settings", "config", "cfg")
        ):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            is_getattr = isinstance(func, ast.Name) and func.id == "getattr"
            is_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr in ("getenv", "get")
                or isinstance(func, ast.Name)
                and func.id == "getenv"
            )
            for arg in node.args:
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    continue
                if is_getattr and arg.value == field:
                    return True
                if is_getenv and arg.value in (env_name, field):
                    return True
        if isinstance(node, ast.Subscript):
            sub = node.slice
            if (
                isinstance(sub, ast.Constant)
                and isinstance(sub.value, str)
                and sub.value in (env_name, field)
            ):
                return True
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == field:
                    return True
    return False


def check_ops_002(repo_path: Path) -> list[Finding]:
    """OPS-002: Migrations run under a database role without DDL on the runtime path.

    The control this rule protects is a database role separation: the
    role that can create and drop tables is not the role the request
    path authenticates as, so an application-level compromise cannot
    reshape the schema. That separation is expressed in the repo as two
    distinct connection settings, and it survives only as long as the
    second one stays out of the application.

    Three steps, in the order check_notes gives them. (1) Two connection
    settings must be declared: one setting under two names is not a
    separation, and neither is one setting. (2) The migration entry
    point — alembic env, drizzle config, or the release workflow's
    migrate step — must actually reference one of them, otherwise the
    second setting is decorative and migrations are still running as the
    runtime role. (3) No module under the application source may import
    or read the migration setting.

    Step (3) is the load-bearing one, and it is done with the AST rather
    than a substring scan for exactly the reason the rule exists: the
    string ``DATABASE_URL_MIGRATIONS`` occurs in comments, docs and log
    lines that have no bearing on what the request path can do, while an
    actual ``settings.database_url_migrations`` read is a real edge from
    the runtime process to the privileged role. Two settings that both
    reach the request path are one setting with two names.

    The declaring settings module is excluded from step (3): defining a
    field is not reading it, and flagging the declaration site would
    make the rule unsatisfiable.
    """
    CHECK_ID = "OPS-002"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    settings_modules = _settings_modules(src)
    declared_fields = {f for fields in settings_modules.values() for f in fields}
    conn_fields = sorted(f for f in declared_fields if _is_db_connection_field(f))

    if not conn_fields:
        # No database connection setting at all. Either this service does
        # not talk to a database, or its configuration is shaped in a way
        # this checker cannot read; in neither case is there evidence of a
        # collapsed role separation, so say nothing rather than guess.
        return findings

    if len(conn_fields) < 2:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"Only one database connection setting is declared "
                f"({conn_fields[0]}) — migrations and the request path "
                f"therefore share a single database role, which leaves DDL "
                f"privileges reachable from the runtime path.",
                f"Declare a second connection setting (for example "
                f"`{conn_fields[0]}_migrations`) bound to a role that owns "
                f"the schema, point the migration entry point at it, and "
                f"leave `{conn_fields[0]}` without DDL privileges.",
            )
        )
        return findings

    entrypoints: dict[str, str] = {}
    for pattern in _MIGRATION_ENTRYPOINT_GLOBS:
        for path in sorted(repo_path.glob(pattern)):
            if not path.is_file():
                continue
            try:
                entrypoints[str(path.relative_to(repo_path))] = path.read_text(
                    encoding="utf-8"
                )
            except (OSError, UnicodeDecodeError):
                continue

    if not entrypoints:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{len(conn_fields)} database connection settings are "
                f"declared ({', '.join(conn_fields)}) but no migration entry "
                f"point was found — searched for an alembic env, a drizzle "
                f"config and a release-workflow migrate step.",
                "Add an alembic env.py (or drizzle.config.ts, or a migrate "
                "step in the release workflow) that reads the migration-only "
                "connection setting, so it is provable which role runs DDL.",
            )
        )
        return findings

    # Word-boundary matching, not substring: `database_url` is a substring
    # of `database_url_migrations`, so a plain `in` test would decide that
    # an env.py referencing only the migration setting references both, and
    # step (3) would then flag every legitimate runtime read.
    migration_fields = [
        field
        for field in conn_fields
        if any(
            re.search(rf"\b{re.escape(field)}\b", text, re.IGNORECASE)
            for text in entrypoints.values()
        )
    ]

    if not migration_fields:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"No migration entry point references any declared database "
                f"connection setting. Declared: {', '.join(conn_fields)}. "
                f"Searched: {', '.join(sorted(entrypoints))}.",
                "Make the migration entry point read the migration-only "
                "connection setting explicitly (for example set "
                "sqlalchemy.url from settings.database_url_migrations in "
                "alembic/env.py) rather than inheriting the runtime URL.",
            )
        )
        return findings

    declaration_sites = {
        path
        for path, fields in settings_modules.items()
        if fields & set(migration_fields)
    }

    for py_file in sorted(src.rglob("*.py")):
        if py_file in declaration_sites:
            continue
        rel_posix = str(py_file).replace("\\", "/")
        if (
            "/tests/" in rel_posix
            or "/alembic/" in rel_posix
            or "/migrations/" in rel_posix
        ):
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        rel = py_file.relative_to(repo_path)
        for field in migration_fields:
            if _reads_setting(tree, field):
                findings.append(
                    _finding(
                        CHECK_ID,
                        "ERROR",
                        _DIMENSION,
                        f"{rel} reads the migration connection setting "
                        f"`{field}` from the application source. Two settings "
                        f"that both reach the request path are one setting "
                        f"with two names, so the DDL-capable role is "
                        f"reachable at runtime.",
                        f"Remove the `{field}` read from {rel} and use the "
                        f"runtime connection setting there; `{field}` must be "
                        f"referenced only by the migration entry point.",
                    )
                )

    return findings


# --------------------------------------------------------------------------
# OPS-003 — machine-readable SLO
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# OPS-004 — data classification covers the schema
# --------------------------------------------------------------------------

# Top-level keys in data-classification.yaml that describe the document
# rather than a table, so that a file written as a bare table -> entry
# mapping can still be read without mistaking its header for a table.


# --------------------------------------------------------------------------
# OPS-005 — declared egress surface
# --------------------------------------------------------------------------


# Fallback in-ecosystem suffixes, used only when the caller passes no
# ecosystem.yaml. These are the ecosystem's own domains; traffic to them
# is internal and is not part of the external egress surface.
