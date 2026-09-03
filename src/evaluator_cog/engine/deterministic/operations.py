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
from datetime import date, datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

import yaml

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
_FRONT_MATTER_RE = re.compile(r"\A\s*---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)

_RESTORE_EVIDENCE_REL = "docs/operations/restore-evidence.md"
_RESTORE_MAX_AGE_DAYS = 180
_ROTATION_LOG_REL = "docs/operations/rotation-log.md"
_ROTATION_MAX_AGE_DAYS = 365


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


def _front_matter_date(path: Path, field: str, rel: str) -> _FrontMatterDate:
    """Read `field` as a date out of the YAML front matter of `path`.

    Shared by OPS-001 and OPS-006, which are the same check with three
    constants swapped. Keeping the parsing in one place means the two
    rules cannot drift apart in how they treat a quoted date, a missing
    fence or an unreadable file — the failure modes are the fiddly part,
    not the comparison.

    PyYAML resolves an unquoted ``2026-01-15`` to a ``datetime.date``
    before we ever see it, but a quoted ``"2026-01-15"`` stays a string
    and a ``2026-01-15T09:00:00Z`` becomes a ``datetime``. All three are
    legitimate things for a human to have typed into a runbook, so all
    three are accepted and normalised to a plain ``date``. Anything else
    — a number, a list, an unparseable string — is reported rather than
    guessed at.

    `rel` is the repo-relative path used in problem sentences, so that
    findings name the file the operator has to open rather than an
    absolute path from the evaluator's temporary checkout.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _FrontMatterDate(None, {}, f"{rel} could not be read: {exc}")

    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        return _FrontMatterDate(
            None,
            {},
            f"{rel} has no YAML front matter block — expected a `---` "
            f"fenced header carrying `{field}:`",
        )

    try:
        data = yaml.safe_load(match.group("body"))
    except yaml.YAMLError as exc:
        return _FrontMatterDate(
            None, {}, f"the front matter of {rel} is not parseable YAML: {exc}"
        )

    if not isinstance(data, dict):
        return _FrontMatterDate(
            None,
            {},
            f"the front matter of {rel} is not a mapping, so `{field}:` "
            f"cannot be read from it",
        )

    raw = data.get(field)
    if raw is None:
        return _FrontMatterDate(
            None, data, f"{rel} front matter has no `{field}:` value"
        )

    if isinstance(raw, datetime):
        return _FrontMatterDate(raw.date(), data, None)
    if isinstance(raw, date):
        return _FrontMatterDate(raw, data, None)
    if isinstance(raw, str):
        candidate = raw.strip()
        for parse in (
            lambda s: date.fromisoformat(s),
            lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).date(),
        ):
            try:
                return _FrontMatterDate(parse(candidate), data, None)
            except ValueError:
                continue
        return _FrontMatterDate(
            None,
            data,
            f"{rel} front matter has `{field}: {candidate}` which is not an "
            f"ISO-8601 date (expected YYYY-MM-DD)",
        )

    return _FrontMatterDate(
        None,
        data,
        f"{rel} front matter has `{field}:` of type {type(raw).__name__}, "
        f"not a date (expected YYYY-MM-DD)",
    )


def check_ops_001(repo_path: Path, *, now: date | None = None) -> list[Finding]:
    """OPS-001: Restore evidence is current and states a measured RPO and RTO.

    A restore that was last exercised two years ago is evidence of
    nothing, and a restore-evidence page that omits its RPO and RTO
    records that somebody ran a restore without recording what it
    bought. Both are decidable from the file: the date is present or it
    is not, recent or it is not, and the two fields are populated or
    they are not.

    Deliberately absent: any judgement about the RPO and RTO *values*.
    check_notes is explicit that we must not validate them. Whether a
    four-hour RPO is appropriate for this service is a design argument
    that needs a reviewer who knows the business; whether the field is
    filled in at all is a conformance fact. Only the second belongs in a
    deterministic check, and conflating them would produce confident
    findings about numbers this checker has no basis to have an opinion
    on.

    `now` defaults to today and exists so callers — tests especially —
    can pin the evaluation date; a staleness assertion against the real
    clock is a test with an expiry date.
    """
    CHECK_ID = "OPS-001"
    findings: list[Finding] = []
    today = now or date.today()
    path = repo_path / "docs" / "operations" / "restore-evidence.md"

    if not path.is_file():
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"No restore evidence at {_RESTORE_EVIDENCE_REL} — there is "
                f"no record that a restore has ever been exercised.",
                f"Perform a restore from backup and record it in "
                f"{_RESTORE_EVIDENCE_REL} with front matter carrying "
                f"`restored_on:`, `rpo:` and `rto:`.",
            )
        )
        return findings

    parsed = _front_matter_date(path, "restored_on", _RESTORE_EVIDENCE_REL)
    if parsed.value is None:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"OPS-001 could not determine when the restore was last "
                f"exercised: {parsed.problem}.",
                f"Add `restored_on: YYYY-MM-DD` to the YAML front matter of "
                f"{_RESTORE_EVIDENCE_REL}, recording the date the restore "
                f"was actually performed.",
            )
        )
    else:
        age_days = (today - parsed.value).days
        if age_days > _RESTORE_MAX_AGE_DAYS:
            findings.append(
                _finding(
                    CHECK_ID,
                    "ERROR",
                    _DIMENSION,
                    f"{_RESTORE_EVIDENCE_REL} records `restored_on: "
                    f"{parsed.value.isoformat()}`, which is {age_days} days "
                    f"before the evaluation date {today.isoformat()} — "
                    f"{age_days - _RESTORE_MAX_AGE_DAYS} days past the "
                    f"{_RESTORE_MAX_AGE_DAYS}-day limit.",
                    f"Exercise a restore from backup and update `restored_on:` "
                    f"in {_RESTORE_EVIDENCE_REL} to the date it was performed; "
                    f"the evidence must be re-established at least every "
                    f"{_RESTORE_MAX_AGE_DAYS} days.",
                )
            )

    for field, label in (
        ("rpo", "recovery point objective"),
        ("rto", "recovery time objective"),
    ):
        if parsed.data.get(field) is None:
            findings.append(
                _finding(
                    CHECK_ID,
                    "ERROR",
                    _DIMENSION,
                    f"{_RESTORE_EVIDENCE_REL} front matter has no `{field}:` "
                    f"value — the measured {label} was not recorded.",
                    f"Add `{field}:` to the front matter of "
                    f"{_RESTORE_EVIDENCE_REL} with the {label} measured "
                    f"during the restore (for example `{field}: 15m`).",
                )
            )

    return findings


def check_ops_006(repo_path: Path, *, now: date | None = None) -> list[Finding]:
    """OPS-006: Credential rotation has been exercised within the last year.

    The same shape as OPS-001 — read a runbook, parse one date out of
    its front matter, compare it to a limit — and deliberately so: both
    call ``_front_matter_date`` rather than each growing its own idea of
    what a date in front matter looks like.

    A service that genuinely holds no machine credentials is not
    supposed to satisfy this rule with an empty log; it is supposed to
    carry a repo exemption, the way deejaytools-com-api does for CD-019.
    That distinction is a catalog-level decision, so this check does not
    try to infer "there are no credentials to rotate" from the source —
    an absent log is reported and the exemption mechanism, not the
    checker, is where a legitimate exception is recorded.
    """
    CHECK_ID = "OPS-006"
    findings: list[Finding] = []
    today = now or date.today()
    path = repo_path / "docs" / "operations" / "rotation-log.md"

    if not path.is_file():
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"No credential rotation log at {_ROTATION_LOG_REL} — there "
                f"is no record that rotation has ever been exercised.",
                f"Rotate the service's machine credentials and record it in "
                f"{_ROTATION_LOG_REL} with front matter carrying "
                f"`last_exercised: YYYY-MM-DD`, or register a repo exemption "
                f"for OPS-006 if this service holds no machine credentials.",
            )
        )
        return findings

    parsed = _front_matter_date(path, "last_exercised", _ROTATION_LOG_REL)
    if parsed.value is None:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"OPS-006 could not determine when credential rotation was "
                f"last exercised: {parsed.problem}.",
                f"Add `last_exercised: YYYY-MM-DD` to the YAML front matter "
                f"of {_ROTATION_LOG_REL}, recording the date rotation was "
                f"actually performed.",
            )
        )
        return findings

    age_days = (today - parsed.value).days
    if age_days > _ROTATION_MAX_AGE_DAYS:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"{_ROTATION_LOG_REL} records `last_exercised: "
                f"{parsed.value.isoformat()}`, which is {age_days} days "
                f"before the evaluation date {today.isoformat()} — "
                f"{age_days - _ROTATION_MAX_AGE_DAYS} days past the "
                f"{_ROTATION_MAX_AGE_DAYS}-day limit.",
                f"Rotate the service's machine credentials and update "
                f"`last_exercised:` in {_ROTATION_LOG_REL}; rotation must be "
                f"exercised at least every {_ROTATION_MAX_AGE_DAYS} days.",
            )
        )
    return findings


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

_SLO_REQUIRED_KEYS = ("name", "indicator", "target", "window")


def check_ops_003(repo_path: Path) -> list[Finding]:
    """OPS-003: Service declares a machine-readable service level objective.

    ``slo.yaml`` exists so that an objective can be read by something
    other than a human — an alert generator, a dashboard, a review
    script. That only works if each objective carries all four parts:
    what it is called, what signal it is measured from, what value it
    must hold, and over what window. Three out of four is prose with
    colons in it.

    Deliberately absent: any evaluation of attainment. check_notes says
    not to, and the file could not support it anyway — whether the
    service met its 99.9% last month is a question for the metrics
    store, not for a repo checker. This rule asks only that a
    well-formed objective exists to be measured against.

    One well-formed entry is enough to pass. A file may legitimately
    hold draft or commented-out objectives alongside the real one, and
    demanding that every entry be complete would penalise a service for
    writing more of them down.
    """
    CHECK_ID = "OPS-003"
    findings: list[Finding] = []
    path = repo_path / "slo.yaml"

    if not path.is_file():
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                "No slo.yaml at the repo root — the service declares no "
                "machine-readable service level objective.",
                "Add slo.yaml at the repo root with an `objectives:` list, "
                "each entry carrying `name`, `indicator`, `target` and "
                "`window`.",
            )
        )
        return findings

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"slo.yaml could not be parsed: {exc}",
                "Fix the YAML syntax in slo.yaml so the objectives can be "
                "read by tooling as well as by people.",
            )
        )
        return findings

    objectives = data.get("objectives") if isinstance(data, dict) else None
    if not isinstance(objectives, list) or not objectives:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                "slo.yaml declares no `objectives:` list — the file exists "
                "but carries no service level objective.",
                "Add an `objectives:` list to slo.yaml with at least one "
                "entry carrying `name`, `indicator`, `target` and `window`.",
            )
        )
        return findings

    defects: list[str] = []
    for index, entry in enumerate(objectives):
        if not isinstance(entry, dict):
            defects.append(f"objectives[{index}] is not a mapping")
            continue
        missing = [k for k in _SLO_REQUIRED_KEYS if entry.get(k) is None]
        if not missing:
            return findings  # One well-formed objective satisfies the rule.
        label = entry.get("name") or f"objectives[{index}]"
        defects.append(f"{label} is missing {', '.join(missing)}")

    findings.append(
        _finding(
            CHECK_ID,
            "WARN",
            _DIMENSION,
            f"slo.yaml has no well-formed objective — every entry is missing "
            f"at least one required key ({', '.join(_SLO_REQUIRED_KEYS)}): "
            f"{'; '.join(defects)}.",
            f"Complete at least one entry under `objectives:` in slo.yaml so "
            f"it carries all of {', '.join(_SLO_REQUIRED_KEYS)}; an objective "
            f"missing its window or indicator cannot be measured "
            f"automatically.",
        )
    )
    return findings


# --------------------------------------------------------------------------
# OPS-004 — data classification covers the schema
# --------------------------------------------------------------------------

# Top-level keys in data-classification.yaml that describe the document
# rather than a table, so that a file written as a bare table -> entry
# mapping can still be read without mistaking its header for a table.
_CLASSIFICATION_META_KEYS = frozenset(
    {"version", "updated", "owner", "notes", "service", "repo", "schema_version"}
)

_CREATE_TABLE_RE = re.compile(
    r"""create\s+table\s+(?:if\s+not\s+exists\s+)?
        ["`\[]?(?:(?P<schema>[A-Za-z0-9_]+)["`\]]?\.["`\[]?)?
        (?P<name>[A-Za-z0-9_]+)""",
    re.IGNORECASE | re.VERBOSE,
)
_DROP_TABLE_RE = re.compile(
    r"""drop\s+table\s+(?:if\s+exists\s+)?
        ["`\[]?(?:[A-Za-z0-9_]+["`\]]?\.["`\[]?)?
        (?P<name>[A-Za-z0-9_]+)""",
    re.IGNORECASE | re.VERBOSE,
)

_MIGRATION_DIR_CANDIDATES = (
    "migrations",
    "alembic/versions",
    "db/migrations",
    "drizzle",
    "supabase/migrations",
)

_ORM_TABLE_FACTORIES = ("pgTable", "sqliteTable", "mysqlTable", "Table")


def _tables_from_migrations(repo_path: Path) -> set[str]:
    """Collect table names created by the migration directory (raw-SQL repos).

    Reads both spellings a migration directory uses: ``CREATE TABLE`` in
    ``.sql`` files, and ``op.create_table("name")`` in Alembic's Python
    revisions. Tables dropped by a later migration are removed again —
    a table that was created in 2024 and dropped in 2025 is not part of
    the schema and demanding a classification entry for it would be
    exactly backwards.
    """
    created: set[str] = set()
    dropped: set[str] = set()
    for rel in _MIGRATION_DIR_CANDIDATES:
        directory = repo_path / rel
        if not directory.is_dir():
            continue
        for sql_file in sorted(directory.rglob("*.sql")):
            try:
                text = sql_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            created.update(
                m.group("name").lower() for m in _CREATE_TABLE_RE.finditer(text)
            )
            dropped.update(
                m.group("name").lower() for m in _DROP_TABLE_RE.finditer(text)
            )
        for py_file in sorted(directory.rglob("*.py")):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                attr = func.attr if isinstance(func, ast.Attribute) else None
                if attr not in ("create_table", "drop_table"):
                    continue
                if not node.args:
                    continue
                first = node.args[0]
                if not (
                    isinstance(first, ast.Constant) and isinstance(first.value, str)
                ):
                    continue
                target = created if attr == "create_table" else dropped
                target.add(first.value.lower())
    return created - dropped


def _tables_from_orm(repo_path: Path) -> set[str]:
    """Collect table names declared by an ORM schema module.

    Two dialects, because the ecosystem runs both: SQLAlchemy models set
    ``__tablename__ = "x"``, and Drizzle schemas call
    ``pgTable("x", ...)``. Both are read from the AST rather than by
    regex so that a table name mentioned in a docstring or a comment is
    not mistaken for a declaration.
    """
    tables: set[str] = set()
    src = repo_path / "src"
    roots = [src] if src.is_dir() else [repo_path]

    for root in roots:
        for py_file in sorted(root.rglob("*.py")):
            if "/tests/" in str(py_file).replace("\\", "/"):
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and any(
                        isinstance(t, ast.Name) and t.id == "__tablename__"
                        for t in node.targets
                    )
                ):
                    tables.add(node.value.value.lower())
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "Table"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    tables.add(node.args[0].value.lower())

        for ts_file in sorted(root.rglob("*.ts")):
            path_posix = str(ts_file).replace("\\", "/")
            if "/tests/" in path_posix or ts_file.name.endswith(
                (".test.ts", ".spec.ts")
            ):
                continue
            try:
                text = ts_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for factory in _ORM_TABLE_FACTORIES:
                for match in re.finditer(
                    rf"\b{factory}\s*\(\s*[\"'`]([A-Za-z0-9_]+)[\"'`]", text
                ):
                    tables.add(match.group(1).lower())
    return tables


def _classification_entries(data: Any) -> dict[str, Any]:
    """Normalise data-classification.yaml into a table-name -> entry map.

    Three spellings are accepted because all three are reasonable and
    the rule is about coverage, not about file shape: a ``tables:``
    mapping, a ``tables:`` list of entries each naming its table, and a
    bare top-level mapping of table name to entry. Document-level header
    keys are excluded from the last of those so a ``version:`` line is
    not reported as a table nobody classified.
    """
    explicit = isinstance(data, dict) and "tables" in data
    raw = data.get("tables") if explicit else data

    entries: dict[str, Any] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            name = str(key).strip().lower()
            if not name:
                continue
            # The header-key filter applies only to the bare fallback shape.
            # Under an explicit `tables:` every key is a table, and several
            # plausible table names (`notes`, `owner`, `version`) collide with
            # document header keys — dropping those would report a classified
            # table as uncovered.
            if not explicit and name in _CLASSIFICATION_META_KEYS:
                continue
            entries[name] = value
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("table") or item.get("name")
            if name:
                entries[str(name).strip().lower()] = item
    return entries


def check_ops_004(repo_path: Path) -> list[Finding]:
    """OPS-004: Data classification covers every table in the schema.

    The risk this rule is about is asymmetric, and the check is shaped
    around that asymmetry. A table nobody has classified is a table
    whose retention and handling nobody decided — it may be holding
    personal data under no policy at all, and it is invisible precisely
    because it is undocumented. A classification entry for a table that
    has since been dropped is, by contrast, merely untidy: it describes
    data that no longer exists.

    So an uncovered table and an entry missing `class` or `retention`
    are reported as failures, while a stale entry is reported as an
    advisory finding whose text says so. Both arrive at the catalog's
    WARN severity — that is the rule's declared severity and it is used
    verbatim — but the wording distinguishes the two, so a reader is
    never left guessing which findings are the ones that matter.

    The schema is read from whichever source the repo actually has: the
    migration directory for raw-SQL repos, the schema module for ORM
    repos. When neither yields a table there is nothing to cover, and
    the check reports only the file-level problems rather than
    manufacturing a pass or a failure out of an empty set.
    """
    CHECK_ID = "OPS-004"
    findings: list[Finding] = []
    path = repo_path / "data-classification.yaml"

    if not path.is_file():
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                "No data-classification.yaml at the repo root — no table in "
                "the schema has a recorded classification or retention "
                "period.",
                "Add data-classification.yaml at the repo root listing every "
                "table in the schema with a `class` and a `retention` value.",
            )
        )
        return findings

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"data-classification.yaml could not be parsed: {exc}",
                "Fix the YAML syntax in data-classification.yaml so table "
                "coverage can be checked against the schema.",
            )
        )
        return findings

    entries = _classification_entries(data)
    schema_tables = _tables_from_migrations(repo_path) or _tables_from_orm(repo_path)

    for name in sorted(schema_tables - set(entries)):
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"Table `{name}` exists in the schema but has no entry in "
                f"data-classification.yaml — its data class and retention "
                f"period are undeclared.",
                f"Add a `{name}` entry to data-classification.yaml with a "
                f"`class` (for example internal, personal, secret) and a "
                f"`retention` value for how long its rows are kept.",
            )
        )

    for name in sorted(entries):
        entry = entries[name]
        if not isinstance(entry, dict):
            findings.append(
                _finding(
                    CHECK_ID,
                    "WARN",
                    _DIMENSION,
                    f"data-classification.yaml entry for table `{name}` is "
                    f"not a mapping, so its `class` and `retention` cannot "
                    f"be read.",
                    f"Rewrite the `{name}` entry in data-classification.yaml "
                    f"as a mapping carrying `class` and `retention` keys.",
                )
            )
            continue
        missing = [k for k in ("class", "retention") if entry.get(k) is None]
        if missing:
            findings.append(
                _finding(
                    CHECK_ID,
                    "WARN",
                    _DIMENSION,
                    f"data-classification.yaml entry for table `{name}` is "
                    f"missing {', '.join(missing)}.",
                    f"Add {', '.join(missing)} to the `{name}` entry in "
                    f"data-classification.yaml; an entry without them "
                    f"records that the table exists, not how it is handled.",
                )
            )

    if schema_tables:
        for name in sorted(set(entries) - schema_tables):
            findings.append(
                _finding(
                    CHECK_ID,
                    "WARN",
                    _DIMENSION,
                    f"Stale entry (advisory, not a conformance failure): "
                    f"data-classification.yaml classifies table `{name}`, "
                    f"which no longer exists in the schema.",
                    f"Remove the `{name}` entry from "
                    f"data-classification.yaml once its table has been "
                    f"dropped, so the file keeps describing the live schema.",
                )
            )

    return findings


# --------------------------------------------------------------------------
# OPS-005 — declared egress surface
# --------------------------------------------------------------------------

_EGRESS_REL = "egress.yaml"

_LOCAL_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
)

# Fallback in-ecosystem suffixes, used only when the caller passes no
# ecosystem.yaml. These are the ecosystem's own domains; traffic to them
# is internal and is not part of the external egress surface.
_FALLBACK_ECOSYSTEM_SUFFIXES = ("kaianolevine.com", "mini-app-polis")

_URL_RE = re.compile(r"""https?://[^\s"'`)<>\]]+""")
_HOST_CONST_RE = re.compile(
    r"""(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:HOST|Host|host|DOMAIN|Domain|domain)[A-Za-z0-9_]*)
        \s*[:=]\s*["'`](?P<host>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)["'`]""",
    re.VERBOSE,
)

_SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs", ".astro")


def _is_test_path(path: Path) -> bool:
    """True for test and fixture files, which are excluded from the scan.

    check_notes says to ignore hosts appearing only in test fixtures.
    Excluding those files from the scan implements exactly that: a host
    used in real source is still found there, and a host that occurs
    nowhere else is never seen.
    """
    posix = str(path).replace("\\", "/")
    if any(seg in posix for seg in ("/tests/", "/test/", "/__tests__/", "/fixtures/")):
        return True
    name = path.name
    return (
        name.startswith("test_")
        or name.endswith(("_test.py", ".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx"))
        or name.startswith("conftest")
    )


def _normalise_host(raw: str) -> str:
    """Reduce a URL, a host:port or a bare hostname to a lowercase host."""
    value = raw.strip().strip("/").lower()
    if "://" in value:
        value = urlparse(value).netloc or value
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")]
    return value.split(":", 1)[0]


def _collect_hosts(node: Any, into: set[str]) -> None:
    """Walk a parsed YAML document collecting every host-looking string.

    egress.yaml and ecosystem.yaml are both allowed to be shaped however
    their authors found natural — a flat list of hosts, entries with a
    ``host:`` key, entries with a full ``url:``. Rather than dictate one
    shape, collect any string that resolves to something with a dot in
    it and treat that as declared. Over-collecting here can only silence
    a finding about a host the repo genuinely names somewhere in its
    declaration, which is the correct outcome.
    """
    if isinstance(node, dict):
        for value in node.values():
            _collect_hosts(value, into)
    elif isinstance(node, list):
        for value in node:
            _collect_hosts(value, into)
    elif isinstance(node, str):
        host = _normalise_host(node)
        if "." in host and " " not in host:
            into.add(host)


def _is_local_host(host: str) -> bool:
    """True for hosts that are not egress at all."""
    if not host or "." not in host:
        return True
    return host in _LOCAL_HOSTS or host.endswith(".local") or host.endswith(".internal")


def _matches_declared(host: str, declared: set[str]) -> bool:
    """True if `host` is named by, or is a subdomain of, a declared host."""
    return any(host == known or host.endswith("." + known) for known in declared)


def _is_fallback_ecosystem_reference(reference: str) -> bool:
    """True if a reference is in-ecosystem per the built-in fallback list.

    Used only when the caller passes no ecosystem.yaml. The two tokens
    are matched against the whole reference rather than against its host
    because they are not both domains: ``kaianolevine.com`` is the
    ecosystem's own domain, while ``mini-app-polis`` is its GitHub org
    and appears in the *path* of an in-ecosystem
    ``raw.githubusercontent.com`` URL. Matching only the host would
    report the ecosystem's own standards files as external egress.
    """
    low = reference.lower()
    return any(suffix in low for suffix in _FALLBACK_ECOSYSTEM_SUFFIXES)


def check_ops_005(repo_path: Path, *, ecosystem: dict | None = None) -> list[Finding]:
    """OPS-005: External egress surface is declared and covers every host in source.

    The point of ``egress.yaml`` is that somebody can answer "what does
    this service talk to?" without reading the code — for a firewall
    policy, an incident timeline, or a vendor review. The declaration is
    only worth anything if it is complete, so this check reads the hosts
    out of the source and reports the ones the declaration does not
    mention.

    Three categories are ignored, per check_notes. Localhost and its
    aliases are not egress. In-ecosystem hosts are internal traffic, and
    the caller may pass ``ecosystem.yaml`` as ``ecosystem=`` so the list
    is the real one rather than a guess; when it is not passed, the
    check falls back to skipping hosts under ``kaianolevine.com`` and
    ``mini-app-polis``. Hosts that appear only in test fixtures are
    excluded by not scanning test files at all — a host used in real
    source is still found in real source.

    INFO severity, from the catalog, and deliberately so: an undeclared
    host is a documentation gap, not a broken control. The remediation
    is one line in a YAML file.
    """
    CHECK_ID = "OPS-005"
    findings: list[Finding] = []
    path = repo_path / _EGRESS_REL

    if not path.is_file():
        findings.append(
            _finding(
                CHECK_ID,
                "INFO",
                _DIMENSION,
                f"No {_EGRESS_REL} at the repo root — the service's external "
                f"egress surface is undeclared.",
                f"Add {_EGRESS_REL} at the repo root listing every external "
                f"host the service calls, so the egress surface is reviewable "
                f"without reading the source.",
            )
        )
        return findings

    try:
        declared_doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        findings.append(
            _finding(
                CHECK_ID,
                "INFO",
                _DIMENSION,
                f"{_EGRESS_REL} could not be parsed: {exc}",
                f"Fix the YAML syntax in {_EGRESS_REL} so the declared egress "
                f"hosts can be compared against the hosts found in source.",
            )
        )
        return findings

    declared: set[str] = set()
    _collect_hosts(declared_doc, declared)

    ecosystem_hosts: set[str] = set()
    if ecosystem is not None:
        _collect_hosts(ecosystem, ecosystem_hosts)

    src = repo_path / "src"
    root = src if src.is_dir() else repo_path

    seen: dict[str, str] = {}
    for source_file in sorted(root.rglob("*")):
        if not source_file.is_file() or source_file.suffix not in _SOURCE_SUFFIXES:
            continue
        if _is_test_path(source_file):
            continue
        try:
            text = source_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(source_file.relative_to(repo_path))
        candidates = [m.group(0) for m in _URL_RE.finditer(text)]
        candidates += [m.group("host") for m in _HOST_CONST_RE.finditer(text)]
        for candidate in candidates:
            host = _normalise_host(candidate)
            if _is_local_host(host):
                continue
            if ecosystem_hosts:
                if _matches_declared(host, ecosystem_hosts):
                    continue
            elif _is_fallback_ecosystem_reference(candidate):
                continue
            if _matches_declared(host, declared):
                continue
            seen.setdefault(host, rel)

    for host in sorted(seen):
        findings.append(
            _finding(
                CHECK_ID,
                "INFO",
                _DIMENSION,
                f"External host `{host}` is reached from {seen[host]} but is "
                f"not declared in {_EGRESS_REL}.",
                f"Add `{host}` to {_EGRESS_REL} with the reason the service "
                f"calls it, or remove the call if it is no longer needed.",
            )
        )
    return findings
