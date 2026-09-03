"""FastAPI / data-store / HTTP API rule checks."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
    _is_checker_self_source,
    _is_inside_string_literal,
)


def check_railway_hosted_api(
    repo_path: Path, *, language: str = "python"
) -> list[Finding]:
    """API-001: API services are hosted on Railway (deterministic slice).

    Implements Railway deployment artifact presence (condition 1) and
    framework dependency presence (condition 3). Workflow-based checks for
    competing hosts belong to other rules.

    TODO(API-001-condition-2): ecosystem.yaml per-service ``host: railway`` is
    deferred — requires threading service context through deterministic checks.
    """
    CHECK_ID = "API-001"
    findings: list[Finding] = []
    has_railway = (
        (repo_path / "railway.toml").exists()
        or (repo_path / "railway.json").exists()
        or (repo_path / "nixpacks.toml").exists()
    )
    if not has_railway:
        findings.append(
            _finding(
                "API-001",
                "WARN",
                "structural_conformance",
                "Railway deployment configuration is missing (expected railway.toml, railway.json, or nixpacks.toml at repo root).",
                "Add Railway configuration so deployments are explicit and reviewable.",
            )
        )

    if language == "python":
        pyproject = repo_path / "pyproject.toml"
        py_text = pyproject.read_text().lower() if pyproject.exists() else ""
        req = repo_path / "requirements.txt"
        req_text = req.read_text().lower() if req.exists() else ""
        if "fastapi" not in py_text + "\n" + req_text:
            findings.append(
                _finding(
                    "API-001",
                    "WARN",
                    "structural_conformance",
                    "FastAPI is not declared for this Python API service.",
                    "Declare fastapi in pyproject.toml or requirements.txt dependencies.",
                )
            )
    else:
        pkg = repo_path / "package.json"
        pkg_text = pkg.read_text().lower() if pkg.exists() else ""
        if "hono" not in pkg_text:
            findings.append(
                _finding(
                    "API-001",
                    "WARN",
                    "structural_conformance",
                    "Hono is not declared for this TypeScript API service.",
                    "Add hono to package.json dependencies.",
                )
            )
    return findings


_NON_POSTGRES_STORE_MARKERS_PY = (
    "mysql",
    "mysqlclient",
    "pymysql",
    "aiosqlite",
    "sqlite3",
    "sqlalchemy[sqlite]",
    "mongodb",
    "motor",
    "pymongo",
    "dynamodb",
    "boto3",
)


def check_postgres_only_data_store(
    repo_path: Path, *, language: str = "python"
) -> list[Finding]:
    """API-002: PostgreSQL as the only primary relational data store.

    Scans declared Python and Node dependencies for obvious non-Postgres
    primary-store clients. Redis as a cache alongside Postgres is a judgment
    call — a bare ``redis`` dependency still flags here; narrow exemptions
    belong in evaluator.yaml when justified.
    """
    CHECK_ID = "API-002"
    findings: list[Finding] = []
    if language == "python":
        combined = ""
        for rel in (
            "pyproject.toml",
            "requirements.txt",
            "requirements/base.txt",
            "requirements/prod.txt",
        ):
            p = repo_path / rel
            if p.exists():
                combined += "\n" + p.read_text().lower()
        for marker in _NON_POSTGRES_STORE_MARKERS_PY:
            if marker in combined:
                findings.append(
                    _finding(
                        "API-002",
                        "ERROR",
                        "structural_conformance",
                        f"Non-Postgres data-store client or driver signal detected ({marker!r}).",
                        "Standardize on PostgreSQL as the primary relational store; remove alternate DB drivers unless formally excepted.",
                    )
                )
                break
    else:
        pkg = repo_path / "package.json"
        if not pkg.exists():
            return findings
        text = pkg.read_text().lower()
        node_markers = (
            '"mysql"',
            '"mysql2"',
            '"sqlite3"',
            '"better-sqlite3"',
            '"mongodb"',
            '"mongoose"',
            '"redis"',
            '"ioredis"',
            '"dynamodb"',
        )
        for marker in node_markers:
            if marker in text:
                findings.append(
                    _finding(
                        "API-002",
                        "ERROR",
                        "structural_conformance",
                        f"Non-Postgres data-store dependency present ({marker}).",
                        "Use PostgreSQL with an approved client (e.g. drizzle + postgres).",
                    )
                )
                break
    return findings


def check_response_shape_parity(
    repo_path: Path, *, language: str = "python"
) -> list[Finding]:
    """XSTACK-002: HTTP handlers expose typed response models / helpers."""
    CHECK_ID = "XSTACK-002"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    if language == "python":
        for py in src.rglob("*.py"):
            if "tests/" in str(py).replace("\\", "/"):
                continue
            try:
                text = py.read_text()
            except OSError:
                continue
            if not re.search(
                r"@(?:router|app)\.(get|post|put|delete|patch)\s*\(", text
            ):
                continue
            if "response_model=" not in text:
                findings.append(
                    _finding(
                        "XSTACK-002",
                        "WARN",
                        "cross_repo_coherence",
                        f"FastAPI route missing response_model= in {py.relative_to(repo_path)}.",
                        "Declare response_model (or return type) for every public route.",
                    )
                )
                break
    else:
        for ts in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
            ts_path_str = str(ts).replace("\\", "/")
            # Skip test code regardless of layout:
            #   - any file under a tests/ or test/ directory
            #   - any *.test.ts or *.test.tsx file (Vitest/Jest convention)
            if (
                "/tests/" in ts_path_str
                or "/test/" in ts_path_str
                or ts.name.endswith(".test.ts")
                or ts.name.endswith(".test.tsx")
            ):
                continue
            try:
                text = ts.read_text()
            except OSError:
                continue
            if not re.search(r"\bc\.json\s*\(", text):
                continue
            if "success(" in text or re.search(
                r"from\s+['\"][^'\"]*success", text, re.I
            ):
                continue
            findings.append(
                _finding(
                    "XSTACK-002",
                    "WARN",
                    "cross_repo_coherence",
                    f"Hono handler uses raw c.json without success()/error() helper ({ts.relative_to(repo_path)}).",
                    "Wrap JSON responses with the shared success()/error() helpers.",
                )
            )
            break
    return findings


def check_db_writes_use_upserts(repo_path: Path) -> list[Finding]:
    """PIPE-002: Database writes should use upsert / ON CONFLICT patterns.

    Skips the deterministic checker's own source — see
    _is_checker_self_source for the rationale (checker pattern literals
    like ``"session.add("`` trigger meta-self-detection when scanning
    evaluator-cog itself).
    """
    CHECK_ID = "PIPE-002"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    for py in src.rglob("*.py"):
        if _is_checker_self_source(py):
            continue
        if "tests/" in str(py).replace("\\", "/"):
            continue
        try:
            text = py.read_text()
        except OSError:
            continue
        if (
            "session.add(" in text
            and "on_conflict" not in text.lower()
            and "merge(" not in text
        ):
            findings.append(
                _finding(
                    "PIPE-002",
                    "WARN",
                    "pipeline_consistency",
                    f"session.add() without merge()/on_conflict in {py.relative_to(repo_path)}.",
                    "Prefer upsert patterns (merge or ON CONFLICT) for idempotent writes.",
                )
            )
        if (
            re.search(r"\bINSERT\s+INTO\b", text, re.I)
            and "ON CONFLICT" not in text.upper()
        ):
            findings.append(
                _finding(
                    "PIPE-002",
                    "WARN",
                    "pipeline_consistency",
                    f"Raw INSERT without ON CONFLICT in {py.relative_to(repo_path)}.",
                    "Use INSERT ... ON CONFLICT for idempotent persistence.",
                )
            )
    return findings


def check_inputs_not_deleted(repo_path: Path) -> list[Finding]:
    """PIPE-005: Input files must not be deleted or moved to trash."""
    CHECK_ID = "PIPE-005"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    for path in list(src.rglob("*.py")) + list(src.rglob("*.ts")):
        if "tests/" in str(path).replace("\\", "/"):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        has_delete = ".files().delete(" in text or "files().delete(" in text
        if has_delete and not (
            _is_inside_string_literal(text, ".files().delete(")
            and _is_inside_string_literal(text, "files().delete(")
        ):
            findings.append(
                _finding(
                    "PIPE-005",
                    "WARN",
                    "structural_conformance",
                    f"Drive files().delete() referenced in {path.relative_to(repo_path)}.",
                    "Never delete raw input artifacts from Drive — move to derived outputs only.",
                )
            )
        if (
            "trashed" in text.lower()
            and "update" in text.lower()
            and "files()" in text
            and not _is_inside_string_literal(text, "trashed")
        ):
            findings.append(
                _finding(
                    "PIPE-005",
                    "WARN",
                    "structural_conformance",
                    f"Potential Drive trash update on input file in {path.relative_to(repo_path)}.",
                    "Avoid trashing upstream inputs; operate on copies.",
                )
            )
        if re.search(r"os\.(remove|unlink)\(|shutil\.rmtree\(", text) and re.search(
            r"\b(input_path|input_file|source_path|src_path|local_path)\b", text
        ):
            findings.append(
                _finding(
                    "PIPE-005",
                    "WARN",
                    "structural_conformance",
                    f"os.remove/unlink/rmtree may target input paths ({path.relative_to(repo_path)}).",
                    "Only remove scratch/temp paths — never input variables.",
                )
            )
    return findings


def check_orm_usage(repo_path: Path, language: str = "python") -> list[Finding]:
    """API-003: ORM usage required; no raw SQL outside ORM."""
    CHECK_ID = "API-003"
    findings: list[Finding] = []
    if language == "python":
        pyproject = repo_path / "pyproject.toml"
        py_text = pyproject.read_text().lower() if pyproject.exists() else ""
        if "sqlalchemy" not in py_text:
            findings.append(
                _finding(
                    "API-003",
                    "WARN",
                    "structural_conformance",
                    "api-service (Python) does not declare sqlalchemy in pyproject.toml.",
                    "Add sqlalchemy to dependencies and declare models via ORM.",
                )
            )
    else:
        pkg = repo_path / "package.json"
        pkg_text = pkg.read_text().lower() if pkg.exists() else ""
        if "drizzle-orm" not in pkg_text and "prisma" not in pkg_text:
            findings.append(
                _finding(
                    "API-003",
                    "WARN",
                    "structural_conformance",
                    "api-service (TypeScript) does not declare drizzle-orm or prisma.",
                    "Depend on an ORM (drizzle-orm preferred) instead of raw SQL.",
                )
            )
    return findings


def check_v1_route_prefix(repo_path: Path, language: str = "python") -> list[Finding]:
    """API-004: /v1/ prefix required on public routes."""
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    exempt_paths = (
        "/health",
        "/docs",
        "/openapi.json",
        "/metrics",
        "/redoc",
        "/version",
    )

    if language == "python":
        import ast

        route_attrs = {"get", "post", "put", "delete", "patch", "head", "options"}

        def _const_str(node: ast.AST | None) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            return None

        def _router_var_name(node: ast.AST | None) -> str | None:
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                return node.attr
            return None

        def _norm_path(segment: str) -> str:
            seg = (segment or "").strip()
            if not seg:
                return ""
            if not seg.startswith("/"):
                seg = "/" + seg
            if seg != "/":
                seg = seg.rstrip("/")
            return seg

        def _join_paths(left: str, right: str) -> str:
            left_norm = _norm_path(left)
            right_norm = _norm_path(right)
            if not left_norm:
                return right_norm or "/"
            if not right_norm:
                return left_norm
            if left_norm == "/":
                return right_norm
            if right_norm == "/":
                return left_norm
            return f"{left_norm}/{right_norm.lstrip('/')}"

        py_files = list(src.rglob("*.py"))
        local_prefixes: dict[str, str] = {}
        include_prefixes: dict[str, str] = {}

        # Pass 1: collect APIRouter local prefixes and include_router mount prefixes.
        for py_file in py_files:
            try:
                text = py_file.read_text()
                tree = ast.parse(text)
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    call = node.value
                    if not (
                        isinstance(call.func, ast.Name) and call.func.id == "APIRouter"
                    ) and not (
                        isinstance(call.func, ast.Attribute)
                        and call.func.attr == "APIRouter"
                    ):
                        continue
                    prefix_val: str | None = None
                    for kw in call.keywords:
                        if kw.arg == "prefix":
                            prefix_val = _const_str(kw.value)
                            break
                    if not prefix_val:
                        continue
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            local_prefixes[target.id] = prefix_val

                if isinstance(node, ast.Call):
                    if not (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "include_router"
                    ):
                        continue
                    if not node.args:
                        continue
                    router_name = _router_var_name(node.args[0])
                    if not router_name:
                        continue
                    include_prefix: str | None = None
                    for kw in node.keywords:
                        if kw.arg == "prefix":
                            include_prefix = _const_str(kw.value)
                            break
                    if not include_prefix:
                        continue
                    existing = include_prefixes.get(router_name)
                    # Prefer a v1-bearing mount if multiple include_router calls exist.
                    if existing is None or (
                        "/v1" in include_prefix and "/v1" not in existing
                    ):
                        include_prefixes[router_name] = include_prefix

        # Pass 2: evaluate effective route path from include + local + decorator path.
        for py_file in py_files:
            try:
                text = py_file.read_text()
                tree = ast.parse(text)
            except Exception:
                continue
            rel = py_file.relative_to(repo_path)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    if not isinstance(dec.func, ast.Attribute):
                        continue
                    if dec.func.attr not in route_attrs:
                        continue
                    if not dec.args:
                        continue
                    path_arg = dec.args[0]
                    route = _const_str(path_arg)
                    if route is None:
                        continue
                    router_var = _router_var_name(dec.func.value)
                    include_prefix = include_prefixes.get(router_var or "", "")
                    local_prefix = local_prefixes.get(router_var or "", "")
                    effective_route = _join_paths(
                        _join_paths(include_prefix, local_prefix), route
                    )

                    if any(effective_route.startswith(p) for p in exempt_paths):
                        continue
                    if not effective_route.startswith("/v1/"):
                        findings.append(
                            _finding(
                                "API-004",
                                "ERROR",
                                "structural_conformance",
                                f"{rel}::{node.name}: effective route {effective_route!r} missing /v1/ prefix.",
                                "Mount routes under /v1/ to support versioning.",
                            )
                        )
    else:
        route_re = re.compile(
            r"""(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*['"]([^'"]+)['"]"""
        )
        for ts_file in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
            try:
                text = ts_file.read_text()
            except Exception:
                continue
            rel = ts_file.relative_to(repo_path)
            for m in route_re.finditer(text):
                route = m.group(1)
                if any(route.startswith(p) for p in exempt_paths):
                    continue
                if not route.startswith("/v1/"):
                    findings.append(
                        _finding(
                            "API-004",
                            "ERROR",
                            "structural_conformance",
                            f"{rel}: route {route!r} missing /v1/ prefix.",
                            "Mount routes under /v1/ to support versioning.",
                        )
                    )
    return findings


def check_response_envelope_presence(repo_path: Path) -> list[Finding]:
    """API-005: Response envelope — endpoints declare response_model.

    Partial overlap with XSTACK-002, but this one specifically looks at
    shape consistency. Our deterministic pass just asserts response_model=
    exists on each endpoint (delegating shape inspection to the LLM).
    """
    CHECK_ID = "API-005"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    route_attrs = {"get", "post", "put", "delete", "patch"}
    flagged: set[tuple[str, str]] = set()
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in route_attrs:
                    continue
                has_rm = any(kw.arg == "response_model" for kw in dec.keywords)
                if not has_rm:
                    key = (str(rel), node.name)
                    if key in flagged:
                        continue
                    flagged.add(key)
                    findings.append(
                        _finding(
                            "API-005",
                            "ERROR",
                            "structural_conformance",
                            f"{rel}::{node.name}: endpoint missing response_model=.",
                            "Declare a response_model Pydantic class so the envelope "
                            "shape is explicit.",
                        )
                    )
    return findings


def check_owner_id_column(repo_path: Path) -> list[Finding]:
    """API-006: Every table is authorization-scoped to a Clerk user.

    Three patterns satisfy this:

    1. Direct ownership — the table has an ``owner_id`` column holding the
       Clerk user ID of the row's owner. Majority case.

    2. Identity table — the table IS the user; its primary key IS the user
       identifier. Detected by: primary-key column named ``user_id`` (or
       class ends in ``Profile``/``Identity``/``User``).

    3. Relationship table — the row represents a relationship to a user,
       using a ``user_id`` column that carries a ``ForeignKey`` to an
       identity table. Detected by: ``user_id`` column with a ``ForeignKey``
       argument in its ``mapped_column(...)`` / ``Column(...)`` call.

    Existing exemptions by class-name suffix (``_lookup``, ``_config``,
    ``_enum``) continue to apply. The SQLAlchemy declarative root
    (``Base``, ``DeclarativeBase``, ``Model``) is skipped.
    """
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    # Variable names that hint a model is internal/lookup and exempt.
    exempt_suffixes = ("_lookup", "_config", "_enum", "Lookup", "Config", "Enum")

    # Class names suggesting the model is the user identity itself.
    identity_class_suffixes = ("Profile", "Identity", "User")

    # Class names that are the SQLAlchemy declarative root itself, not a
    # table. These inherit from DeclarativeBase or declarative_base() and
    # exist to serve as the base for every real model — they have no
    # columns of their own.
    abstract_root_names = {"Base", "DeclarativeBase", "Model"}

    def _column_call_kwargs(value: ast.AST) -> list[ast.keyword]:
        """For a `mapped_column(...)` / `Column(...)` call, return its kwargs."""
        if isinstance(value, ast.Call):
            func = value.func
            func_name = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name in ("mapped_column", "Column"):
                return list(value.keywords)
        return []

    def _column_call_positional_args(value: ast.AST) -> list[ast.AST]:
        """For a column call, return its positional args (where FK can live)."""
        if isinstance(value, ast.Call):
            func = value.func
            func_name = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name in ("mapped_column", "Column"):
                return list(value.args)
        return []

    def _has_foreign_key(value: ast.AST) -> bool:
        """True if a column call contains a ForeignKey(...) argument."""
        # ForeignKey can appear as a positional arg or as kwarg `foreign_keys=...`
        for arg in _column_call_positional_args(value):
            if isinstance(arg, ast.Call):
                fn = arg.func
                name = None
                if isinstance(fn, ast.Name):
                    name = fn.id
                elif isinstance(fn, ast.Attribute):
                    name = fn.attr
                if name == "ForeignKey":
                    return True
        return False

    def _is_primary_key(value: ast.AST) -> bool:
        """True if a column call has primary_key=True."""
        for kw in _column_call_kwargs(value):
            if (
                kw.arg == "primary_key"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ):
                return True
        return False

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            # Skip the SQLAlchemy declarative root itself.
            if node.name in abstract_root_names:
                continue
            # Heuristic: class is a SQLAlchemy model if it inherits from a
            # class ending in Base or DeclarativeBase.
            base_names = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    base_names.append(b.id)
                elif isinstance(b, ast.Attribute):
                    base_names.append(b.attr)
            if not any(bn.endswith("Base") or "Declarative" in bn for bn in base_names):
                continue
            if any(node.name.endswith(s) for s in exempt_suffixes):
                continue

            has_owner_id = False
            user_id_is_pk = False
            user_id_has_fk = False

            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign):
                    targets = [stmt.target]
                    value = stmt.value
                elif isinstance(stmt, ast.Assign):
                    targets = stmt.targets
                    value = stmt.value
                else:
                    continue
                for t in targets:
                    if not isinstance(t, ast.Name):
                        continue
                    if t.id == "owner_id":
                        has_owner_id = True
                    elif t.id == "user_id" and value is not None:
                        if _is_primary_key(value):
                            user_id_is_pk = True
                        if _has_foreign_key(value):
                            user_id_has_fk = True

            if has_owner_id:
                continue
            # Pattern 2: identity table — user_id is PK, OR class-name suffix
            # indicates identity.
            if user_id_is_pk or any(
                node.name.endswith(s) for s in identity_class_suffixes
            ):
                continue
            # Pattern 3: relationship table — user_id carries a ForeignKey.
            if user_id_has_fk:
                continue

            findings.append(
                _finding(
                    "API-006",
                    "WARN",
                    "structural_conformance",
                    f"{rel}::{node.name}: SQLAlchemy model is not "
                    "authorization-scoped — no owner_id column, not an identity "
                    "table (user_id primary key), and no user_id ForeignKey to "
                    "an identity table.",
                    "Add owner_id for ordinary tables; make user_id the primary "
                    "key for identity tables; or add a ForeignKey on user_id for "
                    "tables representing a relationship to a user. Suffix with "
                    "_lookup/_config/_enum for internal tables, or document in "
                    "evaluator.yaml.",
                )
            )
    return findings


def check_cors_config(repo_path: Path, language: str = "python") -> list[Finding]:
    """API-009: CORS middleware configured; no hardcoded origins."""
    CHECK_ID = "API-009"
    findings: list[Finding] = []
    src = repo_path / "src"

    if language == "python":
        has_cors = False
        has_cors_origins_env = False
        if src.is_dir():
            for py_file in src.rglob("*.py"):
                try:
                    text = py_file.read_text()
                except Exception:
                    continue
                if "CORSMiddleware" in text:
                    has_cors = True
                if (
                    "CORS_ORIGINS" in text
                    or 'getenv("CORS_ORIGINS"' in text
                    or "getenv('CORS_ORIGINS'" in text
                ):
                    has_cors_origins_env = True
        if not has_cors:
            findings.append(
                _finding(
                    "API-009",
                    "ERROR",
                    "structural_conformance",
                    "api-service (Python) has no CORSMiddleware configuration.",
                    "Register CORSMiddleware from fastapi.middleware.cors with origins "
                    "sourced from CORS_ORIGINS env var.",
                )
            )
        elif not has_cors_origins_env:
            findings.append(
                _finding(
                    "API-009",
                    "WARN",
                    "structural_conformance",
                    "api-service (Python) uses CORSMiddleware but CORS_ORIGINS env var is not referenced.",
                    "Source allowed origins from CORS_ORIGINS rather than hardcoded values.",
                )
            )
    else:
        has_cors_import = False
        if src.is_dir():
            for ts_file in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
                try:
                    text = ts_file.read_text()
                except Exception:
                    continue
                if (
                    "cors(" in text
                    or "from 'hono/cors'" in text
                    or 'from "hono/cors"' in text
                ):
                    has_cors_import = True
                    break
        if not has_cors_import:
            findings.append(
                _finding(
                    "API-009",
                    "ERROR",
                    "structural_conformance",
                    "api-service (TypeScript) has no cors() middleware import.",
                    "Import and register cors() from hono/cors with origins from process.env.CORS_ORIGINS.",
                )
            )
    return findings


def check_health_endpoint(repo_path: Path, language: str = "python") -> list[Finding]:
    """API-010: GET /health endpoint present."""
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    exts = ("*.py",) if language == "python" else ("*.ts", "*.tsx")
    has_health = False
    for ext in exts:
        for f in src.rglob(ext):
            try:
                text = f.read_text()
            except Exception:
                continue
            if "/health" in text and (
                "def health" in text or '"/health"' in text or "'/health'" in text
            ):
                has_health = True
                break
        if has_health:
            break
    if not has_health:
        findings.append(
            _finding(
                "API-010",
                "WARN",
                "cd_readiness",
                "api-service has no visible GET /health endpoint.",
                "Add a GET /health route that returns {'status': 'ok'} with no auth "
                "and no DB queries.",
            )
        )
    return findings


def check_fetch_error_handling(repo_path: Path) -> list[Finding]:
    """FE-006: Astro fetch calls wrapped in try/catch with fallback."""
    CHECK_ID = "FE-006"
    import re

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for astro_file in src.rglob("*.astro"):
        try:
            text = astro_file.read_text()
        except Exception:
            continue
        # Extract frontmatter
        m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not m:
            continue
        fm = m.group(1)
        if "fetch(" not in fm:
            continue
        # Crude heuristic: the frontmatter should contain `try` and `catch`
        # somewhere around the fetch call.
        if "try" not in fm or "catch" not in fm:
            rel = astro_file.relative_to(repo_path)
            findings.append(
                _finding(
                    "FE-006",
                    "ERROR",
                    "structural_conformance",
                    f"{rel}: frontmatter fetch() without try/catch error handling.",
                    "Wrap fetch calls in try/catch with a fallback value so build "
                    "succeeds when the API is unavailable.",
                )
            )
    return findings


def check_pydantic_for_external_data(repo_path: Path) -> list[Finding]:
    """PY-004: External data goes through Pydantic.

    Heuristic: flag files that access response.json() or csv.DictReader
    results directly without defining a BaseModel subclass.
    """
    CHECK_ID = "PY-004"
    import re

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    suspect_patterns = (
        r"\.json\(\)\[",  # response.json()["..."]
        r"csv\.DictReader",
        r"csv\.reader",
    )
    suspect_re = re.compile("|".join(suspect_patterns))
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        if suspect_re.search(text) and "BaseModel" not in text:
            findings.append(
                _finding(
                    "PY-004",
                    "WARN",
                    "structural_conformance",
                    f"{rel}: accesses external data (JSON/CSV) without a Pydantic BaseModel.",
                    "Define a Pydantic model and validate external payloads through it.",
                )
            )
    return findings


def check_async_sqlalchemy(repo_path: Path) -> list[Finding]:
    """PY-015: SQLAlchemy uses async API."""
    CHECK_ID = "PY-015"
    findings: list[Finding] = []
    pyproject = repo_path / "pyproject.toml"
    pyp_text = pyproject.read_text() if pyproject.exists() else ""

    src = repo_path / "src"
    if not src.is_dir():
        return findings

    src_text = ""
    for py_file in src.rglob("*.py"):
        try:
            src_text += "\n" + py_file.read_text()
        except Exception:
            continue

    if "sqlalchemy" not in src_text.lower() and "sqlalchemy" not in pyp_text.lower():
        return findings  # Not a SQLAlchemy repo

    # Flag sync imports
    if (
        (
            "from sqlalchemy.orm import Session" in src_text
            or "from sqlalchemy.orm import sessionmaker" in src_text
        )
        and "AsyncSession" not in src_text
        and "async_sessionmaker" not in src_text
    ):
        findings.append(
            _finding(
                "PY-015",
                "ERROR",
                "structural_conformance",
                "Sync Session/sessionmaker imported without AsyncSession/async_sessionmaker counterpart.",
                "Use AsyncSession and async_sessionmaker from sqlalchemy.ext.asyncio.",
            )
        )
    # Flag sync create_engine
    if "create_engine(" in src_text and "create_async_engine(" not in src_text:
        findings.append(
            _finding(
                "PY-015",
                "ERROR",
                "structural_conformance",
                "Sync create_engine() used without create_async_engine() counterpart.",
                "Use create_async_engine from sqlalchemy.ext.asyncio.",
            )
        )
    # asyncpg required when sqlalchemy is present
    if "sqlalchemy" in pyp_text.lower() and "asyncpg" not in pyp_text.lower():
        findings.append(
            _finding(
                "PY-015",
                "ERROR",
                "structural_conformance",
                "sqlalchemy declared without asyncpg in pyproject.toml.",
                "Add asyncpg to dependencies for async PostgreSQL.",
            )
        )
    return findings
