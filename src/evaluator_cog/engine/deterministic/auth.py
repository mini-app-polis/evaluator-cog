"""Clerk / authentication rule checks."""

from __future__ import annotations

import ast
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)


def check_clerk_auth_dep(repo_path: Path, language: str = "python") -> list[Finding]:
    """API-007: Clerk verification helper referenced."""
    CHECK_ID = "API-007"
    findings: list[Finding] = []

    if language == "python":
        src = repo_path / "src"
        if not src.is_dir():
            return findings
        has_verify_token_usage = False
        for py_file in src.rglob("*.py"):
            try:
                text = py_file.read_text()
            except Exception:
                continue
            if "verify_token" in text or "clerk" in text.lower():
                has_verify_token_usage = True
                break
        if not has_verify_token_usage:
            findings.append(
                _finding(
                    "API-007",
                    "WARN",
                    "structural_conformance",
                    "api-service (Python) has no visible Clerk or verify_token usage.",
                    "Import verify_token from common-python-utils and add "
                    "Depends(verify_token) to protected routes.",
                )
            )
    else:
        pkg = repo_path / "package.json"
        pkg_text = pkg.read_text() if pkg.exists() else ""
        if "@clerk" not in pkg_text and "common-typescript-utils" not in pkg_text:
            findings.append(
                _finding(
                    "API-007",
                    "WARN",
                    "structural_conformance",
                    "api-service (TypeScript) has no Clerk SDK or common-typescript-utils dep.",
                    "Add @clerk/clerk-sdk-node or use verifyClerkToken from "
                    "common-typescript-utils.",
                )
            )
    return findings


def check_unauthenticated_routes(
    repo_path: Path, language: str = "python"
) -> list[Finding]:
    """API-008: Unauthenticated routes must be intentional.

    A FastAPI route clears the check if any of the following hold:

      1. The function signature has a Depends(...) default (authed).
      2. The route path is on the built-in exempt list (/health,
         /metrics, /docs, /openapi.json, /redoc).
      3. The decorator's description= or summary= kwarg contains the
         phrase "intentionally public" (case-insensitive) — the route
         explicitly documents its unauthenticated intent.
      4. The handler's docstring contains "intentionally public".

    Routes that lack auth and none of the above intent markers are
    flagged.
    """
    findings: list[Finding] = []
    if language != "python":
        return findings
    import ast

    src = repo_path / "src"
    if not src.is_dir():
        return findings

    route_attrs = {"get", "post", "put", "delete", "patch"}
    exempt_paths = ("/health", "/metrics", "/docs", "/openapi.json", "/redoc")
    intent_marker = "intentionally public"

    def _kwarg_contains(dec: ast.Call, kwarg_name: str, needle: str) -> bool:
        """Return True if decorator's kwarg (string or concatenated) contains needle."""
        for kw in dec.keywords:
            if kw.arg != kwarg_name:
                continue
            # Simple string constant
            if (
                isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
                and needle in kw.value.value.lower()
            ):
                return True
            # Implicit-string-concatenation or parenthesized multi-line string:
            # FastAPI often has description=( "..." "..." ) which AST represents
            # as a single Constant in Python 3.12+, but may be a BinOp or
            # JoinedStr in other forms. Fall back to unparsing the node.
            try:
                unparsed = ast.unparse(kw.value)
                if needle in unparsed.lower():
                    return True
            except Exception:
                pass
        return False

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
            # Is this a route handler?
            route_path: str | None = None
            route_dec: ast.Call | None = None
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in route_attrs:
                    continue
                if (
                    dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)
                ):
                    route_path = dec.args[0].value
                    route_dec = dec
                    break
            if route_path is None or route_dec is None:
                continue
            if any(route_path.startswith(p) for p in exempt_paths):
                continue
            # Depends(...) default?
            has_depends = False
            for arg_default in node.args.defaults + node.args.kw_defaults:
                if arg_default is None:
                    continue
                if (
                    isinstance(arg_default, ast.Call)
                    and isinstance(arg_default.func, ast.Name)
                    and arg_default.func.id == "Depends"
                ):
                    has_depends = True
                    break
            if has_depends:
                continue
            # "intentionally public" marker in decorator description/summary?
            if _kwarg_contains(
                route_dec, "description", intent_marker
            ) or _kwarg_contains(route_dec, "summary", intent_marker):
                continue
            # "intentionally public" marker in function docstring?
            doc = ast.get_docstring(node) or ""
            if intent_marker in doc.lower():
                continue
            findings.append(
                _finding(
                    "API-008",
                    "ERROR",
                    "structural_conformance",
                    f"{rel}::{node.name}: route {route_path!r} has no Depends(...) auth.",
                    "Add Depends(verify_token) for protected routes, or document the "
                    "intentional public access in the route's decorator "
                    "description=/summary= or docstring with the phrase "
                    "'intentionally public'.",
                )
            )
    return findings


def check_auth_header_parity(repo_path: Path) -> list[Finding]:
    """AUTH-002: Auth header parity — shared library vs api.

    Evaluator-cog can't compare two repos at once. We check a lighter
    property: auth.py (if present) has a cross-reference comment to
    common-python-utils.
    """
    CHECK_ID = "AUTH-002"
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    # Find auth.py files
    auth_files = list(src.rglob("auth.py"))
    if not auth_files:
        return findings
    for auth_file in auth_files:
        try:
            text = auth_file.read_text()
        except Exception:
            continue
        if "common-python-utils" not in text and "common_python_utils" not in text:
            rel = auth_file.relative_to(repo_path)
            findings.append(
                _finding(
                    "AUTH-002",
                    "WARN",
                    "cross_repo_coherence",
                    f"{rel}: auth.py has no reference to common-python-utils.",
                    "Add a cross-reference comment noting the auth header parity with "
                    "CommonPythonApiClient.",
                )
            )
    return findings
