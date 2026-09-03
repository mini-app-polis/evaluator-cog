"""Identity-contract rule checks (AUTH-003, AUTH-004, CD-019).

These three rules encode the credential architecture that shipped in
September 2026, and each one exists because a specific class of mistake
survived review before it:

  - **AUTH-003** every non-public route names the scope it requires.
  - **AUTH-004** the authorization decision comes from the shared
    ``identity`` contract and every decision is audited.
  - **CD-019** the bearer-credential contract itself: Clerk session
    JWTs for humans, named per-machine API keys for machines, and
    nothing else.

The architecture the checks encode
----------------------------------
Machines hold **named API keys**, not JWTs. A machine's key lives in
Doppler, reaches the API as an environment variable derived from the
machine name (``deejay-cog`` -> ``DEEJAY_COG_API_KEY``) and is compared
in process in constant time inside the shared library. Clerk M2M was
removed outright — there is no token to mint, no ``m2m_tokens/verify``
endpoint to call, and no machine secret to exchange. Humans get a Clerk
session JWT which the API verifies offline against cached JWKS.
Credentials are routed structurally by dot count: two dots is a Clerk
session JWT, zero dots is a named machine key. A scope is exactly three
dot-separated segments, ``<domain>.<resource>.<action>``. The binding
contract is four functions run once per request: verify, resolve,
authorize, emit_audit.

Every remediation string in this module is written against that
architecture. A remediation that told a caller to acquire a Clerk M2M
token would be telling it to use a path that no longer exists; the
retired CD-012 check was deleted for exactly that inversion, and none
of the suggestions below may reintroduce it.

Why the checks are shaped the way they are
------------------------------------------
**Routes are enumerated by registration, never by path string.** All
route enumeration is delegated to ``_routes.enumerate_routes``, whose
module docstring explains the constraint in full: four routes in
api-kaianolevine-com's ``wcs_wiki.py`` are registered by a module-level
factory and have no literal path anywhere in the file, which is how
they went unguarded originally. There is deliberately not one path
regex in this module.

**Unresolvable is a finding, not a pass.** A registration whose guard
cannot be read statically — a bare ``Depends(current_user)``, a
``dependencies=`` built from a variable, a handler defined in another
module, a route registered inside a factory onto a router handed in as
a parameter — is reported by AUTH-003 clause (1). An unknown route is
the case the rule exists to catch, and recording "I could not tell" as
"it was fine" would reproduce the original failure with better tooling.

**Structure is read with the AST, not with substring containment.**
The retired CD-012 check scanned source text for its own detection
literals and flagged itself. Two guards from ``_shared`` keep that from
recurring: ``_is_checker_self_source`` skips the deterministic package's
own modules, and ``_is_inside_string_literal`` exempts a file whose only
occurrences of a retired token sit inside quoted strings (a fixture
holding source snippets). Where CD-019 must look at a string — a header
name such as ``X-Owner-Id`` is only ever a string — the check asks where
in the tree the string sits: a dict key, a call argument or a comparison
operand is real usage; a bare element of a tuple of patterns is not.
Docstrings are treated as real occurrences on purpose, because CD-019
step (2) says in so many words that stale docstrings are not exempt.

Nothing here raises. Every filesystem read and every clause is wrapped;
a repo with no ``src/`` produces no findings rather than an exception.
Scope filtering by ``applies_to`` is the dispatcher's job, not ours —
the only gating done here is on evidence (a repo with no route
registrations has no enforcement point to audit), never on repo type,
with the single exception of CD-019's documented ``repo_type`` split
between caller steps and receiver steps.
"""

from __future__ import annotations

import ast
import contextlib
import re
from dataclasses import dataclass
from pathlib import Path

from evaluator_cog.engine.deterministic._routes import (
    SCOPE_SEGMENTS,
    Route,
    enumerate_routes,
    is_valid_scope,
)
from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
    _is_checker_self_source,
    _is_inside_string_literal,
)

_DIMENSION = "structural_conformance"
_SEVERITY = "ERROR"

# --- vocabulary --------------------------------------------------------------

# FastAPI parameter sources that pull a value out of the request itself.
# Path parameters are excluded: a path segment is addressing, not a
# claim of identity.
_REQUEST_PARAM_SOURCES = frozenset({"Header", "Query", "Body", "Cookie", "Form"})

# Request attributes that yield caller-controlled data.
_REQUEST_BAGS = ("headers", "query_params", "cookies", "args", "params", "json", "form")

# Header names that assert who the caller is. A header cannot be a
# filter value under any reading — the name says principal — so these
# are flagged wherever they are read, independent of the route's guard.
_IDENTITY_HEADERS = frozenset(
    {
        "xownerid",
        "xuserid",
        "xuser",
        "xcallerid",
        "xcaller",
        "xprincipal",
        "xprincipalid",
        "xmachinename",
        "xmachine",
        "xservicename",
        "xserviceaccount",
        "xonbehalfof",
        "xactorid",
        "xactor",
        "xclientid",
        "xinternalcaller",
        "xauthenticateduser",
    }
)

# Names that mean "this value is the principal". Binding a request-read
# value to one of these is the step AUTH-003 (3) forbids. Deliberately
# excludes owner_id / user_id: those are the legitimate filter spelling.
_PRINCIPAL_BINDINGS = frozenset(
    {
        "principal",
        "caller",
        "current_user",
        "currentuser",
        "actor",
        "identity",
        "requester",
        "subject",
        "auth_principal",
        "auth_user",
        "authenticated_user",
        "auth_context",
    }
)

# Calls that consume a principal to make or enforce a decision.
_AUTHORIZE_CALLS = frozenset(
    {
        "authorize",
        "require_scope",
        "require_scopes",
        "check_permission",
        "require_permission",
        "check_scope",
        "has_scope",
        "assert_can",
        "enforce",
        "enforce_scope",
    }
)

# Functions whose return value *is* the principal.
_PRINCIPAL_PROVIDER_RE = re.compile(
    r"^(get_)?current_|_principal$|_caller$|^get_principal|^get_caller|"
    r"^resolve_caller|^resolve_principal|^authenticate"
)

# AUTH-003 (4): boolean authority columns and credential-type fields.
_AUTHORITY_COLUMNS = frozenset(
    {"is_admin", "is_service", "is_staff", "is_superuser", "is_root", "is_machine"}
)
_CREDENTIAL_TYPE_FIELDS = frozenset(
    {
        "credential_type",
        "principal_type",
        "token_type",
        "caller_type",
        "auth_type",
        "credential_kind",
        "principal_kind",
    }
)
_CREDENTIAL_TYPE_VALUES = frozenset(
    {"machine", "human", "user", "service", "m2m", "session", "apikey", "api_key"}
)

# Exceptions that mean "denied". Used to tell a decision branch from a
# provisioning-time read of the same column, which AUTH-003 (4) allows.
_DENIAL_EXCEPTIONS = (
    "httpexception",
    "forbidden",
    "permissionerror",
    "permissiondenied",
    "notauthorized",
    "unauthorized",
    "authorizationerror",
    "accessdenied",
    "notpermitted",
)

# AUTH-004 (2)/(3): how an audit emit is spelled, and what it must carry.
_AUDIT_FUNCTIONS = frozenset(
    {"emit_audit", "record_audit", "write_audit", "audit_log", "log_audit", "audit"}
)
_AUDIT_METHODS = frozenset({"emit", "record", "write", "log"})
_AUDIT_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "enforcement point": frozenset(
        {"enforcement_point", "enforcement", "point", "enforcement_pt"}
    ),
    "principal": frozenset(
        {"principal", "principal_id", "subject", "caller", "caller_id"}
    ),
    "scope": frozenset({"scope", "scopes", "required_scope", "requested_scope"}),
    "outcome": frozenset({"outcome", "result", "decision", "allowed", "granted"}),
    "reason": frozenset({"reason", "reason_code", "why", "detail"}),
}

# CD-019 (2): symbols retired with Clerk M2M. Split by how they appear
# in source — an identifier is read from the tree, a wire string is read
# from its position in the tree.
_RETIRED_IDENTIFIERS = frozenset(
    {
        "get_m2m_token",
        "machine_secret",
        "require_wcs_admin",
        "require_wcs_service",
        "get_current_caller",
        "resolve_or_provision",
    }
)
_RETIRED_WIRE_STRINGS = ("m2m_tokens/verify", "X-Internal-API-Key", "X-Owner-Id")
# What each retired symbol has been replaced by, so the remediation is
# concrete rather than "remove this".
_RETIRED_REPLACEMENT: dict[str, str] = {
    "get_m2m_token": (
        "Delete the token acquisition. A machine presents its own named key: "
        'construct the client with KaianoApiClient.from_env("<machine-name>") '
        "and let it read <MACHINE_NAME>_API_KEY from the environment."
    ),
    "m2m_tokens/verify": (
        "Delete the call. The endpoint no longer exists — machine credentials "
        "are named keys compared in process by the shared identity library, "
        "never verified over the network."
    ),
    "machine_secret": (
        "Replace the shared machine secret with the caller's own named key "
        "(<MACHINE_NAME>_API_KEY in Doppler) resolved via "
        'KaianoApiClient.from_env("<machine-name>").'
    ),
    "X-Internal-API-Key": (
        "Send the named key as a bearer credential in the Authorization header "
        "instead; the private internal-key header was retired with Clerk M2M."
    ),
    "X-Owner-Id": (
        "Delete the header. The caller's identity comes from the presented "
        "credential — a Clerk session JWT or a named machine key — and is "
        "resolved by identity.chain, never read off the request."
    ),
    "require_wcs_admin": (
        'Replace with Depends(require_scope("<domain>.<resource>.<action>")) '
        "naming the scope the route actually needs; role-shaped FastAPI "
        "dependencies were retired with the scope model."
    ),
    "require_wcs_service": (
        'Replace with Depends(require_scope("<domain>.<resource>.<action>")) '
        "naming the scope the route actually needs; there is no separate "
        "service-caller dependency in the shipped contract."
    ),
    "get_current_caller": (
        "Replace with the shared identity chain (identity.chain) as the route "
        "dependency; it resolves the principal from the presented credential."
    ),
    "resolve_or_provision": (
        "Replace with identity.store lookup plus explicit provisioning; "
        "implicit provisioning on the credential path was retired."
    ),
}

# CD-019 (2): the narrow reading of CLERK_SECRET_KEY.
_CLERK_SECRET_NAMES = ("CLERK_SECRET_KEY", "clerk_secret_key")
_TOKEN_ACQUISITION_CALLS = frozenset(
    {
        "get_m2m_token",
        "create_m2m_token",
        "_create_clerk_m2m_token",
        "mint_token",
        "mint",
        "issue_token",
        "exchange_token",
        "token_exchange",
        "create_access_token",
        "create_service_token",
    }
)
_JWT_SIGNING_CALLS = frozenset({"encode", "sign", "generate_token", "create_jwt"})
_HTTP_VERBS = frozenset(
    {"get", "post", "put", "patch", "delete", "request", "send", "stream", "urlopen"}
)
_HTTP_CLIENT_HINTS = ("httpx", "requests", "aiohttp", "urllib", "session", "client")

# CD-019 (1): how an ecosystem API client is spelled.
_ECOSYSTEM_CLIENT_RE = re.compile(r"ApiClient$")
_ECOSYSTEM_URL_HINTS = (
    "kaianolevine",
    "KAIANO_API_BASE_URL",
    "api_base_url",
    "ecosystem_api",
)

# CD-019 (8): key material must not be persisted.
_KEY_TABLE_RE = re.compile(
    r"principal|machine|issuer|credential|api_?key|service_account", re.IGNORECASE
)
_KEY_COLUMN_RE = re.compile(r"key|secret|token|hash", re.IGNORECASE)
_KEY_COLUMN_EXEMPT = frozenset({"jwks_url", "jwks_uri"})


# --- source access -----------------------------------------------------------


@dataclass
class _PyFile:
    """One parsed Python module, plus the maps every clause needs.

    Parsed once and passed around: several clauses need the parent map
    and the enclosing-function map, and rebuilding them per clause would
    triple the walk count for no benefit.
    """

    path: Path
    rel: str
    source: str
    tree: ast.Module

    @property
    def is_test(self) -> bool:
        norm = self.rel.replace("\\", "/")
        return (
            "/tests/" in f"/{norm}"
            or norm.startswith("tests/")
            or self.path.name.startswith("test_")
            or self.path.name == "conftest.py"
        )


def _parse_python(
    root: Path, repo_path: Path, *, skip_tests: bool = True
) -> list[_PyFile]:
    """Parse every module under ``root``, skipping what must not be scanned.

    Two categories are dropped here rather than in each clause:

      - the deterministic package's own source, via
        ``_is_checker_self_source``. These modules contain every literal
        the checks look for, as detection patterns; scanning them would
        make evaluator-cog fail its own identity rules.
      - test modules, when ``skip_tests`` is set. CD-019 turns this off
        because its step (2) has an explicit, narrower exemption (tests
        that mock the auth layer) that has to be applied per file.
    """
    out: list[_PyFile] = []
    if not root.is_dir():
        return out
    for py in sorted(root.rglob("*.py")):
        if _is_checker_self_source(py):
            continue
        norm = str(py).replace("\\", "/")
        if skip_tests and (
            "/tests/" in norm or py.name.startswith("test_") or py.name == "conftest.py"
        ):
            continue
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        try:
            rel = str(py.relative_to(repo_path))
        except ValueError:
            rel = py.name
        out.append(_PyFile(py, rel.replace("\\", "/"), source, tree))
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# --- small AST utilities -----------------------------------------------------


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - ast.unparse is total in 3.9+
        return "<unparseable>"


def _call_name(node: ast.AST) -> str:
    """The callee's simple name: ``a.b.c(x)`` -> ``c``, ``f(x)`` -> ``f``."""
    if not isinstance(node, ast.Call):
        return ""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _owner_function(tree: ast.AST) -> dict[int, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Map every node to the innermost function that contains it.

    ``ast.walk`` is breadth-first, so outer functions are visited before
    the functions nested inside them and the later assignment wins —
    which is what makes the innermost function the recorded owner.
    """
    owners: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                owners[id(node)] = fn
    return owners


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _string_constants(node: ast.AST) -> list[str]:
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _docstring_constants(tree: ast.AST) -> set[int]:
    """ids of Constant nodes that are docstrings.

    CD-019 (2) treats a docstring mention of a retired symbol as a real
    occurrence ("stale docstrings are not exempt") but must not treat a
    pattern string in a table of detection literals the same way. The
    only way to tell them apart is position, so docstring nodes are
    identified up front.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _normalize(name: str) -> str:
    """Fold a header or parameter name to a comparable form.

    ``X-Owner-Id``, ``x_owner_id`` and ``xOwnerId`` are the same claim
    spelled three ways; all three fold to ``xownerid``.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _denies_access(stmts: list[ast.stmt]) -> bool:
    """True if this branch body refuses the request.

    AUTH-003 (4) permits reading an authority column to mirror it into a
    role at provisioning time and forbids reading it to make the
    decision. A branch that raises a denial or returns False is making
    the decision; a branch that assigns a value is not.
    """
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                exc = node.exc
                target = ""
                if isinstance(exc, ast.Call):
                    target = _call_name(exc)
                elif isinstance(exc, ast.Name):
                    target = exc.id
                elif isinstance(exc, ast.Attribute):
                    target = exc.attr
                if any(marker in target.lower() for marker in _DENIAL_EXCEPTIONS):
                    return True
            if (
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Constant)
                and node.value.value is False
            ):
                return True
    return False


def _enclosing_http_call(parents: dict[int, ast.AST], node: ast.AST) -> ast.Call | None:
    """Walk up until an HTTP send is found, or the tree runs out.

    Used for the "and then sends it to an ecosystem API" half of
    CD-019 (2): an Authorization header built in a dict that is an
    argument to ``client.post(...)`` is on the wire; the same dict
    assigned to a config constant is not.
    """
    current: ast.AST | None = node
    seen = 0
    while current is not None and seen < 12:
        parent = parents.get(id(current))
        if parent is None:
            return None
        if isinstance(parent, ast.Call):
            name = _call_name(parent)
            target = _unparse(parent.func).lower()
            if name in _HTTP_VERBS and any(h in target for h in _HTTP_CLIENT_HINTS):
                return parent
            if name in _HTTP_VERBS and isinstance(parent.func, ast.Attribute):
                return parent
        current = parent
        seen += 1
    return None


def _looks_like_http_call(node: ast.Call) -> bool:
    name = _call_name(node)
    if name not in _HTTP_VERBS:
        return False
    target = _unparse(node.func).lower()
    return any(hint in target for hint in _HTTP_CLIENT_HINTS) or name == "urlopen"


# --- AUTH-003 ----------------------------------------------------------------


def _auth_module_docstrings(files: list[_PyFile]) -> list[tuple[str, str]]:
    """(relative path, module docstring) for every plausible auth module.

    AUTH-003 (2) asks whether the bootstrap set is enumerated "in the
    service's auth module docstring". Services spell that module several
    ways, so the candidate set is by filename rather than by import.
    """
    wanted = {
        "auth",
        "authz",
        "authorization",
        "identity",
        "deps",
        "dependencies",
        "security",
    }
    out: list[tuple[str, str]] = []
    for f in files:
        if f.path.stem not in wanted:
            continue
        doc = ast.get_docstring(f.tree) or ""
        out.append((f.rel, doc))
    return out


def _auth003_clause1(check_id: str, routes: list[Route]) -> list[Finding]:
    """(1) Every registration lands in one of the three classes, or is flagged.

    Two shapes reach this clause. The first is a route the enumerator
    marked unresolvable: a bare ``Depends(current_user)``, a
    ``dependencies=`` built from a variable, an ``add_api_route`` whose
    endpoint lives in another module. The second is a route registered
    inside a factory with no visible dependency. That second one is not
    "public" — the router it registers on arrives as a parameter and may
    carry router-level dependencies the registration site cannot see —
    so concluding "public" from the absence of a decorator argument
    would be exactly the silent pass the rule forbids. Both are reported
    as unresolvable guards.
    """
    findings: list[Finding] = []
    for route in routes:
        kind = route.classify()
        if kind == "unresolvable":
            exprs = (
                ", ".join(d.expr for d in route.unresolvable_dependencies) or "<none>"
            )
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"AUTH-003 (1): {route.location} has a guard that cannot be "
                    f"resolved statically ({exprs}) — the route classifies as "
                    f"neither scope-guarded, authenticated-only nor public, so "
                    f"the evaluator cannot say what it requires.",
                    "Declare the guard at the registration site so it is readable: "
                    'Depends(require_scope("<domain>.<resource>.<action>")) with a '
                    "literal scope string, rather than a bare dependency callable "
                    "whose requirement is decided elsewhere.",
                )
            )
        elif kind == "public" and route.in_factory:
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"AUTH-003 (1): {route.location} is registered inside a factory "
                    f"and carries no visible dependency. The router it registers on "
                    f"is supplied by the caller, so its guard cannot be resolved "
                    f"from the registration site and the route cannot be concluded "
                    f"public — this is the shape that left four wcs_wiki routes "
                    f"unguarded.",
                    "Attach the scope guard at the registration itself — "
                    'Depends(require_scope("<domain>.<resource>.<action>")) in the '
                    "handler signature or dependencies=[...] on the decorator — so "
                    "the requirement is visible where the route is declared.",
                )
            )
    return findings


def _auth003_clause2(
    check_id: str, routes: list[Route], files: list[_PyFile]
) -> list[Finding]:
    """(2) Authenticated-only routes must be a documented bootstrap set.

    A route that authenticates but names no scope is legitimate only as
    a bootstrap endpoint — the call a caller makes before it knows what
    it may do. The rule's test for "documented" is that the auth
    module's docstring enumerates the set, so a route counts as
    documented when the docstring names its handler function or its
    literal path.
    """
    findings: list[Finding] = []
    authenticated = [r for r in routes if r.classify() == "authenticated-only"]
    if not authenticated:
        return findings

    docs = _auth_module_docstrings(files)
    corpus = "\n".join(doc for _rel, doc in docs)
    module_names = ", ".join(rel for rel, _doc in docs) or "<no auth module found>"

    for route in authenticated:
        mentioned = route.func_name and route.func_name in corpus
        if not mentioned and route.path:
            mentioned = route.path in corpus
        if mentioned:
            continue
        findings.append(
            _finding(
                check_id,
                _SEVERITY,
                _DIMENSION,
                f"AUTH-003 (2): {route.location} is authenticated-only — it "
                f"declares a dependency but names no scope — and is not "
                f"enumerated as a bootstrap endpoint in the auth module docstring "
                f"({module_names}).",
                "Either give the route the scope it requires with "
                'Depends(require_scope("<domain>.<resource>.<action>")), or, if it '
                "is genuinely a bootstrap endpoint, enumerate it by handler name in "
                "the auth module's docstring so the set stays small and reviewable.",
            )
        )
    return findings


def _request_identity_reads(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[int, str, str]]:
    """Every place ``fn`` derives the caller's identity from the request.

    Returns (line, what was read, why it is the principal). Two sources
    are recognised: a FastAPI parameter bound to ``Header``/``Query``/
    ``Body``/``Cookie``/``Form``, and a direct read from a request bag
    (``request.headers.get(...)``, ``request.query_params[...]``).

    The discrimination AUTH-003 (3) asks for — "reading an owner as a
    filter value is not the violation; deriving the principal from it
    is" — is made two ways, neither of which depends on the route's
    guard:

      a. the header itself names a principal (``X-Owner-Id``,
         ``X-Machine-Name``). A header that asserts who the caller is
         has no reading as a filter.
      b. the value flows into a principal position: it is bound to a
         name like ``principal`` or ``current_user``, passed to an
         authorization call, or returned from a function whose name
         says it provides the principal.

    A ``owner_id: str = Query(None)`` used to filter rows matches
    neither and is not reported.
    """
    out: list[tuple[int, str, str]] = []
    parents = _parents(fn)

    provider = bool(_PRINCIPAL_PROVIDER_RE.search(fn.name))
    authorize_args: set[str] = set()
    returned: set[str] = set()
    principal_assigned: dict[str, list[ast.AST]] = {}

    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _call_name(node) in _AUTHORIZE_CALLS:
            authorize_args |= _names_in(node)
        if isinstance(node, ast.Return) and node.value is not None:
            returned |= _names_in(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    principal_assigned.setdefault(target.id, []).append(node.value)

    def _flows_to_principal(bound: str, expr_names: set[str]) -> str:
        if bound and _normalize(bound) in {_normalize(p) for p in _PRINCIPAL_BINDINGS}:
            return f"bound to {bound!r}, a principal name"
        if bound and bound in authorize_args:
            return f"{bound!r} is passed to the authorization decision"
        if bound and provider and bound in returned:
            return f"{bound!r} is returned by {fn.name}(), a principal provider"
        if provider and expr_names & returned:
            return f"the value is returned by {fn.name}(), a principal provider"
        return ""

    # a. FastAPI parameter sources.
    args = list(fn.args.args) + list(fn.args.kwonlyargs) + list(fn.args.posonlyargs)
    defaults: dict[str, ast.AST] = {}
    positional = list(fn.args.posonlyargs) + list(fn.args.args)
    bound_positional = positional[len(positional) - len(fn.args.defaults) :]
    for arg, default in zip(bound_positional, fn.args.defaults, strict=False):
        defaults[arg.arg] = default
    for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=False):
        if default is not None:
            defaults[arg.arg] = default

    for arg in args:
        candidates: list[ast.AST] = []
        if arg.arg in defaults:
            candidates.append(defaults[arg.arg])
        if arg.annotation is not None:
            candidates.append(arg.annotation)
        for candidate in candidates:
            for sub in ast.walk(candidate):
                if not isinstance(sub, ast.Call):
                    continue
                source = _call_name(sub)
                if source not in _REQUEST_PARAM_SOURCES:
                    continue
                literals = _string_constants(sub)
                wire_name = literals[0] if literals else arg.arg
                why = ""
                if source == "Header" and _normalize(wire_name) in _IDENTITY_HEADERS:
                    why = f"header {wire_name!r} names the caller"
                if not why:
                    why = _flows_to_principal(arg.arg, {arg.arg})
                if why:
                    out.append(
                        (
                            getattr(sub, "lineno", fn.lineno),
                            f"{source}({wire_name!r}) -> parameter {arg.arg!r}",
                            why,
                        )
                    )

    # b. Direct reads from a request bag.
    for node in ast.walk(fn):
        key = ""
        bag_expr = ""
        if isinstance(node, ast.Call) and _call_name(node) == "get":
            target = _unparse(node.func).lower()
            if not any(f".{bag}" in target for bag in _REQUEST_BAGS):
                continue
            literals = _string_constants(node)
            if not literals:
                continue
            key = literals[0]
            bag_expr = _unparse(node)
        elif isinstance(node, ast.Subscript):
            target = _unparse(node.value).lower()
            if not any(f".{bag}" in target for bag in _REQUEST_BAGS):
                continue
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                key = index.value
                bag_expr = _unparse(node)
        if not key:
            continue

        bound = ""
        parent = parents.get(id(node))
        if isinstance(parent, ast.Assign) and parent.targets:
            first = parent.targets[0]
            if isinstance(first, ast.Name):
                bound = first.id
        why = ""
        if _normalize(key) in _IDENTITY_HEADERS:
            why = f"{key!r} names the caller"
        if not why:
            why = _flows_to_principal(bound, _names_in(node))
        if why:
            out.append((getattr(node, "lineno", fn.lineno), bag_expr, why))

    return out


def _route_handler_keys(routes: list[Route]) -> set[tuple[str, str]]:
    """(module, function name) for every route handler.

    Handlers are matched by name and module rather than by AST node
    identity on purpose: the enumerator parses each module itself, so
    ``Route.node`` belongs to a different tree than the one the clauses
    walk and an identity comparison across the two is always false. It
    would fail silently — every handler would look like a non-handler
    and clause (3) would report nothing — which is exactly the shape of
    quiet pass this rule exists to prevent. Name matching also covers
    ``add_api_route`` registrations, whose ``Route.node`` is the
    referenced endpoint and may be None when it lives elsewhere.
    """
    return {(r.file.replace("\\", "/"), r.func_name) for r in routes if r.func_name}


def _auth003_clause3(
    check_id: str, routes: list[Route], files: list[_PyFile]
) -> list[Finding]:
    """(3) The principal never comes from the request body, query or headers."""
    findings: list[Finding] = []
    handler_keys = _route_handler_keys(routes)
    dependency_callees = {d.callee for r in routes for d in r.dependencies if d.callee}

    for f in files:
        for fn in _functions(f.tree):
            is_handler = (f.rel, fn.name) in handler_keys
            is_dependency = fn.name in dependency_callees
            if not (is_handler or is_dependency):
                continue
            role = "route handler" if is_handler else "route dependency"
            for lineno, what, why in _request_identity_reads(fn):
                findings.append(
                    _finding(
                        check_id,
                        _SEVERITY,
                        _DIMENSION,
                        f"AUTH-003 (3): {f.rel}:{lineno} {fn.name}() ({role}) derives "
                        f"the caller's identity from the request — {what} — because "
                        f"{why}. The credential names the caller; the request body "
                        f"does not.",
                        "Resolve the principal from the presented credential through "
                        "the shared identity chain (a Clerk session JWT for a human, "
                        "a named machine API key for a machine) and use the "
                        "request-supplied value only as a filter, never as identity.",
                    )
                )
    return findings


def _auth003_clause4(
    check_id: str, routes: list[Route], files: list[_PyFile]
) -> list[Finding]:
    """(4) No dependency decides access from an authority column or credential type.

    Only branches that actually refuse the request are reported.
    ``role = "admin" if row.is_admin else "member"`` mirrors the column
    into a role at provisioning time, which the rule permits;
    ``if not row.is_admin: raise HTTPException(403)`` decides access
    from it, which it does not.
    """
    findings: list[Finding] = []
    dependency_callees = {d.callee for r in routes for d in r.dependencies if d.callee}
    if not dependency_callees:
        return findings

    for f in files:
        for fn in _functions(f.tree):
            if fn.name not in dependency_callees:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.If):
                    continue
                if not (_denies_access(node.body) or _denies_access(node.orelse)):
                    continue
                reason = ""
                for sub in ast.walk(node.test):
                    name = ""
                    if isinstance(sub, ast.Attribute):
                        name = sub.attr
                    elif isinstance(sub, ast.Name):
                        name = sub.id
                    if name in _AUTHORITY_COLUMNS:
                        reason = f"boolean authority column {name!r}"
                        break
                    if name in _CREDENTIAL_TYPE_FIELDS:
                        reason = f"the credential's type field {name!r}"
                        break
                if not reason:
                    for sub in ast.walk(node.test):
                        if isinstance(sub, ast.Compare):
                            literals = {s.lower() for s in _string_constants(sub)}
                            overlap = literals & _CREDENTIAL_TYPE_VALUES
                            fields = {
                                n.attr
                                if isinstance(n, ast.Attribute)
                                else getattr(n, "id", "")
                                for n in ast.walk(sub)
                            }
                            if overlap and fields & _CREDENTIAL_TYPE_FIELDS:
                                reason = (
                                    f"the credential's type compared against "
                                    f"{sorted(overlap)}"
                                )
                                break
                if not reason:
                    continue
                findings.append(
                    _finding(
                        check_id,
                        _SEVERITY,
                        _DIMENSION,
                        f"AUTH-003 (4): {f.rel}:{node.lineno} {fn.name}() is used as a "
                        f"route dependency and decides access from {reason} — the "
                        f"branch refuses the request rather than mirroring the value "
                        f"into a role.",
                        "Decide access from the scope the route names: call "
                        "identity.policy.authorize with the required "
                        "<domain>.<resource>.<action> scope and let the grant store "
                        "answer. Read the column only to seed a role at provisioning "
                        "time.",
                    )
                )
    return findings


def _auth003_clause5(check_id: str, routes: list[Route]) -> list[Finding]:
    """(5) A scope is exactly ``<domain>.<resource>.<action>``."""
    findings: list[Finding] = []
    for route in routes:
        for dep in route.scope_dependencies:
            for scope in dep.scopes:
                if is_valid_scope(scope):
                    continue
                segments = scope.count(".") + 1
                findings.append(
                    _finding(
                        check_id,
                        _SEVERITY,
                        _DIMENSION,
                        f"AUTH-003 (5): {route.location} requires scope {scope!r}, "
                        f"which has {segments} dot-separated segments; the grammar is "
                        f"<domain>.<resource>.<action>, exactly {SCOPE_SEGMENTS}.",
                        f"Rewrite {scope!r} as a three-segment scope "
                        f"(<domain>.<resource>.<action>) and register the corrected "
                        f"scope in the grant store so existing principals keep access.",
                    )
                )
    return findings


def check_auth_003(repo_path: Path) -> list[Finding]:
    """AUTH-003: every non-public route names the scope it requires.

    Routes are enumerated by ``_routes.enumerate_routes`` — by their
    registration call, never by matching path strings. The four
    factory-registered wcs_wiki routes have no literal path anywhere in
    their module; a path scan cannot see them, and this rule exists
    because they went unguarded that way once already.

    Each of the catalog's five numbered sub-checks emits its own
    findings, tagged ``AUTH-003 (n)`` in the finding text, so a repo
    learns which clause is open rather than only that the rule failed.

    The check returns nothing when the repo registers no routes: there
    is no enforcement point to reason about, and every clause below is a
    statement about routes.
    """
    CHECK_ID = "AUTH-003"
    findings: list[Finding] = []
    try:
        routes = enumerate_routes(repo_path)
    except Exception:
        return findings
    if not routes:
        return findings

    files = _parse_python(repo_path / "src", repo_path)
    for clause in (
        lambda: _auth003_clause1(CHECK_ID, routes),
        lambda: _auth003_clause2(CHECK_ID, routes, files),
        lambda: _auth003_clause3(CHECK_ID, routes, files),
        lambda: _auth003_clause4(CHECK_ID, routes, files),
        lambda: _auth003_clause5(CHECK_ID, routes),
    ):
        try:
            findings.extend(clause())
        except Exception:
            continue
    return findings


# --- AUTH-004 ----------------------------------------------------------------


def _imported_modules(files: list[_PyFile]) -> tuple[set[str], set[str]]:
    """(dotted module paths imported, names imported from them)."""
    modules: set[str] = set()
    names: set[str] = set()
    for f in files:
        for node in ast.walk(f.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                modules.add(module)
                for alias in node.names:
                    names.add(alias.name)
                    if module:
                        modules.add(f"{module}.{alias.name}")
    return modules, names


def _audit_calls(fn: ast.AST) -> list[ast.Call]:
    """Every audit emission inside ``fn``.

    Matched by function name (``emit_audit``) or by a method on an
    audit-shaped object (``audit.emit``, ``self.audit_sink.write``);
    a bare ``emit`` on an unrelated object is not an audit record.
    """
    out: list[ast.Call] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in _AUDIT_FUNCTIONS or (
            name in _AUDIT_METHODS and "audit" in _unparse(node.func).lower()
        ):
            out.append(node)
    return out


def _guard_functions(
    files: list[_PyFile],
) -> list[tuple[_PyFile, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Functions that make the authorization decision for a request.

    A guard is a function that calls ``authorize`` — the shared decision
    function — or one named as a scope guard. That is the enforcement
    point AUTH-004 (2) and (3) are about; audit obligations attach
    there, not to every function in the service.
    """
    out: list[tuple[_PyFile, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    guard_names = {
        "require_scope",
        "require_scopes",
        "scope_guard",
        "enforce_scope",
        "guard",
    }
    for f in files:
        for fn in _functions(f.tree):
            calls_authorize = any(
                isinstance(n, ast.Call) and _call_name(n) == "authorize"
                for n in ast.walk(fn)
            )
            if calls_authorize or fn.name in guard_names:
                out.append((f, fn))
    return out


def _auth004_clause1(check_id: str, files: list[_PyFile]) -> list[Finding]:
    """(1) The decision comes from the shared contract, not from a local copy."""
    findings: list[Finding] = []
    modules, names = _imported_modules(files)

    has_policy = any(m.startswith("identity.policy") for m in modules) or (
        "identity" in modules and "policy" in names
    )
    has_store = any(m.startswith("identity.store") for m in modules) or (
        "identity" in modules and "store" in names
    )

    if not has_policy:
        findings.append(
            _finding(
                check_id,
                _SEVERITY,
                _DIMENSION,
                "AUTH-004 (1): the service does not import the shared decision "
                "function — no import of identity.policy.authorize was found "
                "anywhere under src/.",
                "Import authorize from identity.policy and make every route's "
                "allow/deny decision through it, so the decision has one "
                "implementation across the ecosystem.",
            )
        )
    if not has_store:
        findings.append(
            _finding(
                check_id,
                _SEVERITY,
                _DIMENSION,
                "AUTH-004 (1): the service does not import the shared store and "
                "audit adapters — no import of identity.store was found anywhere "
                "under src/.",
                "Import the store and audit adapters from identity.store; grants "
                "and audit rows must go through the shared adapters rather than "
                "per-service tables.",
            )
        )

    # A service that imports the types but reimplements the decision.
    for f in files:
        for fn in _functions(f.tree):
            walks_grants = False
            for node in ast.walk(fn):
                if isinstance(node, ast.For) and any(
                    re.search(r"role|scope|grant|permission", n, re.IGNORECASE)
                    for n in _names_in(node.iter) | {_unparse(node.iter)}
                ):
                    walks_grants = True
                if isinstance(node, ast.Compare) and any(
                    isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
                ):
                    haystack = " ".join(_unparse(c) for c in node.comparators)
                    if re.search(
                        r"role|scope|grant|permission", haystack, re.IGNORECASE
                    ):
                        walks_grants = True
            if not walks_grants:
                continue
            returns_verdict = any(
                isinstance(n, ast.Return)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, bool)
                for n in ast.walk(fn)
            ) or _denies_access(fn.body)
            if not returns_verdict:
                continue
            if _call_name_used(fn, "authorize"):
                continue
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"AUTH-004 (1): {f.rel}:{fn.lineno} {fn.name}() reimplements the "
                    f"authorization decision locally — it walks roles/scopes/grants "
                    f"and returns an allow/deny verdict without calling the shared "
                    f"identity.policy.authorize.",
                    "Delete the local decision and call identity.policy.authorize "
                    "with the principal and the required scope; a second "
                    "implementation of the decision drifts from the first and is "
                    "the reason the contract is shared.",
                )
            )
    return findings


def _call_name_used(fn: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Call) and _call_name(n) == name for n in ast.walk(fn))


def _auth004_clause2(
    check_id: str, guards: list[tuple[_PyFile, ast.FunctionDef | ast.AsyncFunctionDef]]
) -> list[Finding]:
    """(2) The audit record is emitted on both branches.

    An emit that sits outside every ``if`` in the guard runs whatever
    the decision was, and satisfies the clause. Otherwise every ``if``
    that emits must emit in both its body and its ``else`` — an emit in
    only one branch is exactly the "audited only when allowed" (or only
    when denied) shape the clause names.
    """
    findings: list[Finding] = []
    for f, fn in guards:
        emits = _audit_calls(fn)
        if not emits:
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"AUTH-004 (2): {f.rel}:{fn.lineno} {fn.name}() makes the "
                    f"authorization decision but emits no audit record on either "
                    f"branch.",
                    "Call emit_audit on both the allow and the deny path, carrying "
                    "enforcement point, principal, scope, outcome and reason — a "
                    "decision that leaves no row cannot be reviewed later.",
                )
            )
            continue

        emit_ids = {id(e) for e in emits}
        conditional_ids: set[int] = set()
        offenders: list[ast.If] = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            body_ids = {id(n) for stmt in node.body for n in ast.walk(stmt)} & emit_ids
            else_ids = {
                id(n) for stmt in node.orelse for n in ast.walk(stmt)
            } & emit_ids
            conditional_ids |= body_ids | else_ids
            if bool(body_ids) != bool(else_ids):
                offenders.append(node)
        if emit_ids - conditional_ids:
            # At least one emit runs unconditionally: both branches covered.
            continue
        for node in offenders:
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"AUTH-004 (2): {f.rel}:{node.lineno} {fn.name}() emits the audit "
                    f"record on only one branch of the decision — the other branch "
                    f"of this if leaves no row.",
                    "Emit the audit record once, unconditionally, after the decision "
                    "is known, with outcome set from the decision — or emit "
                    "explicitly in both the allow and the deny branch.",
                )
            )
    return findings


def _auth004_clause3(
    check_id: str, guards: list[tuple[_PyFile, ast.FunctionDef | ast.AsyncFunctionDef]]
) -> list[Finding]:
    """(3) The audit record carries all five fields.

    Fields are read from the emit call's keyword arguments and from the
    keys of any dict literal passed to it, which covers both
    ``emit_audit(principal=..., scope=...)`` and
    ``emit_audit({"principal": ...})``.
    """
    findings: list[Finding] = []
    for f, fn in guards:
        for call in _audit_calls(fn):
            provided = {kw.arg for kw in call.keywords if kw.arg}
            for node in ast.walk(call):
                if isinstance(node, ast.Dict):
                    for key in node.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            provided.add(key.value)
            normalized = {p.lower() for p in provided}
            missing = [
                label
                for label, synonyms in _AUDIT_REQUIRED_FIELDS.items()
                if not (normalized & synonyms)
            ]
            if not missing:
                continue
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"AUTH-004 (3): {f.rel}:{call.lineno} the audit record emitted by "
                    f"{fn.name}() is missing {', '.join(missing)} "
                    f"(fields present: {', '.join(sorted(provided)) or 'none'}). An "
                    f"audit row that cannot say why is not an audit row.",
                    "Pass enforcement_point, principal, scope, outcome and reason to "
                    "emit_audit so a reviewer can reconstruct which guard ran, for "
                    "whom, against what scope, with what result and on what grounds.",
                )
            )
    return findings


def _auth004_clause4(check_id: str, routes: list[Route]) -> list[Finding]:
    """(4) One scope dependency per route.

    The contract is four functions run once per request. Two scope
    dependencies on one route resolve the principal twice and write two
    audit rows for a single decision.
    """
    findings: list[Finding] = []
    for route in routes:
        deps = route.scope_dependencies
        if len(deps) <= 1:
            continue
        findings.append(
            _finding(
                check_id,
                _SEVERITY,
                _DIMENSION,
                f"AUTH-004 (4): {route.location} declares {len(deps)} scope "
                f"dependencies ({'; '.join(d.expr for d in deps)}). The verify -> "
                f"resolve -> authorize -> emit_audit chain would run once per "
                f"dependency, auditing the same request twice.",
                "Collapse the guards into a single scope dependency naming the one "
                "scope the route requires; if the route genuinely needs two scopes, "
                "express that inside one require_scope call so the chain still runs "
                "once per request.",
            )
        )
    return findings


def _auth004_clause5(check_id: str, repo_path: Path) -> list[Finding]:
    """(5) The service's own suite exercises the guard on both branches.

    Deterministically this is read as: somewhere under ``tests/`` a
    request goes through the application (a ``TestClient`` or an ASGI
    transport bound to the app) and the suite asserts both a success
    status and a 401/403 refusal. Calling the decision function directly
    does not count — that is the library's own suite — so the assertion
    has to live in a module that builds a client.
    """
    findings: list[Finding] = []
    tests_root = repo_path / "tests"
    if not tests_root.is_dir():
        return [
            _finding(
                check_id,
                _SEVERITY,
                _DIMENSION,
                "AUTH-004 (5): the service has no tests/ directory, so its "
                "enforcement point is never exercised as a whole.",
                "Add tests that drive a real route through the application's "
                "dependency graph with a TestClient: one request allowed by a "
                "granted scope, one refused with 403 for want of it.",
            )
        ]

    saw_allow = False
    saw_deny = False
    for f in _parse_python(tests_root, repo_path, skip_tests=False):
        builds_client = False
        for node in ast.walk(f.tree):
            if isinstance(node, ast.Call) and _call_name(node) in {
                "TestClient",
                "AsyncClient",
                "ASGITransport",
            }:
                builds_client = True
                break
        if not builds_client:
            continue
        for node in ast.walk(f.tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left] + list(node.comparators)
            codes = {
                sub.value
                for operand in operands
                for sub in ast.walk(operand)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, int)
            }
            text = _unparse(node).lower()
            if "status" not in text:
                continue
            if any(200 <= code < 300 for code in codes):
                saw_allow = True
            if codes & {401, 403}:
                saw_deny = True

    if saw_allow and saw_deny:
        return findings

    missing = []
    if not saw_allow:
        missing.append("an allowed request")
    if not saw_deny:
        missing.append("a request denied for want of a scope (401/403)")
    findings.append(
        _finding(
            check_id,
            _SEVERITY,
            _DIMENSION,
            f"AUTH-004 (5): the test suite never exercises the guard end to end — "
            f"no test drives a real route through the application and asserts "
            f"{' or '.join(missing)}.",
            "Add a test that calls a scope-guarded route through a TestClient with "
            "a principal that holds the scope (expect 2xx) and a second with a "
            "principal that does not (expect 403), so the enforcement point itself "
            "is covered rather than only the decision function.",
        )
    )
    return findings


def check_auth_004(repo_path: Path) -> list[Finding]:
    """AUTH-004: decisions come from the shared contract and are audited.

    The five clauses are checked independently and tagged
    ``AUTH-004 (n)`` in the finding text. Clauses (1), (4) and (5) are
    gated on the repo actually registering routes: a repo with no route
    registrations has no enforcement point, and demanding that it import
    the decision function would be a statement about repo type, which is
    the dispatcher's business rather than this check's.

    Clauses (2) and (3) attach to the guard — the function that calls
    ``authorize`` — because that is where the audit obligation lives.
    A service with no such function produces no findings from them; its
    missing import is already clause (1)'s finding.
    """
    CHECK_ID = "AUTH-004"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    try:
        routes = enumerate_routes(repo_path)
    except Exception:
        routes = []
    if not routes:
        return findings

    files = _parse_python(src, repo_path)
    try:
        guards = _guard_functions(files)
    except Exception:
        guards = []

    for clause in (
        lambda: _auth004_clause1(CHECK_ID, files),
        lambda: _auth004_clause2(CHECK_ID, guards),
        lambda: _auth004_clause3(CHECK_ID, guards),
        lambda: _auth004_clause4(CHECK_ID, routes),
        lambda: _auth004_clause5(CHECK_ID, repo_path),
    ):
        try:
            findings.extend(clause())
        except Exception:
            continue
    return findings


# --- CD-019: shared detection ------------------------------------------------


def _mocks_auth_layer(f: _PyFile) -> bool:
    """True if this test module mocks the auth layer.

    CD-019 (2) exempts tests that mock the auth layer, because a test
    that patches the old surface to prove the new one replaced it is not
    a repo still using the old surface. The exemption is deliberately
    keyed on the presence of mocking machinery, not on the file being a
    test: a test module that genuinely calls a retired helper is still a
    violation, and a stale docstring is never exempt.
    """
    if not f.is_test:
        return False
    for node in ast.walk(f.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in {"respx", "responses", "mock"}:
                    return True
                if alias.name.startswith("unittest.mock"):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("unittest.mock") or module in {
                "mock",
                "respx",
                "pytest_mock",
            }:
                return True
        elif isinstance(node, ast.Name) and node.id in {
            "monkeypatch",
            "mocker",
            "MagicMock",
            "AsyncMock",
        }:
            return True
        elif isinstance(node, ast.Attribute) and node.attr in {"patch", "setattr"}:
            target = _unparse(node).lower()
            if "monkeypatch" in target or "mock" in target:
                return True
    return False


def _string_position(parents: dict[int, ast.AST], node: ast.Constant) -> str:
    """Where in the tree a string literal sits: usage, or a pattern table.

    A retired header name is only ever a string, so CD-019 (2) cannot
    avoid looking at strings — but it can ask what the string is doing.
    A dict key or value, a call argument, a comparison operand or a
    module constant is the header being used. An element of a bare
    tuple/list/set is a table of patterns, which is what a checker's own
    source looks like, and is not reported.
    """
    parent = parents.get(id(node))
    if parent is None:
        return "module-level string"
    if isinstance(parent, (ast.Tuple, ast.List, ast.Set)):
        return ""
    if isinstance(parent, ast.Dict):
        return "dict entry"
    if isinstance(parent, ast.Call):
        return "call argument"
    if isinstance(parent, ast.keyword):
        return "keyword argument"
    if isinstance(parent, ast.Compare):
        return "comparison"
    if isinstance(parent, (ast.Assign, ast.AnnAssign)):
        return "assigned constant"
    if isinstance(parent, (ast.JoinedStr, ast.FormattedValue, ast.BinOp)):
        return "interpolated string"
    if isinstance(parent, ast.Subscript):
        return "subscript key"
    return ""


def _is_embedded_python_source(value: str) -> bool:
    """True when a string constant is a quoted Python *module*, not prose.

    A checker's fixtures quote the very patterns the checker hunts for.
    A module-level constant holding a triple-quoted block of Python that
    imports httpx and builds a bad header is a test fixture describing a
    violation, not a repo committing one — nothing in a string literal
    is ever executed, and flagging it is how the retired CD-012 check
    ended up reporting its own detection logic.

    The test is deliberately narrow, because "it is only a string" is
    otherwise an easy place to hide a real violation. A value qualifies
    only when it spans multiple lines, parses as Python, and contains at
    least one statement that is not a bare expression. That excludes the
    shapes a genuine violation takes — ``"X-Internal-API-Key"`` does not
    parse at all, and a single header name parses as a bare string
    expression, not as a module.

    Docstrings never reach this function: they are matched earlier and
    stay flagged, because a stale docstring documenting a symbol that no
    longer exists is explicitly not exempt.
    """
    if "\n" not in value:
        return False
    try:
        tree = ast.parse(value)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return False
    return any(not isinstance(stmt, ast.Expr) for stmt in tree.body)


def _retired_hits_in_python(f: _PyFile) -> list[tuple[int, str, str]]:
    """(line, symbol, where) for every retired symbol used in one module."""
    hits: list[tuple[int, str, str]] = []
    doc_ids = _docstring_constants(f.tree)
    parents = _parents(f.tree)

    for node in ast.walk(f.tree):
        name = ""
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, ast.keyword):
            name = node.arg or ""
        elif isinstance(node, ast.alias):
            name = (node.name or "").split(".")[-1]
        if name and name in _RETIRED_IDENTIFIERS:
            hits.append((getattr(node, "lineno", 0), name, "code reference"))

    wire = list(_RETIRED_WIRE_STRINGS) + ["MACHINE_SECRET"]
    for node in ast.walk(f.tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        for needle in wire:
            if needle.lower() not in node.value.lower():
                continue
            symbol = needle if needle != "MACHINE_SECRET" else "machine_secret"
            if id(node) in doc_ids:
                hits.append((node.lineno, symbol, "docstring"))
                continue
            if _is_embedded_python_source(node.value):
                # A quoted module is a fixture, not a violation.
                continue
            position = _string_position(parents, node)
            if position:
                hits.append((node.lineno, symbol, position))

    # Retired identifiers named in prose: a stale docstring documenting a
    # helper that no longer exists is explicitly not exempt.
    for node in ast.walk(f.tree):
        if id(node) not in doc_ids or not isinstance(node, ast.Constant):
            continue
        for symbol in sorted(_RETIRED_IDENTIFIERS):
            if symbol in str(node.value):
                hits.append((node.lineno, symbol, "docstring"))
    return hits


def _clerk_secret_violations(f: _PyFile) -> list[tuple[int, str]]:
    """Where CLERK_SECRET_KEY is used to obtain or verify a credential.

    CLERK_SECRET_KEY is **not** a violation by itself. A frontend that
    hands it to Clerk's server SDK for session handling or to read user
    profiles from Clerk's backend API is doing the legitimate thing, and
    a blanket flag would fire on a correct repo. CD-019 (2) names two
    narrow uses that are violations, and only those are reported here:

      1. the secret feeds a call to the retired ``m2m_tokens`` surface,
         or to a token-acquisition helper (minting or exchanging a
         credential); or
      2. the secret is placed into an ``Authorization`` header on an
         outbound HTTP call — the repo presenting a minted credential to
         an ecosystem API rather than its own named key.

    ``Clerk(bearer_auth=os.environ["CLERK_SECRET_KEY"])`` followed by
    ``clerk.users.get(...)`` matches neither and is passed.

    The secret is recognised structurally — a string constant that *is*
    the variable name, or an identifier spelled ``clerk_secret_key`` —
    rather than by asking whether the unparsed call contains the text
    anywhere. A containment test would fire on any expression that
    merely quotes the name, which is what a checker's own test fixtures
    and any documentation snippet look like; that self-flagging is
    precisely what got the retired CD-012 check deleted.
    """
    out: list[tuple[int, str]] = []
    parents = _parents(f.tree)

    def _mentions_clerk_secret(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if sub.value in _CLERK_SECRET_NAMES:
                    return True
            elif (
                isinstance(sub, ast.Name)
                and sub.id.lower() == "clerk_secret_key"
                or isinstance(sub, ast.Attribute)
                and sub.attr.lower() == "clerk_secret_key"
            ):
                return True
        return False

    secret_names: set[str] = set()
    for node in ast.walk(f.tree):
        if not isinstance(node, ast.Assign) or not node.targets:
            continue
        if not _mentions_clerk_secret(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                secret_names.add(target.id)

    def _references_secret(node: ast.AST) -> bool:
        if _mentions_clerk_secret(node):
            return True
        return bool(_names_in(node) & secret_names)

    for node in ast.walk(f.tree):
        if not isinstance(node, ast.Call) or not _references_secret(node):
            continue
        text = _unparse(node).lower()
        if "m2m_token" in text:
            out.append(
                (
                    node.lineno,
                    "CLERK_SECRET_KEY is passed to the retired m2m_tokens surface, "
                    "which obtains or verifies a machine credential",
                )
            )
        elif _call_name(node) in _TOKEN_ACQUISITION_CALLS:
            out.append(
                (
                    node.lineno,
                    f"CLERK_SECRET_KEY is passed to {_call_name(node)}(), acquiring a "
                    f"credential the caller then presents",
                )
            )

    for node in ast.walk(f.tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=False):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if key.value.lower() != "authorization":
                continue
            if not _references_secret(value):
                continue
            if _enclosing_http_call(parents, node) is None:
                continue
            out.append(
                (
                    node.lineno,
                    "CLERK_SECRET_KEY is built into an Authorization header on an "
                    "outbound HTTP call",
                )
            )
    return out


def _url_argument(node: ast.Call) -> str:
    """The URL expression of an HTTP call — first positional, or ``url=``.

    Matching the ecosystem hint against the *whole* unparsed call was a
    defect: ``client.post("/v1/contact", headers={"origin":
    "https://kaianolevine.com"})`` matched on the origin header, and
    ``client.post("/v1/evaluations", json={"repo":
    "api-kaianolevine-com"})`` matched on a value inside the payload.
    Neither is a call to an ecosystem API — the hint has to be tested
    against the destination, not against everything the request carries.
    """
    if node.args:
        return _unparse(node.args[0])
    for kw in node.keywords:
        if kw.arg in {"url", "path"}:
            return _unparse(kw.value)
    return ""


def _ecosystem_client_sites(
    files: list[_PyFile],
) -> tuple[list[tuple[str, int, str]], set[str], bool]:
    """(client constructions, machine names seen, any ecosystem outbound call).

    A construction is recognised structurally — a call to ``*ApiClient``
    or to its ``from_env`` classmethod — rather than by matching a URL,
    for the same reason routes are enumerated by registration: the base
    URL is configuration and often never appears as a literal.

    Test modules are excluded. A ``client.post("/v1/contact", ...)`` in a
    service's own test suite is a FastAPI ``TestClient`` exercising the
    app in process — no HTTP, no credential, nothing to attribute — and
    reading it as an unattributed ecosystem call put dozens of false
    ERRORs on api-kaianolevine-com in the 2026-09-03 fleet run. Clauses
    (5)-(9) already filtered tests; clause (1) did not, and this is
    where that belongs so every caller of this helper inherits it.
    """
    constructions: list[tuple[str, int, str]] = []
    machine_names: set[str] = set()
    outbound = False

    for f in files:
        if f.is_test:
            continue
        for node in ast.walk(f.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            owner = ""
            if isinstance(func, ast.Attribute) and func.attr == "from_env":
                owner = _unparse(func.value)
                if _ECOSYSTEM_CLIENT_RE.search(owner):
                    outbound = True
                    literals = [
                        a.value
                        for a in node.args
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    ]
                    if literals:
                        machine_names.update(literals)
                        constructions.append((f.rel, node.lineno, _unparse(node)))
                    else:
                        constructions.append((f.rel, node.lineno, _unparse(node)))
                continue
            name = _call_name(node)
            if _ECOSYSTEM_CLIENT_RE.search(name):
                outbound = True
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                if "machine_name" in kwargs:
                    for kw in node.keywords:
                        if (
                            kw.arg == "machine_name"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)
                        ):
                            machine_names.add(kw.value.value)
                constructions.append((f.rel, node.lineno, _unparse(node)))
                continue
            if _looks_like_http_call(node):
                target = _url_argument(node)
                if any(hint in target for hint in _ECOSYSTEM_URL_HINTS):
                    outbound = True
                    constructions.append((f.rel, node.lineno, _unparse(node)))
    return constructions, machine_names, outbound


# --- CD-019: caller steps (1)-(4) --------------------------------------------


def _cd019_clause1(
    check_id: str, files: list[_PyFile]
) -> tuple[list[Finding], set[str], bool]:
    """(1) The outbound client is constructed with a machine name."""
    findings: list[Finding] = []
    constructions, machine_names, outbound = _ecosystem_client_sites(files)
    if not outbound:
        return findings, machine_names, outbound

    for rel, lineno, expr in constructions:
        text = expr
        named = False
        if "from_env(" in text:
            named = not re.search(r"from_env\(\s*\)", text)
        if "machine_name" in text:
            named = True
        if named:
            continue
        findings.append(
            _finding(
                check_id,
                _SEVERITY,
                _DIMENSION,
                f"CD-019 (1): {rel}:{lineno} calls an ecosystem API through "
                f"`{text}` without naming the machine — the call authenticates as "
                f"the unnamed fallback and its writes are unattributable.",
                "Construct the client with the repo's machine name — "
                'KaianoApiClient.from_env("<machine-name>") or '
                'KaianoApiClient(machine_name="<machine-name>") — so it resolves '
                "that machine's own <MACHINE_NAME>_API_KEY from the environment.",
            )
        )
    return findings, machine_names, outbound


def _cd019_clause2(
    check_id: str, repo_path: Path, files: list[_PyFile]
) -> list[Finding]:
    """(2) No reference to any symbol retired with Clerk M2M."""
    findings: list[Finding] = []

    for f in files:
        if _mocks_auth_layer(f):
            continue
        for lineno, symbol, where in _retired_hits_in_python(f):
            if (
                f.is_test
                and _is_inside_string_literal(f.source, symbol)
                and where != "docstring"
            ):
                # A fixture module holding source snippets as strings, not a
                # module using the retired surface.
                continue
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"CD-019 (2): {f.rel}:{lineno} references the retired symbol "
                    f"{symbol!r} ({where}). Clerk M2M was removed outright — "
                    f"machines present named API keys compared in process.",
                    _RETIRED_REPLACEMENT.get(
                        symbol,
                        "Remove the retired symbol; machines authenticate with their "
                        "own named API key resolved from the environment.",
                    ),
                )
            )
        for lineno, why in _clerk_secret_violations(f):
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"CD-019 (2): {f.rel}:{lineno} {why}. CLERK_SECRET_KEY is "
                    f"legitimate for Clerk session handling and profile reads, but "
                    f"not for obtaining or verifying a credential presented to an "
                    f"ecosystem API.",
                    "Drop the credential acquisition and authenticate with this "
                    'repo\'s own named key — KaianoApiClient.from_env("<machine-name>") '
                    "reading <MACHINE_NAME>_API_KEY. Keep CLERK_SECRET_KEY only for "
                    "Clerk's server SDK.",
                )
            )

    # Text surfaces: .env.example, README and CI config.
    text_targets: list[Path] = [
        repo_path / ".env.example",
        repo_path / "README.md",
        repo_path / "README.rst",
        repo_path / ".gitlab-ci.yml",
    ]
    workflows = repo_path / ".github" / "workflows"
    if workflows.is_dir():
        text_targets.extend(
            sorted(p for p in workflows.iterdir() if p.suffix in {".yml", ".yaml"})
        )

    tokens = (
        sorted(_RETIRED_IDENTIFIERS) + list(_RETIRED_WIRE_STRINGS) + ["MACHINE_SECRET"]
    )
    for target in text_targets:
        if not target.is_file():
            continue
        content = _read_text(target)
        if not content:
            continue
        lowered = content.lower()
        try:
            rel = str(target.relative_to(repo_path))
        except ValueError:
            rel = target.name
        for token in tokens:
            if token.lower() not in lowered:
                continue
            symbol = token if token != "MACHINE_SECRET" else "machine_secret"
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"CD-019 (2): {rel} still documents the retired symbol "
                    f"{symbol!r}; the Clerk M2M surface it names no longer exists.",
                    _RETIRED_REPLACEMENT.get(
                        symbol,
                        "Remove the retired symbol from this file and document the "
                        "repo's own <MACHINE_NAME>_API_KEY instead.",
                    ),
                )
            )
    return findings


def _cd019_clause3(check_id: str, files: list[_PyFile]) -> list[Finding]:
    """(3) The Authorization header is never built from a self-minted credential.

    Callers present a credential they were configured with or handed —
    a named machine key, or a human's Clerk session. Minting one
    locally (signing a JWT) or exchanging for one and putting the result
    on the wire is the pattern that Clerk M2M's removal was meant to end.
    """
    findings: list[Finding] = []
    for f in files:
        parents = _parents(f.tree)
        minted: dict[str, tuple[int, str]] = {}
        for node in ast.walk(f.tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            target = _unparse(node.func).lower()
            is_mint = False
            description = ""
            if name in _JWT_SIGNING_CALLS and (
                "jwt" in target or "jose" in target or "jws" in target
            ):
                is_mint = True
                description = f"a locally signed JWT ({_unparse(node.func)})"
            elif name in _TOKEN_ACQUISITION_CALLS:
                is_mint = True
                description = f"a token acquired by {name}()"
            elif _looks_like_http_call(node) and re.search(
                r"m2m_token|oauth/token|/token\b|token_exchange", _unparse(node)
            ):
                is_mint = True
                description = "a token-exchange response"
            if not is_mint:
                continue
            parent = parents.get(id(node))
            while parent is not None and not isinstance(
                parent, (ast.Assign, ast.Module)
            ):
                parent = parents.get(id(parent))
            if isinstance(parent, ast.Assign):
                for t in parent.targets:
                    if isinstance(t, ast.Name):
                        minted[t.id] = (node.lineno, description)

        for node in ast.walk(f.tree):
            header_value: ast.AST | None = None
            lineno = 0
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=False):
                    if (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and key.value.lower() == "authorization"
                    ):
                        header_value = value
                        lineno = node.lineno
            elif isinstance(node, ast.Assign) and node.targets:
                first = node.targets[0]
                if isinstance(first, ast.Subscript):
                    index = first.slice
                    if (
                        isinstance(index, ast.Constant)
                        and isinstance(index.value, str)
                        and index.value.lower() == "authorization"
                    ):
                        header_value = node.value
                        lineno = node.lineno
            if header_value is None:
                continue
            referenced = _names_in(header_value)
            hit = ""
            for name in sorted(referenced & set(minted)):
                hit = minted[name][1]
            if not hit:
                for sub in ast.walk(header_value):
                    if (
                        isinstance(sub, ast.Call)
                        and _call_name(sub) in _JWT_SIGNING_CALLS
                    ):
                        hit = f"a locally signed JWT ({_unparse(sub.func)})"
            if not hit:
                continue
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"CD-019 (3): {f.rel}:{lineno} builds the Authorization header "
                    f"from {hit} — a credential this repo minted for itself.",
                    "Present the credential the repo was configured with instead: the "
                    "machine's named key from <MACHINE_NAME>_API_KEY via "
                    'KaianoApiClient.from_env("<machine-name>"), or the Clerk '
                    "session the human handed you. Callers never mint credentials.",
                )
            )
    return findings


def _cd019_clause4(
    check_id: str, repo_path: Path, machine_names: set[str]
) -> list[Finding]:
    """(4) .env.example documents the repo's own <MACHINE_NAME>_API_KEY."""
    findings: list[Finding] = []
    env_example = repo_path / ".env.example"
    expected = sorted(
        f"{name.upper().replace('-', '_')}_API_KEY" for name in machine_names
    )
    if not env_example.is_file():
        findings.append(
            _finding(
                check_id,
                _SEVERITY,
                _DIMENSION,
                "CD-019 (4): the repo calls an ecosystem API but has no .env.example, "
                "so its own "
                + (expected[0] if expected else "<MACHINE_NAME>_API_KEY")
                + " is undocumented.",
                "Add .env.example declaring "
                + (expected[0] if expected else "<MACHINE_NAME>_API_KEY")
                + " with a note that it is this machine's named key from Doppler and "
                "that there is no fallback — unset means 401 on every call.",
            )
        )
        return findings

    content = _read_text(env_example)
    if expected:
        for variable in expected:
            if variable in content:
                continue
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"CD-019 (4): .env.example does not declare {variable}, the named "
                    f"key for the machine this repo authenticates as.",
                    f"Add {variable} to .env.example with a note that it is this "
                    f"machine's key from Doppler, matching the machine name passed to "
                    f"the API client, and that there is no unnamed fallback.",
                )
            )
    elif not re.search(r"^[A-Z0-9_]+_API_KEY", content, re.MULTILINE):
        findings.append(
            _finding(
                check_id,
                _SEVERITY,
                _DIMENSION,
                "CD-019 (4): the repo calls an ecosystem API but .env.example declares "
                "no <MACHINE_NAME>_API_KEY variable at all.",
                "Declare the repo's own <MACHINE_NAME>_API_KEY in .env.example, "
                "matching the machine name the API client is constructed with, so "
                "deployments know which Doppler secret to bind.",
            )
        )
    return findings


# --- CD-019: receiver steps (5)-(9) ------------------------------------------


def _in_identity_library(f: _PyFile) -> bool:
    """True for the shared library's own source, vendored into the repo.

    Steps (5) and (7) forbid verification primitives "written outside
    that library"; if the library itself is present, its own JWKS
    handling is the sanctioned implementation.
    """
    norm = f.rel.replace("\\", "/")
    return "/identity/" in f"/{norm}" or norm.startswith("identity/")


# Vocabulary that marks a function as handling the caller's credential.
# Deliberately excludes bare "token" and "secret" — a CAPTCHA response is
# a token and a webhook signing key is a secret.
_CREDENTIAL_VOCAB = (
    "authorization",
    "bearer",
    "jwks",
    "jwt",
    "clerk",
    "api_key",
    "apikey",
    "principal",
    "machine_key",
    "machine_name",
    "issuer",
    "access_token",
    "id_token",
    "session_token",
    "credential",
)


def _handles_caller_credential(fn: ast.AST) -> bool:
    """True when a function's body names the caller-credential vocabulary."""
    haystack = (
        [fn.name.lower()]
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        else []
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            haystack.append(node.id.lower())
        elif isinstance(node, ast.Attribute):
            haystack.append(node.attr.lower())
        elif isinstance(node, ast.arg):
            haystack.append(node.arg.lower())
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            haystack.append(node.value.lower())
    blob = " ".join(haystack)
    return any(word in blob for word in _CREDENTIAL_VOCAB)


def _verification_functions(
    files: list[_PyFile],
) -> list[tuple[_PyFile, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Functions on the **caller-credential** path.

    Named like the contract's first two functions — ``verify_*``,
    ``authenticate``, ``resolve_principal``, ``get_principal`` — *and*
    actually handling a caller credential.

    The second condition is load-bearing. Name alone matched
    ``_verify_turnstile()`` in api-kaianolevine-com, which posts a
    Cloudflare Turnstile response to ``challenges.cloudflare.com`` from
    a contact form. That is a bot check on a public form, not
    verification of the caller's identity, so clause (7)'s "verification
    is local" has nothing to say about it — a third-party challenge is
    remote by construction. Requiring credential vocabulary keeps the
    clause pointed at the JWKS/Clerk/machine-key path it exists to
    police.

    The vocabulary is deliberately specific. Bare ``token`` and
    ``secret`` are not on it: a CAPTCHA response is a token and a
    webhook signing key is a secret, and neither is the caller
    presenting a credential.
    """
    pattern = re.compile(
        r"^(_?verify|_?authenticate|resolve_principal|resolve_caller|get_principal|"
        r"get_caller|check_credential|decode_token|validate_token)",
        re.IGNORECASE,
    )
    out: list[tuple[_PyFile, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for f in files:
        if _in_identity_library(f):
            continue
        for fn in _functions(f.tree):
            if pattern.search(fn.name) and _handles_caller_credential(fn):
                out.append((f, fn))
    return out


def _cd019_clause5(check_id: str, files: list[_PyFile]) -> list[Finding]:
    """(5) Verification is consumed from the shared library, not rewritten."""
    findings: list[Finding] = []
    modules, _names = _imported_modules(files)
    if not any(m == "identity" or m.startswith("identity.") for m in modules):
        findings.append(
            _finding(
                check_id,
                _SEVERITY,
                _DIMENSION,
                "CD-019 (5): the service imports nothing from the shared identity "
                "library (identity.chain / identity.clerk / identity.apikey), so its "
                "credential verification is local.",
                "Import the verification chain from identity (identity.chain for the "
                "request chain, identity.clerk for session JWTs, identity.apikey for "
                "named machine keys) and delete the in-repo equivalent.",
            )
        )

    for f in files:
        if _in_identity_library(f):
            continue
        for node in ast.walk(f.tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            text = _unparse(node)
            lowered = text.lower()
            problem = ""
            if name == "PyJWKClient" or (
                _looks_like_http_call(node) and "jwks" in lowered
            ):
                problem = "fetches JWKS itself"
            elif name == "decode":
                algorithms = ""
                for kw in node.keywords:
                    if kw.arg == "algorithms":
                        algorithms = _unparse(kw.value)
                if re.search(r"RS\d{3}|ES\d{3}|PS\d{3}", algorithms + text):
                    problem = "performs an asymmetric (RS256-family) token decode"
            elif name == "compare_digest":
                problem = "implements its own constant-time credential comparison"
            if not problem:
                continue
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"CD-019 (5): {f.rel}:{node.lineno} {problem} outside the shared "
                    f"identity library (`{text[:120]}`). Verification has one "
                    f"implementation; see XSTACK-005.",
                    "Delete the local primitive and call the shared library instead: "
                    "identity.clerk verifies session JWTs against cached JWKS and "
                    "identity.apikey compares named machine keys in constant time.",
                )
            )
    return findings


def _cd019_clause6(check_id: str, files: list[_PyFile]) -> list[Finding]:
    """(6) Both populations are configured: Clerk issuers and machine keys.

    A service that configures only humans rejects every machine, and one
    that configures only machines rejects every human. Both failures
    look like a credential bug at the caller, which is why the rule
    checks the configuration rather than waiting for the 401.
    """
    findings: list[Finding] = []
    literals: set[str] = set()
    attributes: set[str] = set()
    joined_parts: list[str] = []
    for f in files:
        for node in ast.walk(f.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
            elif isinstance(node, ast.Attribute):
                attributes.add(node.attr)
            elif isinstance(node, ast.JoinedStr):
                joined_parts.append(_unparse(node))

    def _seen(*candidates: str) -> bool:
        for candidate in candidates:
            if candidate in literals or candidate.lower() in attributes:
                return True
        return False

    clerk_configured = _seen("CLERK_ISSUERS") or (
        _seen("CLERK_ISSUER") and _seen("CLERK_JWKS_URL")
    )
    machine_configured = (
        any(value.endswith("_API_KEY") for value in literals)
        or any("_API_KEY" in part for part in joined_parts)
        or any("_API_KEY" in value for value in literals)
    )

    if clerk_configured and machine_configured:
        return findings
    missing = []
    if not clerk_configured:
        missing.append(
            "the Clerk issuer set (CLERK_ISSUERS, or CLERK_ISSUER together with "
            "CLERK_JWKS_URL)"
        )
    if not machine_configured:
        missing.append(
            "the machine-key set built from per-machine <NAME>_API_KEY variables"
        )
    findings.append(
        _finding(
            check_id,
            _SEVERITY,
            _DIMENSION,
            "CD-019 (6): the service configures only one credential population — "
            "no configuration was found for " + " and ".join(missing) + ".",
            "Configure both populations at startup: build the Clerk issuer set from "
            "CLERK_ISSUERS (or CLERK_ISSUER + CLERK_JWKS_URL) for humans, and the "
            "machine-key set from the per-machine <MACHINE_NAME>_API_KEY environment "
            "variables for machines. A service missing either rejects that whole "
            "population.",
        )
    )
    return findings


def _cd019_clause7(check_id: str, files: list[_PyFile]) -> list[Finding]:
    """(7) Verification is local — no outbound call on the credential path."""
    findings: list[Finding] = []
    for f, fn in _verification_functions(files):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            text = _unparse(node)
            if not (_looks_like_http_call(node) or "api.clerk.com" in text):
                continue
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"CD-019 (7): {f.rel}:{node.lineno} {fn.name}() makes an outbound "
                    f"HTTP call (`{text[:120]}`) inside the verification path. "
                    f"Verification is local: a session JWT is checked offline against "
                    f"cached JWKS and a machine key is compared in process.",
                    "Remove the network call from verification. Refresh JWKS on a "
                    "cache schedule outside the request path and verify the token "
                    "offline; compare the named machine key in process with the "
                    "shared identity.apikey helper.",
                )
            )
    return findings


def _cd019_clause8(
    check_id: str, repo_path: Path, files: list[_PyFile]
) -> list[Finding]:
    """(8) The store holds names and grants, never key material."""
    findings: list[Finding] = []

    def _report(rel: str, lineno: int, column: str, context: str) -> None:
        findings.append(
            _finding(
                check_id,
                _SEVERITY,
                _DIMENSION,
                f"CD-019 (8): {rel}:{lineno} persists key material — {context} column "
                f"{column!r} on a principal/machine/issuer table. The store holds "
                f"names and grants only.",
                "Drop the column. A machine's key lives in Doppler and reaches the "
                "API as <MACHINE_NAME>_API_KEY; the table records the machine's name "
                "and its grants, so a rotated key needs no migration.",
            )
        )

    for f in files:
        for node in ast.walk(f.tree):
            if isinstance(node, ast.ClassDef):
                # The table this model maps to: __tablename__ when it is
                # declared, the class name otherwise.
                table = node.name
                for stmt in node.body:
                    if not (isinstance(stmt, ast.Assign) and stmt.targets):
                        continue
                    target = stmt.targets[0]
                    if (
                        isinstance(target, ast.Name)
                        and target.id == "__tablename__"
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)
                    ):
                        table = stmt.value.value
                if not _KEY_TABLE_RE.search(table):
                    continue
                for stmt in node.body:
                    if not (isinstance(stmt, (ast.Assign, ast.AnnAssign))):
                        continue
                    target = (
                        stmt.targets[0] if isinstance(stmt, ast.Assign) else stmt.target
                    )
                    if not isinstance(target, ast.Name):
                        continue
                    value = stmt.value
                    if not (
                        isinstance(value, ast.Call)
                        and _call_name(value) in {"Column", "mapped_column"}
                    ):
                        continue
                    column = target.id
                    if column in _KEY_COLUMN_EXEMPT or not _KEY_COLUMN_RE.search(
                        column
                    ):
                        continue
                    _report(f.rel, stmt.lineno, column, f"model {node.name} declares")

            if isinstance(node, ast.Call) and _call_name(node) in {
                "create_table",
                "add_column",
            }:
                literals = _string_constants(node)
                table = literals[0] if literals else ""
                if not _KEY_TABLE_RE.search(table):
                    continue
                for sub in ast.walk(node):
                    if not (isinstance(sub, ast.Call) and _call_name(sub) == "Column"):
                        continue
                    names = _string_constants(sub)
                    if not names:
                        continue
                    column = names[0]
                    if column in _KEY_COLUMN_EXEMPT or not _KEY_COLUMN_RE.search(
                        column
                    ):
                        continue
                    _report(f.rel, sub.lineno, column, f"migration on {table!r} adds")

            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                statement = node.value
                lowered = statement.lower()
                if "insert into" not in lowered:
                    continue
                if not _KEY_TABLE_RE.search(statement):
                    continue
                columns = [
                    word
                    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", statement)
                    if _KEY_COLUMN_RE.fullmatch(word) or _KEY_COLUMN_RE.search(word)
                ]
                columns = [c for c in columns if c.lower() not in _KEY_COLUMN_EXEMPT]
                columns = [
                    c
                    for c in columns
                    if re.search(r"key|secret|token|hash", c, re.IGNORECASE)
                ]
                if not columns:
                    continue
                _report(f.rel, node.lineno, columns[0], "an INSERT writes")

    migrations = [
        p
        for folder in ("migrations", "alembic")
        for p in (repo_path / folder).rglob("*.py")
        if (repo_path / folder).is_dir()
    ]
    for py in migrations:
        source = _read_text(py)
        if not source:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        rel = str(py.relative_to(repo_path)).replace("\\", "/")
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and _call_name(node) in {"create_table", "add_column"}
            ):
                continue
            literals = _string_constants(node)
            table = literals[0] if literals else ""
            if not _KEY_TABLE_RE.search(table):
                continue
            for sub in ast.walk(node):
                if not (isinstance(sub, ast.Call) and _call_name(sub) == "Column"):
                    continue
                names = _string_constants(sub)
                if not names:
                    continue
                column = names[0]
                if column in _KEY_COLUMN_EXEMPT or not _KEY_COLUMN_RE.search(column):
                    continue
                _report(rel, sub.lineno, column, f"migration on {table!r} adds")
    return findings


def _cd019_clause9(check_id: str, files: list[_PyFile]) -> list[Finding]:
    """(9) The credential path never reads the machine name from the request.

    The key names the caller. A request that says which machine it is
    lets any holder of any key claim any name, which is the failure the
    named-key model exists to prevent.
    """
    findings: list[Finding] = []
    caller_name_re = re.compile(
        r"machine|service|caller|principal|client_id|actor|owner"
    )
    for f, fn in _verification_functions(files):
        for lineno, what, _why in _request_identity_reads(fn):
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"CD-019 (9): {f.rel}:{lineno} {fn.name}() reads the caller's "
                    f"identity from the request on the credential path — {what}.",
                    "Delete the request-supplied name. The presented credential "
                    "identifies the caller: a named machine key resolves to its "
                    "machine, a Clerk session JWT to its human.",
                )
            )
        args = list(fn.args.args) + list(fn.args.kwonlyargs)
        for arg in args:
            if not caller_name_re.search(arg.arg.lower()):
                continue
            sources: list[str] = []
            for candidate in ([arg.annotation] if arg.annotation else []) + list(
                fn.args.defaults
            ):
                for sub in ast.walk(candidate):
                    if (
                        isinstance(sub, ast.Call)
                        and _call_name(sub) in _REQUEST_PARAM_SOURCES
                    ):
                        sources.append(_call_name(sub))
            if not sources:
                continue
            findings.append(
                _finding(
                    check_id,
                    _SEVERITY,
                    _DIMENSION,
                    f"CD-019 (9): {f.rel}:{fn.lineno} {fn.name}() takes the caller's "
                    f"name from the request ({sources[0]}(...) -> {arg.arg!r}) on the "
                    f"credential path.",
                    "Remove the parameter and derive the machine name from the key "
                    "that was presented — the environment variable the key matched "
                    "names the machine; a request-supplied name can be anything.",
                )
            )
    return findings


def check_cd_019(repo_path: Path, *, repo_type: str = "") -> list[Finding]:
    """CD-019: the bearer credential contract.

    Humans present a Clerk session JWT; machines present a named API key
    that lives in Doppler, arrives as ``<MACHINE_NAME>_API_KEY`` and is
    compared in process in constant time. Clerk M2M was removed
    outright: there is no token to mint and no ``m2m_tokens/verify``
    endpoint to call. Credentials are routed structurally by dot count —
    two dots is a session JWT, zero dots is a named key.

    The catalog splits the rule by which side of the call the repo is
    on. Steps (1)-(4) are the caller's obligations and steps (5)-(9) the
    receiver's, and ``repo_type`` selects between them: ``"api-service"``
    runs the receiver steps, anything else the caller steps. An
    api-service that also makes outbound ecosystem calls is both, and
    runs both halves — its outbound calls are as unattributable as any
    cog's if it does not name itself.

    Findings are deduplicated by text: the same violation can be reached
    from two clauses (a token acquisition is both a retired symbol and a
    self-minted credential), and reporting it twice would make the
    remediation list longer without making it more informative.
    """
    CHECK_ID = "CD-019"
    findings: list[Finding] = []
    src = repo_path / "src"
    # Tests are parsed too: CD-019 (2) has its own, narrower exemption for
    # test modules that mock the auth layer, applied per file.
    files = _parse_python(src, repo_path, skip_tests=False)
    tests_root = repo_path / "tests"
    if tests_root.is_dir():
        files = files + _parse_python(tests_root, repo_path, skip_tests=False)

    is_receiver = repo_type == "api-service"
    machine_names: set[str] = set()
    outbound = False

    try:
        clause1_findings, machine_names, outbound = _cd019_clause1(CHECK_ID, files)
    except Exception:
        clause1_findings = []

    run_caller = (not is_receiver) or outbound
    if run_caller:
        if outbound:
            findings.extend(clause1_findings)
        for clause in (
            lambda: _cd019_clause2(CHECK_ID, repo_path, files),
            lambda: _cd019_clause3(CHECK_ID, files),
        ):
            try:
                findings.extend(clause())
            except Exception:
                continue
        if outbound:
            with contextlib.suppress(Exception):
                findings.extend(_cd019_clause4(CHECK_ID, repo_path, machine_names))

    if is_receiver:
        receiver_files = [f for f in files if not f.is_test]
        clauses = [
            lambda: _cd019_clause5(CHECK_ID, receiver_files),
            lambda: _cd019_clause6(CHECK_ID, receiver_files),
            lambda: _cd019_clause7(CHECK_ID, receiver_files),
            lambda: _cd019_clause8(CHECK_ID, repo_path, receiver_files),
            lambda: _cd019_clause9(CHECK_ID, receiver_files),
        ]
        if not run_caller:
            clauses.insert(0, lambda: _cd019_clause2(CHECK_ID, repo_path, files))
        for clause in clauses:
            try:
                findings.extend(clause())
            except Exception:
                continue

    seen: set[str] = set()
    unique: list[Finding] = []
    for item in findings:
        key = item.get("finding", "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique
