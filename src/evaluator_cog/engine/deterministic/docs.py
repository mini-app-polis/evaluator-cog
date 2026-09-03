"""Documentation-related structural rule checks (README, CHANGELOG, docstrings, etc)."""

from __future__ import annotations

import ast
import re
from contextlib import suppress
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)


def check_readme(repo_path: Path, monorepo_root: Path | None = None) -> list[Finding]:
    """DOC-001: README.md is mandatory."""
    CHECK_ID = "DOC-001"
    findings = []
    exists = (repo_path / "README.md").exists()
    if not exists and monorepo_root:
        exists = (monorepo_root / "README.md").exists()
    if not exists:
        findings.append(
            _finding(
                "DOC-001",
                "ERROR",
                "documentation_coverage",
                "README.md is absent.",
                "Create README.md documenting purpose, inputs, outputs, and how to run locally.",
            )
        )
    return findings


def check_changelog(
    repo_path: Path, monorepo_root: Path | None = None
) -> list[Finding]:
    """DOC-003: CHANGELOG.md required."""
    CHECK_ID = "DOC-003"
    findings = []
    exists = (repo_path / "CHANGELOG.md").exists()
    if not exists and monorepo_root:
        exists = (monorepo_root / "CHANGELOG.md").exists()
    if not exists:
        findings.append(
            _finding(
                "DOC-003",
                "WARN",
                "documentation_coverage",
                "CHANGELOG.md is absent.",
                "Create CHANGELOG.md — managed by semantic-release.",
            )
        )
    return findings


def check_env_example(
    repo_path: Path, monorepo_root: Path | None = None
) -> list[Finding]:
    """DOC-004: .env.example is required."""
    CHECK_ID = "DOC-004"
    findings = []
    # Check root first, then common monorepo locations
    candidates = [
        repo_path / ".env.example",
        repo_path / "apps" / "api" / ".env.example",
        repo_path / "apps" / "app" / ".env.example",
        repo_path / "app" / ".env.example",
        repo_path / "backend" / ".env.example",
        repo_path / "server" / ".env.example",
    ]
    if monorepo_root:
        candidates.append(monorepo_root / ".env.example")
    if not any(p.exists() for p in candidates):
        findings.append(
            _finding(
                "DOC-004",
                "WARN",
                "documentation_coverage",
                ".env.example is absent.",
                "Create .env.example documenting all required environment variables.",
            )
        )
    return findings


def check_split_package_identity(repo_path: Path) -> list[Finding]:
    """DOC-009: Split package identity documented at entry point."""
    CHECK_ID = "DOC-009"

    findings = []
    pyproject = repo_path / "pyproject.toml"
    src = repo_path / "src"
    readme = repo_path / "README.md"
    if not pyproject.exists() or not src.is_dir():
        return findings

    m = re.search(r'^\s*name\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
    if not m:
        return findings
    project_name = m.group(1)
    pkg_dirs = [d for d in src.iterdir() if d.is_dir() and d.name != "__pycache__"]
    if not pkg_dirs:
        return findings
    pkg_name = pkg_dirs[0].name
    if project_name.replace("-", "_") == pkg_name:
        return findings

    init_file = pkg_dirs[0] / "__init__.py"
    init_text = init_file.read_text().lower() if init_file.exists() else ""
    readme_text = readme.read_text().lower() if readme.exists() else ""

    if (
        project_name.lower() not in init_text
        or pkg_name.lower() not in init_text
        or project_name.lower() not in readme_text
        or pkg_name.lower() not in readme_text
    ):
        findings.append(
            _finding(
                "DOC-009",
                "WARN",
                "documentation_coverage",
                "Split package identity is not documented across __init__.py and README.",
                "Document both distribution name and import package name at the service entry points.",
            )
        )
    return findings


def check_readme_running_locally(
    repo_path: Path,
    dod_type: str | None = None,
) -> list[Finding]:
    """DOC-013: README Running locally section is complete."""
    CHECK_ID = "DOC-013"
    findings = []
    readme = repo_path / "README.md"
    if not readme.exists():
        return findings
    text = readme.read_text().lower()

    missing: list[str] = []
    if dod_type in ("new_cog", "new_fastapi_service"):
        required = ["uv sync", "pre-commit install", "pre-commit run", "uv run pytest"]
        missing.extend([r for r in required if r not in text])
        if "prereq" not in text and "python" not in text and "uv" not in text:
            missing.append("python/uv prerequisites")
    elif dod_type == "new_hono_service":
        required = ["pnpm install", "pnpm dev", "pnpm test", "node"]
        missing.extend([r for r in required if r not in text])
    elif dod_type in ("new_frontend_site", "new_react_app"):
        # A "Running locally" (or equivalent) section heading is sufficient
        # evidence that the install step is documented — it may live in a
        # monorepo root README or be implied by the section prose, so we
        # don't require an explicit `pnpm install` line when the section exists.
        _has_running_locally_section = bool(
            re.search(
                r"#+\s*(running locally|local development|developer setup|getting started|development setup)",
                text,
                re.IGNORECASE,
            )
        )
        if (
            not _has_running_locally_section
            and "pnpm install" not in text
            and "npm install" not in text
        ):
            missing.append("pnpm install or npm install")
        if "pnpm build" not in text and "npm run build" not in text:
            missing.append("pnpm build or npm run build")
        if (
            "pnpm dev" not in text
            and "npm run dev" not in text
            and "astro dev" not in text
        ):
            missing.append("pnpm dev or npm run dev or astro dev")
        if ".env.example" not in text:
            missing.append(".env.example")

    for item in missing:
        findings.append(
            _finding(
                "DOC-013",
                "WARN",
                "documentation_coverage",
                f"README Running locally is missing: {item}.",
                "Add the missing command/prerequisite to the Running locally section.",
            )
        )
    return findings


def check_adrs_present(repo_path: Path) -> list[Finding]:
    """DOC-005: ADR trail for non-trivial repos (LOC heuristic under src/)."""
    findings: list[Finding] = []
    src = repo_path / "src"
    loc = 0
    if src.is_dir():
        for p in src.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".mjs"}:
                continue
            with suppress(OSError, UnicodeDecodeError):
                loc += len(p.read_text().splitlines())
    if loc < 50:
        return findings

    dec = repo_path / "docs" / "decisions"
    if not dec.is_dir():
        findings.append(
            _finding(
                "DOC-005",
                "WARN",
                "documentation_coverage",
                "docs/decisions/ directory is missing for a non-trivial codebase.",
                "Add architecture decision records under docs/decisions/.",
            )
        )
        return findings

    if not any(dec.glob("ADR-*.md")):
        findings.append(
            _finding(
                "DOC-005",
                "WARN",
                "documentation_coverage",
                "docs/decisions/ exists but no ADR-NNN-*.md files were found.",
                "Author numbered ADR markdown files for significant decisions.",
            )
        )
    return findings


def _public_definitions(
    tree: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    """Definitions reachable from outside the module.

    ``ast.walk`` reaches every node, including functions defined inside
    other functions. A closure is not public API — it cannot be
    imported, and no consumer can call it — so requiring a docstring on
    it is noise. ``_routes.py::walk``, a four-line recursive helper
    inside ``_enclosing_function_names``, was reported as a public
    function missing documentation.

    Methods stay public: a class defined at module level exposes them.
    A class defined *inside* a function does not, so its methods are
    skipped along with everything else in that scope.
    """
    found: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []

    def visit(node: ast.AST, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not inside_function:
                    found.append(child)
                visit(child, True)
            elif isinstance(child, ast.ClassDef):
                if not inside_function:
                    found.append(child)
                # A module-level class keeps its methods public; a class
                # nested in a function carries that scope down with it.
                visit(child, inside_function)
            else:
                visit(child, inside_function)

    visit(tree, False)
    return found


def check_public_docstrings(repo_path: Path) -> list[Finding]:
    """DOC-006: Public functions/classes have docstrings."""
    CHECK_ID = "DOC-006"

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in _public_definitions(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            if node.name.startswith("_"):
                continue
            # Skip dunder methods
            if node.name.startswith("__") and node.name.endswith("__"):
                continue
            if ast.get_docstring(node):
                continue
            findings.append(
                _finding(
                    "DOC-006",
                    "WARN",
                    "documentation_coverage",
                    f"{rel}::{node.name}: public {type(node).__name__.replace('Def', '').lower()} missing docstring.",
                    "Add a docstring explaining the purpose and usage.",
                )
            )
    return findings


def check_pydantic_field_descriptions(repo_path: Path) -> list[Finding]:
    """DOC-007: Pydantic fields use Field(description=...)."""
    CHECK_ID = "DOC-007"

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

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
            base_names = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    base_names.append(b.id)
                elif isinstance(b, ast.Attribute):
                    base_names.append(b.attr)
            if "BaseModel" not in base_names:
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                if not isinstance(stmt.target, ast.Name):
                    continue
                fname = stmt.target.id
                if fname.startswith("_"):
                    continue
                # Check if value is Field(... description=...)
                has_description = False
                if (
                    stmt.value
                    and isinstance(stmt.value, ast.Call)
                    and (
                        isinstance(stmt.value.func, ast.Name)
                        and stmt.value.func.id == "Field"
                    )
                ):
                    has_description = any(
                        kw.arg == "description" for kw in stmt.value.keywords
                    )
                if not has_description:
                    findings.append(
                        _finding(
                            "DOC-007",
                            "WARN",
                            "documentation_coverage",
                            f"{rel}::{node.name}.{fname}: Pydantic field missing Field(description=...).",
                            "Wrap the field with Field(description='...') for OpenAPI docs.",
                        )
                    )
    return findings


def check_fastapi_route_docs(repo_path: Path) -> list[Finding]:
    """DOC-010: FastAPI route decorators have summary=, description=, response_model=."""
    CHECK_ID = "DOC-010"

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    route_attrs = {"get", "post", "put", "delete", "patch"}
    required = ("summary", "description", "response_model")

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
                kwargs = {kw.arg for kw in dec.keywords}
                missing = [r for r in required if r not in kwargs]
                if missing:
                    findings.append(
                        _finding(
                            "DOC-010",
                            "ERROR",
                            "documentation_coverage",
                            f"{rel}::{node.name}: route decorator missing: {', '.join(missing)}.",
                            "Add all three (summary, description, response_model) to the "
                            "route decorator for complete OpenAPI docs.",
                        )
                    )
    return findings


def check_unauthenticated_routes_documented(repo_path: Path) -> list[Finding]:
    """DOC-011: Unauthenticated routes document their intent."""
    CHECK_ID = "DOC-011"

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    route_attrs = {"get", "post", "put", "delete", "patch"}
    exempt_paths = ("/health", "/metrics", "/docs", "/openapi.json", "/redoc")
    public_markers = (
        "intentionally public",
        "no auth required",
        "read-only public",
        "public endpoint",
    )

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
            route_path: str | None = None
            description: str | None = None
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
                for kw in dec.keywords:
                    if (
                        kw.arg == "description"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        description = kw.value.value
            if route_path is None:
                continue
            if any(route_path.startswith(p) for p in exempt_paths):
                continue
            # Has auth?
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
            # No auth — must have public-intent marker in description or docstring
            ds = ast.get_docstring(node) or ""
            combined = (description or "") + " " + ds
            if not any(marker in combined.lower() for marker in public_markers):
                findings.append(
                    _finding(
                        "DOC-011",
                        "WARN",
                        "documentation_coverage",
                        f"{rel}::{node.name}: unauthenticated route {route_path!r} lacks public-intent marker.",
                        "Add 'intentionally public' or 'no auth required' to the route "
                        "description or docstring.",
                    )
                )
    return findings
