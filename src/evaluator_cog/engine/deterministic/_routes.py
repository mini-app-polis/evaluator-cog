"""AST route enumeration for the identity rules (AUTH-003, AUTH-004, CD-019).

Enumerate routes **by their registration call, never by matching literal
path strings.**

This is not a stylistic preference. In api-kaianolevine-com's
``wcs_wiki.py`` four routes are registered by a module-level factory:
the decorator is applied inside a function whose path argument is a
variable, so the route has no literal path anywhere in the file. That
is precisely how those four went unguarded originally. A path-regex
scan would miss them a second time, and the rule exists to catch
exactly the routes a path scan cannot see.

So the unit of enumeration here is the *registration* — a
route-method decorator, or an ``add_api_route(...)`` call — and the
path is recorded as whatever expression appears, resolved to a string
only when it happens to be a literal. ``Route.path`` is ``None`` for a
variable path and that is a normal, expected value, not an error.

The second design rule: **unresolvable is a finding, not a pass.**
When a dependency cannot be read statically — it is a bare name, it
comes from a variable, it is built by a call we cannot see into — the
route is marked ``resolvable=False`` and the caller is expected to
flag it. An unknown route is the case the rule exists to catch, and
silently treating "I could not tell" as "it was fine" would reproduce
the original failure with better tooling.

Nested definitions are walked, which is what makes the factory case
work: ``ast.walk`` reaches a ``FunctionDef`` inside another function
body just as readily as one at module level, and a route registered
inside a factory is found by its decorator regardless of when the
factory runs.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

# HTTP verbs that register a route when used as a decorator attribute
# (``@router.get``, ``@app.post``) — plus ``api_route``/``websocket``,
# which register too and would otherwise be invisible.
ROUTE_METHODS = frozenset(
    {
        "get",
        "post",
        "put",
        "delete",
        "patch",
        "head",
        "options",
        "trace",
        "api_route",
        "websocket",
    }
)

# The dependency-injection wrappers whose argument is the guard.
DEPENDENCY_MARKERS = frozenset({"Depends", "Security"})

# A string that looks like a scope: dot-separated lowercase identifier
# segments, two or more. Deliberately looser than the grammar so that a
# malformed scope is *found* and then reported by AUTH-003 step (5),
# rather than going unrecognized and silently classifying its route as
# authenticated-only.
SCOPE_LIKE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")

# The grammar: exactly <domain>.<resource>.<action>.
SCOPE_SEGMENTS = 3


def is_scope_like(value: str) -> bool:
    """True for anything shaped like a scope, valid or not.

    Deliberately looser than the grammar so a malformed scope is found
    and reported by AUTH-003 (5) rather than going unrecognised and
    silently classifying its route as authenticated-only.
    """
    return bool(SCOPE_LIKE_RE.match(value))


def is_valid_scope(value: str) -> bool:
    """True for the grammar exactly: <domain>.<resource>.<action>."""
    return is_scope_like(value) and value.count(".") == SCOPE_SEGMENTS - 1


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - ast.unparse is total in 3.9+
        return "<unparseable>"


def _string_literals(node: ast.AST) -> list[str]:
    """Every string constant appearing anywhere inside ``node``."""
    out: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return out


# Dependency names that are infrastructure, not guards. A route injecting
# a database session or a settings object is not thereby authenticated —
# it is a public route that needs a session.
#
# This distinction is load-bearing. Without it every route doing
# `Depends(get_db_session)` classified as "unresolvable" and was flagged
# as a route whose guard could not be read, which put 21 false AUTH-003
# ERRORs on api-kaianolevine-com in the 2026-09-03 fleet run — a service
# whose route table audits clean.
INFRASTRUCTURE_DEP_HINTS = (
    "db",
    "database",
    "session",
    "settings",
    "config",
    "engine",
    "conn",
    "connection",
    "pool",
    "logger",
    "log",
    "cache",
    "redis",
    "storage",
    "bucket",
    "tracer",
    "metrics",
)

# Names that mark a dependency as part of the credential path. A
# dependency matching neither list is left unresolved and therefore
# flagged — "I could not tell" must not resolve to "it was fine", which
# is the failure this rule exists to catch.
AUTH_DEP_HINTS = (
    "auth",
    "require",
    "verify",
    "current_user",
    "current_principal",
    "principal",
    "scope",
    "permission",
    "security",
    "caller",
    "token",
    "credential",
    "guard",
    "identity",
    "clerk",
    # "owner" is this fleet's third word for the authenticated subject,
    # alongside "caller" and "principal": api-kaianolevine-com's
    # get_current_owner returns the Clerk `sub`, and rows across that
    # database key ownership by it. Without this, the two intentionally
    # authenticated-only /wcs/me routes classify as unresolvable and
    # AUTH-003 reports a guard it cannot read — when the guard is
    # readable and does exactly what the rule wants.
    "owner",
)


# Dependencies that *resolve* the caller rather than *assert* a
# requirement. `get_current_owner`, `get_current_user`,
# `current_principal` return who the caller is; none of them can be a
# scope guard, because AUTH-003 defines one as "a dependency that takes
# a scope string" and these take nothing. So a bare
# `Depends(get_current_owner)` is not an unread guard — it is a read
# guard requiring authentication and no scope, which is exactly the
# "authenticated-only" class the rule asks us to classify.
#
# Deliberately narrow. `Depends(require_admin)` stays unresolvable: a
# bare name that *asserts* something may encode authority we cannot see
# from the registration site, and the rule is right to flag it. Only
# names that read as "give me the current X" qualify.
_SUBJECT_RESOLVER = re.compile(r"(^|_)current_[a-z0-9_]+$")


def resolves_subject(callee: str) -> bool:
    """True for a dependency whose name says it returns the caller."""
    return bool(_SUBJECT_RESOLVER.search((callee or "").lower()))


def _dep_kind(callee: str) -> str:
    """'auth', 'infrastructure', or 'unknown' for a dependency callee."""
    name = (callee or "").lower()
    if not name:
        return "unknown"
    if any(hint in name for hint in AUTH_DEP_HINTS):
        return "auth"
    if any(hint in name for hint in INFRASTRUCTURE_DEP_HINTS):
        return "infrastructure"
    return "unknown"


@dataclass
class Dependency:
    """One guard attached to a route.

    ``resolvable`` is the field that matters. It is False when the
    dependency is a bare name (``Depends(current_user)`` — we cannot
    see what that requires) or an expression we cannot read. Callers
    flag those rather than assuming them safe.
    """

    expr: str
    callee: str = ""
    scopes: list[str] = field(default_factory=list)
    resolvable: bool = False
    origin: str = "signature"

    @property
    def is_scope_guard(self) -> bool:
        """True when this dependency names at least one scope string."""
        return bool(self.scopes)

    @property
    def kind(self) -> str:
        """'auth', 'infrastructure' or 'unknown'."""
        if self.scopes:
            return "auth"
        return _dep_kind(self.callee)

    @property
    def is_infrastructure(self) -> bool:
        """A session/settings/logger injection is not a guard."""
        return self.kind == "infrastructure"


@dataclass
class Route:
    """One route registration."""

    file: str
    lineno: int
    func_name: str
    method: str
    registration: str
    path: str | None
    path_expr: str
    decorator_expr: str
    dependencies: list[Dependency] = field(default_factory=list)
    in_factory: bool = False
    node: ast.AST | None = None

    @property
    def location(self) -> str:
        """How a finding should name this route.

        Uses the function, not the path — a factory-registered route
        has no stable path to name, and the function is what a reader
        greps for.
        """
        shown = self.path if self.path is not None else f"<dynamic: {self.path_expr}>"
        return f"{self.file}:{self.lineno} {self.func_name}() [{self.method.upper()} {shown}]"

    @property
    def guard_dependencies(self) -> list[Dependency]:
        """Dependencies that could bear on access — plumbing excluded."""
        return [d for d in self.dependencies if not d.is_infrastructure]

    @property
    def scope_dependencies(self) -> list[Dependency]:
        """The guards on this route that name a scope."""
        return [d for d in self.dependencies if d.is_scope_guard]

    @property
    def scopes(self) -> list[str]:
        """Every scope string this route requires, across all its guards."""
        return [s for d in self.dependencies for s in d.scopes]

    @property
    def unresolvable_dependencies(self) -> list[Dependency]:
        """Guards that could bear on access but could not be read.

        Infrastructure is excluded: a database session is not a guard
        whose requirement is unknown, it is not a guard.
        """
        return [d for d in self.guard_dependencies if not d.resolvable]

    def classify(self) -> str:
        """One of 'scope-guarded', 'authenticated-only', 'public', 'unresolvable'.

        'unresolvable' is returned when the route carries a dependency
        we could not read. It is reported ahead of 'authenticated-only'
        deliberately: a dependency that might be a scope guard and
        might be a bare authentication check must not be silently
        recorded as the weaker of the two.
        """
        if self.scope_dependencies:
            return "scope-guarded"
        if self.unresolvable_dependencies:
            return "unresolvable"
        if self.guard_dependencies:
            return "authenticated-only"
        return "public"


def _build_dependency(node: ast.AST, origin: str) -> Dependency | None:
    """Read one ``Depends(...)`` / ``Security(...)`` expression.

    Returns None when ``node`` is not a dependency marker at all.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    marker = ""
    if isinstance(func, ast.Name):
        marker = func.id
    elif isinstance(func, ast.Attribute):
        marker = func.attr
    if marker not in DEPENDENCY_MARKERS:
        return None

    dep = Dependency(expr=_unparse(node), origin=origin)
    if not node.args:
        # Depends() with no argument — FastAPI infers from the
        # annotation, which we cannot follow. Unresolvable.
        return dep

    inner = node.args[0]
    if isinstance(inner, ast.Call):
        # Depends(require_scope("a.b.c")) — the readable case.
        inner_func = inner.func
        if isinstance(inner_func, ast.Name):
            dep.callee = inner_func.id
        elif isinstance(inner_func, ast.Attribute):
            dep.callee = inner_func.attr
        literals = _string_literals(inner)
        dep.scopes = [s for s in literals if is_scope_like(s)]
        # Resolvable when we can see a call and its literal arguments.
        # A call whose every argument is a variable tells us nothing.
        dep.resolvable = bool(dep.scopes) or not literals and not inner.args
        if literals and not dep.scopes:
            # It has string arguments, none scope-shaped — we read it
            # fine, it just is not a scope guard.
            dep.resolvable = True
    elif isinstance(inner, ast.Name):
        # Depends(current_user) — a bare reference. What it requires is
        # decided elsewhere, unless the name says it resolves the caller
        # rather than asserting a requirement.
        dep.callee = inner.id
        dep.resolvable = resolves_subject(dep.callee)
    elif isinstance(inner, ast.Attribute):
        dep.callee = inner.attr
        dep.resolvable = resolves_subject(dep.callee)
    return dep


def _dependencies_from_signature(func: ast.AST) -> list[Dependency]:
    """Guards declared as parameter defaults or in ``Annotated[...]``."""
    deps: list[Dependency] = []
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return deps

    for default in list(func.args.defaults) + list(func.args.kw_defaults):
        if default is None:
            continue
        dep = _build_dependency(default, "signature")
        if dep is not None:
            deps.append(dep)

    # Annotated[User, Depends(...)] — the modern spelling, invisible to
    # a defaults-only scan.
    all_args = (
        list(func.args.args) + list(func.args.kwonlyargs) + list(func.args.posonlyargs)
    )
    for arg in all_args:
        if arg.annotation is None:
            continue
        for sub in ast.walk(arg.annotation):
            dep = _build_dependency(sub, "annotation")
            if dep is not None:
                deps.append(dep)
    return deps


def _dependencies_from_kwarg(call: ast.Call, origin: str) -> list[Dependency]:
    """Guards passed as ``dependencies=[Depends(...), ...]``.

    A ``dependencies=`` whose value is a variable rather than a list
    display yields one unresolvable dependency, so the route is flagged
    instead of passing for want of a readable guard.
    """
    deps: list[Dependency] = []
    for kw in call.keywords:
        if kw.arg != "dependencies":
            continue
        if isinstance(kw.value, ast.List):
            for element in kw.value.elts:
                dep = _build_dependency(element, origin)
                if dep is not None:
                    deps.append(dep)
                else:
                    deps.append(
                        Dependency(
                            expr=_unparse(element), origin=origin, resolvable=False
                        )
                    )
        else:
            deps.append(
                Dependency(expr=_unparse(kw.value), origin=origin, resolvable=False)
            )
    return deps


def _path_of(call: ast.Call) -> tuple[str | None, str]:
    """(literal path or None, unparsed path expression)."""
    if not call.args:
        for kw in call.keywords:
            if kw.arg == "path":
                if isinstance(kw.value, ast.Constant) and isinstance(
                    kw.value.value, str
                ):
                    return kw.value.value, kw.value.value
                return None, _unparse(kw.value)
        return None, "<no path argument>"
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value, first.value
    return None, _unparse(first)


def _enclosing_function_names(tree: ast.AST) -> dict[ast.AST, bool]:
    """Map each function node to whether it is nested inside another.

    A nested route definition is the factory signal — the registration
    runs when the enclosing function is called, not at import.
    """
    nested: dict[ast.AST, bool] = {}

    def walk(node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested[child] = depth > 0
                walk(child, depth + 1)
            else:
                walk(child, depth)

    walk(tree, 0)
    return nested


def enumerate_routes_in_source(source: str, rel: str) -> list[Route]:
    """Every route registration in one module's source."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    nested = _enclosing_function_names(tree)
    routes: list[Route] = []

    # 1. Decorator registrations — @router.get(...), @app.post(...).
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            if not isinstance(dec.func, ast.Attribute):
                continue
            if dec.func.attr not in ROUTE_METHODS:
                continue
            path, path_expr = _path_of(dec)
            deps = _dependencies_from_signature(node)
            deps.extend(_dependencies_from_kwarg(dec, "decorator"))
            routes.append(
                Route(
                    file=rel,
                    lineno=node.lineno,
                    func_name=node.name,
                    method=dec.func.attr,
                    registration="decorator",
                    path=path,
                    path_expr=path_expr,
                    decorator_expr=_unparse(dec),
                    dependencies=deps,
                    in_factory=nested.get(node, False),
                    node=node,
                )
            )

    # 2. Imperative registrations — add_api_route / add_route /
    #    add_websocket_route. The endpoint is a reference to a function
    #    defined elsewhere, so its signature guards are resolved by
    #    name within this module when possible.
    functions_by_name = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = ""
        if isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        elif isinstance(node.func, ast.Name):
            callee = node.func.id
        if callee not in {"add_api_route", "add_route", "add_websocket_route"}:
            continue

        path, path_expr = _path_of(node)
        endpoint_node: ast.AST | None = None
        endpoint_name = "<unknown endpoint>"
        for kw in node.keywords:
            if kw.arg == "endpoint":
                endpoint_node = kw.value
        if endpoint_node is None and len(node.args) > 1:
            endpoint_node = node.args[1]
        if isinstance(endpoint_node, ast.Name):
            endpoint_name = endpoint_node.id
        elif isinstance(endpoint_node, ast.Attribute):
            endpoint_name = endpoint_node.attr
        elif endpoint_node is not None:
            endpoint_name = _unparse(endpoint_node)

        target = functions_by_name.get(endpoint_name)
        deps = _dependencies_from_signature(target) if target is not None else []
        deps.extend(_dependencies_from_kwarg(node, "add_api_route"))
        if target is None and not deps:
            # The handler lives in another module; we cannot read its
            # guards from here. Unresolvable, not public.
            deps.append(
                Dependency(
                    expr=f"<endpoint {endpoint_name} not defined in this module>",
                    origin="add_api_route",
                    resolvable=False,
                )
            )

        methods = ["route"]
        for kw in node.keywords:
            if kw.arg == "methods" and isinstance(kw.value, ast.List):
                literal_methods = [
                    e.value.lower()
                    for e in kw.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                if literal_methods:
                    methods = literal_methods

        for method in methods:
            routes.append(
                Route(
                    file=rel,
                    lineno=node.lineno,
                    func_name=endpoint_name,
                    method=method,
                    registration="add_api_route",
                    path=path,
                    path_expr=path_expr,
                    decorator_expr=_unparse(node),
                    dependencies=list(deps),
                    in_factory=bool(target is not None and nested.get(target, False)),
                    node=target,
                )
            )

    return routes


def enumerate_routes(repo_path: Path) -> list[Route]:
    """Every route registration under ``src/``, tests excluded.

    Returns [] when there is no ``src/`` — the caller decides whether
    that is a finding.
    """
    src = repo_path / "src"
    if not src.is_dir():
        return []
    routes: list[Route] = []
    for py in sorted(src.rglob("*.py")):
        rel_str = str(py).replace("\\", "/")
        if "/tests/" in rel_str or rel_str.endswith("/conftest.py"):
            continue
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(py.relative_to(repo_path))
        except ValueError:
            rel = py.name
        routes.extend(enumerate_routes_in_source(source, rel))
    return routes
