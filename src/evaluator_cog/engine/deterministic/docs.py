"""Documentation-related structural rule checks (README, CHANGELOG, docstrings, etc)."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from contextlib import suppress
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)
from evaluator_cog.engine.deterministic.config import _settings_declared_fields


def check_readme(repo_path: Path) -> list[Finding]:
    """DOC-001: README.md is mandatory."""
    CHECK_ID = "DOC-001"
    findings = []
    exists = (repo_path / "README.md").exists()
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


def check_changelog(repo_path: Path) -> list[Finding]:
    """DOC-003: CHANGELOG.md required."""
    CHECK_ID = "DOC-003"
    findings = []
    exists = (repo_path / "CHANGELOG.md").exists()
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


#: Folders never scanned for environment reads: dependencies, build output,
#: caches and tests (DOC-004's check_notes).
_ENV_SCAN_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".astro",
        ".vercel",
        ".next",
        ".output",
        "coverage",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "tests",
        "test",
        "__tests__",
    }
)
_ENV_SCAN_SUFFIXES = frozenset(
    {".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".astro"}
)
#: A test file outside a tests folder: test_x.py, x_test.py, x.test.ts, x.spec.ts.
_ENV_TEST_FILE_RE = re.compile(r"(^test_.*\.py$|_test\.py$|\.(test|spec)\.[a-z]+$)")

_ENV_NAME = r"([A-Z][A-Z0-9_]*)"
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_JS_ENV_READ_RES = (
    re.compile(r"\b(?:import\.meta\.env|process\.env)\." + _ENV_NAME + r"\b"),
    re.compile(
        r"\b(?:import\.meta\.env|process\.env)\[\s*[\"'`]" + _ENV_NAME + r"[\"'`]\s*\]"
    ),
)
#: Vite's own import.meta.env fields, which Astro also sets. They are the
#: framework's contract, never configuration a repository supplies.
_VITE_BUILTIN_ENV = frozenset(
    {"MODE", "BASE_URL", "PROD", "DEV", "SSR", "SITE", "ASSETS_PREFIX"}
)

#: A name in .env.example, set or commented out: ``NAME=`` or ``# NAME=``.
_ENV_EXAMPLE_NAME_RE = re.compile(r"^\s*#?\s*" + _ENV_NAME + r"\s*=")
_EXTERNAL_TOOLING_RE = re.compile(r"^\s*#\s*external tooling\b", re.IGNORECASE)


def _env_scan_files(root: Path) -> list[Path]:
    """Source files under ``root`` that DOC-004 reads for environment reads."""
    found: list[Path] = []
    for path in root.rglob("*"):
        rel_parts = path.relative_to(root).parts
        if any(part in _ENV_SCAN_SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if not path.is_file() or path.suffix not in _ENV_SCAN_SUFFIXES:
            continue
        if _ENV_TEST_FILE_RE.search(path.name):
            continue
        found.append(path)
    return sorted(found)


def _is_env_reader(func: ast.expr) -> bool:
    """``getenv``, ``os.getenv``, ``environ.get`` or ``os.environ.get``."""
    if isinstance(func, ast.Name):
        return func.id == "getenv"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "getenv":
        return True
    target = func.value
    return func.attr == "get" and (
        (isinstance(target, ast.Name) and target.id == "environ")
        or (isinstance(target, ast.Attribute) and target.attr == "environ")
    )


def _is_environ(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "environ") or (
        isinstance(node, ast.Attribute) and node.attr == "environ"
    )


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_env_constants(tree: ast.AST) -> dict[str, frozenset[str]]:
    """Module-level names bound to an environment variable name, or to several.

    ``FLAG_VAR = "DRY_RUN"`` binds one name; ``TOKEN_VARS = {"prd":
    "TOKEN_PRD", "dev": "TOKEN_DEV"}`` binds every value of a dict whose
    values are all such literals, for a read indexed into it
    (``os.getenv(TOKEN_VARS[config])``). Anything else is left alone: a name
    built at runtime is not collected.
    """
    found: dict[str, frozenset[str]] = {}
    if not isinstance(tree, ast.Module):
        return found
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        literals = value.values if isinstance(value, ast.Dict) else [value]
        names = [
            v.value
            for v in literals
            if isinstance(v, ast.Constant)
            and isinstance(v.value, str)
            and _ENV_NAME_RE.match(v.value)
        ]
        if names and len(names) == len(literals):
            found[target.id] = frozenset(names)
    return found


class _EnvReadVisitor(ast.NodeVisitor):
    """One pass over a module: literal reads, and functions that wrap a read.

    A wrapper is a function that passes one of its own parameters to an
    environment read, or to another wrapper — ``_require(name)`` around
    ``os.getenv(name)``. A literal passed to a wrapper in that position is a
    read, the same as a literal passed to ``os.getenv``, and so is a
    module-level constant naming one (``os.getenv(FLAG_VAR)``) or a lookup in
    a module-level dict of them (``_require(TOKEN_VARS[config])``).
    """

    def __init__(
        self,
        wrappers: dict[str, int],
        constants: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self.wrappers = wrappers
        self.constants = constants or {}
        self.reads: set[str] = set()
        self.new_wrappers: dict[str, int] = {}
        self._stack: list[tuple[str, list[str]]] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        params = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
        self._stack.append((node.name, params))
        self.generic_visit(node)
        self._stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def _record(self, arg: ast.expr) -> None:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if _ENV_NAME_RE.match(arg.value):
                self.reads.add(arg.value)
        elif isinstance(arg, ast.Name) and self._stack and arg.id in self._stack[-1][1]:
            name, params = self._stack[-1]
            index = params.index(arg.id)
            # A method's call site omits self or cls.
            if params[0] in ("self", "cls"):
                index -= 1
            self.new_wrappers.setdefault(name, index)
        elif isinstance(arg, ast.Name) and arg.id in self.constants:
            self.reads.update(self.constants[arg.id])
        elif (
            isinstance(arg, ast.Subscript)
            and isinstance(arg.value, ast.Name)
            and arg.value.id in self.constants
        ):
            self.reads.update(self.constants[arg.value.id])

    def visit_Call(self, node: ast.Call) -> None:
        if _is_env_reader(node.func) and node.args:
            self._record(node.args[0])
        else:
            called = _call_name(node.func)
            index = self.wrappers.get(called or "")
            if index is not None and 0 <= index < len(node.args):
                self._record(node.args[index])
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_environ(node.value) and isinstance(node.ctx, ast.Load):
            self._record(node.slice)
        self.generic_visit(node)


def _python_env_reads(trees: dict[Path, ast.AST]) -> dict[str, Path]:
    """Names read in Python, directly or through the repository's wrappers."""
    wrappers: dict[str, int] = {}
    while True:
        reads: dict[str, Path] = {}
        found: dict[str, int] = {}
        for path, tree in trees.items():
            visitor = _EnvReadVisitor(wrappers, _module_env_constants(tree))
            visitor.visit(tree)
            for name in sorted(visitor.reads):
                reads.setdefault(name, path)
            for name, index in visitor.new_wrappers.items():
                found.setdefault(name, index)
        if all(wrappers.get(k) == v for k, v in found.items()):
            return reads
        wrappers.update(found)


#: A function taking a parameter: ``function name(param`` or
#: ``const name = (param … ) =>``.
_JS_FUNCTION_RE = re.compile(
    r"function\s+(\w+)\s*\(\s*(\w+)"
    r"|(?:const|let)\s+(\w+)\s*=\s*\(\s*(\w+)[^)]*\)\s*(?::[^=]*)?=>"
)
#: Vite's ``define`` of an ``import.meta.env`` field: a build constant the
#: config computes, not a variable the environment supplies.
_VITE_DEFINE_RE = re.compile(r"[\"']import\.meta\.env\." + _ENV_NAME + r"[\"']\s*:")
_LOAD_ENV_VAR_RE = re.compile(r"(?:const|let)\s+(\w+)\s*=\s*loadEnv\(")
_LOAD_ENV_DIRECT_RE = re.compile(r"loadEnv\([^)]*\)\." + _ENV_NAME + r"\b")


def _script_env_reads(scripts: dict[Path, str]) -> dict[str, Path]:
    """Names read in TypeScript, JavaScript and Astro.

    Direct reads of import.meta.env and process.env; reads through a
    function of the repository's own that indexes the environment with its
    parameter (``required("E2E_BASE_URL")`` around ``process.env[name]``);
    and Vite's ``loadEnv`` results. A name a Vite config ``define``s is a
    build constant, so it is not a read even where the app reads it.
    """
    reads: dict[str, Path] = {}
    defined: set[str] = set()
    wrappers: set[str] = set()
    for text in scripts.values():
        defined.update(_VITE_DEFINE_RE.findall(text))
        for match in _JS_FUNCTION_RE.finditer(text):
            name = match.group(1) or match.group(3)
            param = match.group(2) or match.group(4)
            end = text.find("\n}", match.end())
            body = text[match.end() : end if end != -1 else match.end() + 600]
            if re.search(r"env\[\s*" + re.escape(param) + r"\s*\]", body):
                wrappers.add(name)
    for path, text in scripts.items():
        found: list[str] = []
        for pattern in _JS_ENV_READ_RES:
            found.extend(pattern.findall(text))
        found.extend(_LOAD_ENV_DIRECT_RE.findall(text))
        for var in _LOAD_ENV_VAR_RE.findall(text):
            found.extend(
                re.findall(r"\b" + re.escape(var) + r"\." + _ENV_NAME + r"\b", text)
            )
        for wrapper in wrappers:
            found.extend(
                re.findall(
                    r"\b" + re.escape(wrapper) + r"\(\s*[\"'`]" + _ENV_NAME + r"[\"'`]",
                    text,
                )
            )
        for name in found:
            if name not in _VITE_BUILTIN_ENV and name not in defined:
                reads.setdefault(name, path)
    return reads


def _env_reads(root: Path) -> dict[str, Path]:
    """Every environment variable read under ``root``, with a file reading it.

    Python: os.environ[...], os.environ.get(...), os.getenv(...), and every
    field of a Settings class under src/ (a pydantic-settings field is read
    from the variable of its name). TypeScript, JavaScript and Astro:
    import.meta.env and process.env, by dot or literal bracket access. A
    name built at runtime is not collected.
    """
    reads: dict[str, Path] = {}
    trees: dict[Path, ast.AST] = {}
    scripts: dict[Path, str] = {}
    for path in _env_scan_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.suffix == ".py":
            with suppress(SyntaxError, ValueError):
                trees[path] = ast.parse(text)
        else:
            scripts[path] = text
    for name, path in _script_env_reads(scripts).items():
        reads.setdefault(name, path)
    for name, path in _python_env_reads(trees).items():
        reads.setdefault(name, path)
    src = root / "src"
    if src.is_dir():
        for field in _settings_declared_fields(
            src, include_computed=False, include_plain_assign=False
        ):
            reads.setdefault(field.upper(), src)
    return reads


def _env_example_names(text: str) -> tuple[list[str], set[str]]:
    """Names .env.example lists, in order, and those marked external tooling.

    A comment line starting ``# external tooling`` marks every name after it
    up to the next blank line, so one explanation can head a block of names.
    """
    listed: list[str] = []
    external: set[str] = set()
    marked = False
    for line in text.splitlines():
        if not line.strip():
            marked = False
            continue
        if _EXTERNAL_TOOLING_RE.match(line):
            marked = True
            continue
        match = _ENV_EXAMPLE_NAME_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name not in listed:
            listed.append(name)
        if marked:
            external.add(name)
    return listed, external


def check_env_example(repo_path: Path) -> list[Finding]:
    """DOC-004: .env.example exists and lists exactly the variables read."""
    CHECK_ID = "DOC-004"
    findings = []
    # Check root first, then common service subdirectories
    candidates = [
        repo_path / ".env.example",
        repo_path / "app" / ".env.example",
        repo_path / "backend" / ".env.example",
        repo_path / "server" / ".env.example",
    ]
    env_example = next((p for p in candidates if p.exists()), None)
    if env_example is None:
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

    root = env_example.parent
    try:
        listed, external = _env_example_names(env_example.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return findings
    reads = _env_reads(root)
    rel_env = env_example.relative_to(repo_path)

    for name in sorted(set(reads) - set(listed)):
        findings.append(
            _finding(
                "DOC-004",
                "WARN",
                "documentation_coverage",
                f"{reads[name].relative_to(repo_path)} reads {name}, "
                f"which {rel_env} does not list.",
                f"Add {name} to {rel_env}: uncommented if the code has no "
                "default, commented if it has one.",
            )
        )
    for name in listed:
        if name in reads or name in external:
            continue
        findings.append(
            _finding(
                "DOC-004",
                "WARN",
                "documentation_coverage",
                f"{rel_env} lists {name}, which nothing in the repository reads.",
                f"Remove {name}, or head its block with a '# external tooling' "
                "comment saying what reads it.",
            )
        )
    return findings


def check_split_package_identity(repo_path: Path) -> list[Finding]:
    """DOC-009: Split package identity documented at entry point."""
    CHECK_ID = "DOC-009"

    findings: list[Finding] = []
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
    # Normalised as PEP 503 does, with `_` as the separator PY-011 expects:
    # `My.Service` and `my-service` both import as `my_service`.
    if re.sub(r"[-_.]+", "_", project_name).lower() == pkg_name:
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
    findings: list[Finding] = []
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
        # evidence that the install step is documented — it may be implied
        # by the section prose, so we
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
                "docs/decisions/ exists but no ADR-NNNN-*.md files were found.",
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
    skipped along with everything else in that scope. Nor does a private
    class (``_EnvReadVisitor``): its methods are reachable only through a
    name the module marks as its own, so ``visit_Call`` on an
    ``ast.NodeVisitor`` subclass is not public API.
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
                # A public module-level class keeps its methods public; a
                # private class, or one nested in a function, hides them.
                visit(child, inside_function or child.name.startswith("_"))
            else:
                visit(child, inside_function)

    visit(tree, False)
    return found


#: DOC-006, TypeScript: an exported function or class declaration, or an
#: exported const bound to an arrow function or function expression.
_TS_EXPORT = re.compile(
    r"^[ \t]*export\s+(?:default\s+)?(?:"
    r"(?:async\s+)?function\b\*?\s*(?P<fn>[\w$]+)?"
    r"|(?:abstract\s+)?class\b\s*(?P<cls>[\w$]+)?"
    r"|const\s+(?P<const>[\w$]+)\s*(?::[^=\n]+)?=\s*(?:async\s+)?"
    r"(?:function\b|<[^>\n]*>\s*\(|\([^)]*\)\s*(?::[^=\n]+)?=>|[\w$]+\s*=>)"
    r")",
    re.MULTILINE,
)


def _preceded_by_doc_comment(text: str, offset: int) -> bool:
    """Whether the nearest non-blank line before `offset` closes a ``/** */``."""
    before = text[:offset].rstrip()
    if not before.endswith("*/"):
        return False
    start = before.rfind("/*")
    return start != -1 and before.startswith("/**", start)


def _public_docstrings_typescript(repo_path: Path) -> list[Finding]:
    """DOC-006, TypeScript: exports from ``src/**/*.ts`` carry a ``/** */``.

    Test files and ``*.d.ts`` are not read, nor are ``.tsx`` files: a React
    component's name and props type say what it renders.
    """
    from evaluator_cog.engine.deterministic import _typescript as ts

    findings: list[Finding] = []
    for path in ts.source_files(repo_path, (".ts",)):
        text = ts.read(path)
        rel = path.relative_to(repo_path)
        for m in _TS_EXPORT.finditer(text):
            if _preceded_by_doc_comment(text, m.start()):
                continue
            kind = "class" if re.search(r"\bclass\b", m.group(0)) else "function"
            name = m.group("fn") or m.group("cls") or m.group("const") or "default"
            findings.append(
                _finding(
                    "DOC-006",
                    "WARN",
                    "documentation_coverage",
                    f"{rel}::{name}: exported {kind} missing a /** */ doc comment.",
                    "Add a /** */ comment saying what it does.",
                    file=rel,
                )
            )
    return findings


def check_public_docstrings(repo_path: Path, language: str = "python") -> list[Finding]:
    """DOC-006: Public functions/classes have docstrings.

    Python: every public function and class. TypeScript: every export from
    a ``.ts`` module.
    """
    CHECK_ID = "DOC-006"

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    if language == "typescript":
        return _public_docstrings_typescript(repo_path)

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


# ── DOC-016 / DOC-017: code that stays in its own repo and its own time ─────

#: The source files DOC-016 and DOC-017 read, by suffix. Code and tests only:
#: README, docs/ and CHANGELOG.md are documents, which may describe history
#: and how the repo fits with others.
_CODE_SUFFIXES = frozenset(
    {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".astro", ".sh"}
)
#: The folders they are read from.
_CODE_DIRS = ("src", "tests")
#: Dependencies, build output and caches inside those folders.
_CODE_SKIP_DIRS = _ENV_SCAN_SKIP_DIRS - {"tests", "test", "__tests__"}


def _code_files(repo_path: Path) -> list[Path]:
    """Source and test files under src/ and tests/, skipping dependencies."""
    files: list[Path] = []
    for top in _CODE_DIRS:
        root = repo_path / top
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in _CODE_SUFFIXES:
                continue
            # Below its top folder: tests/ is read here, unlike DOC-004.
            parts = set(path.relative_to(root).parts[:-1])
            if parts & _CODE_SKIP_DIRS:
                continue
            files.append(path)
    return files


#: DOC-016's deny patterns: another repo's internals, or a private tracker.
_TRACKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a task-board id", re.compile(r"\bMINI-\d+\b")),
    ("a task-board link", re.compile(r"app\.asana\.com/0/\d")),
)
#: A Terraform file name. Only infra owns Terraform.
_TF_FILE = re.compile(r"(?<![\w./-])[\w-]+\.tf(?![\w.(])")
#: A migration by number or file name. Only the API repos own migrations.
_MIGRATION = (
    re.compile(r"\bmigrations? \d{3}\b", re.IGNORECASE),
    re.compile(r"(?<![\w])\d{3}_[a-z0-9_]+\.sql\b"),
)
#: The repo types that own what the patterns above name.
_TERRAFORM_OWNERS = frozenset({"infrastructure"})
_MIGRATION_OWNERS = frozenset({"api-service"})


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ── The catalog's `retired` registry, as DOC-014, DOC-016 and DOC-017 read it ─


def _retired_term(term: str) -> str:
    """A retired name as a regex: case-insensitive (the caller's flag),
    bounded on both sides by anything that is not a letter or digit."""
    return rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"


def _retired_terms(retired: dict | None) -> list[str]:
    """The registry's keys, longest first so an overlap reports the fuller name."""
    if not isinstance(retired, dict):
        return []
    return sorted(
        (str(k).strip() for k in retired if str(k).strip()),
        key=lambda t: (-len(t), t),
    )


#: DOC-016: what separates tokens on a line, and a token that is a path or
#: file name — one containing `/`, or ending in a source or document extension.
_TOKEN_SEP = re.compile(r"[\s()\[\]{}<>\"'`,;]+")
_PATH_LIKE = re.compile(r"/|\.(?:py|ts|tsx|js|jsx|sql|md)$", re.IGNORECASE)


def _retired_file_hits(text: str, terms: list[str]) -> list[tuple[int, str, str]]:
    """DOC-016: a retired name followed on the same line by a path or file name.

    Returns (line, matched text, term), the text running from the name to
    the end of the path.
    """
    hits: list[tuple[int, str, str]] = []
    for term in terms:
        for m in re.finditer(_retired_term(term), text, re.IGNORECASE):
            line_end = text.find("\n", m.end())
            rest = text[m.end() : line_end if line_end != -1 else len(text)]
            offset = 0
            for piece in _TOKEN_SEP.split(rest):
                found = rest.find(piece, offset) if piece else offset
                offset = found + len(piece)
                # A comment marker (`*/`, `//`) holds a slash but names nothing.
                if _PATH_LIKE.search(piece) and re.search(r"[A-Za-z0-9]", piece):
                    hits.append(
                        (_line_of(text, m.start()), m.group(0) + rest[:offset], term)
                    )
                    break
    return hits


def _retired_comparison_pattern(term: str) -> re.Pattern[str]:
    """DOC-017: the code measured against a retired component."""
    t = _retired_term(term)
    return re.compile(
        rf"\b(?:same as|as in|as|unlike|like|matching|from|replaces|replaced)\s+{t}"
        rf"|{t}['’]s\b",
        re.IGNORECASE,
    )


def _retired_comparison_hits(
    comments: list[tuple[int, str]], terms: list[str]
) -> list[tuple[int, str, str]]:
    """DOC-017's comparisons in `comments`: (line, matched text, term)."""
    hits: list[tuple[int, str, str]] = []
    for term in terms:
        pattern = _retired_comparison_pattern(term)
        for start, comment in comments:
            for m in pattern.finditer(comment):
                hits.append(
                    (start + comment.count("\n", 0, m.start()), m.group(0), term)
                )
    return hits


def check_doc_016(
    repo_path: Path, *, repo_type: str = "", retired: dict | None = None
) -> list[Finding]:
    """DOC-016: code and tests name another repo's public contract, never its internals.

    `retired` is the catalog's registry of retired components: citing one's
    files is a finding in every repo.
    """
    CHECK_ID = "DOC-016"
    terms = _retired_terms(retired)

    patterns: list[tuple[str, re.Pattern[str]]] = list(_TRACKER_PATTERNS)
    if repo_type not in _TERRAFORM_OWNERS:
        patterns.append(("a Terraform file", _TF_FILE))
    if repo_type not in _MIGRATION_OWNERS:
        patterns.extend(("a migration", p) for p in _MIGRATION)

    findings: list[Finding] = []
    for path in _code_files(repo_path):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = sorted(
            [
                (_line_of(text, m.start()), m.group(0), what)
                for what, pattern in patterns
                for m in pattern.finditer(text)
            ]
            + [
                (n, match, f"a file of retired {term}")
                for n, match, term in _retired_file_hits(text, terms)
            ]
        )
        if not hits:
            continue
        rel = path.relative_to(repo_path)
        listed = "; ".join(f"line {n}: {match} ({what})" for n, match, what in hits)
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                "documentation_coverage",
                f"{rel} names what another repo or a private tracker keeps to "
                f"itself: {listed}.",
                "Name the other repo, or link its public contract (an API "
                "route, its OpenAPI document), instead of its files, "
                "migrations or board tasks.",
                file=rel,
            )
        )
    return findings


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
#: DOC-017's phrases: a comment telling the code's history.
_HISTORY_PHRASES = re.compile(
    r"\b(?:previously|used to|no longer|was changed|was renamed|moved from"
    r"|originally|ported from|carried over"
    r"|as of (?:\d{4}-\d{2}(?:-\d{2})?|\d{1,2}/\d{1,2}/\d{2,4}"
    rf"|{_MONTH} \d{{1,2}}(?:, \d{{4}})?|\d{{1,2}} {_MONTH} \d{{4}}|{_MONTH} \d{{4}}))\b",
    re.IGNORECASE,
)
#: Present-tense uses of those phrases, which describe the code as it is.
#: Each is the text right before the phrase (anchored at its end) or right
#: after it (anchored at its start); comment markers between words count
#: as spaces, so a docstring or block comment wrapped across lines reads
#: the same.
_GAP = r"[\s#*/]+"
_PRESENT_BEFORE: dict[str, re.Pattern[str]] = {
    # "is used to sign", "it's used to".
    "used to": re.compile(
        rf"(?:\b(?:is|are|was|were|be|been|being)|\w['’]s){_GAP}$", re.IGNORECASE
    ),
    # "a task that no longer passes", "a page that no longer exists".
    "no longer": re.compile(
        rf"\b(?:that|which|who|whose|if|when|once|until|while){_GAP}$",
        re.IGNORECASE,
    ),
    # "is not moved from".
    "moved from": re.compile(rf"\b(?:not|is|are|be|been|being){_GAP}$", re.IGNORECASE),
    # "the previously recorded value": after a determiner, the participle
    # that follows is an adjective. With no determiner before it, the
    # phrase reads as history and stays a finding.
    "previously": re.compile(
        r"\b(?:a|an|the|any|each|every|no|some|all|both|this|these|those"
        rf"|its|their|his|her|our|your|my){_GAP}$",
        re.IGNORECASE,
    ),
}
_PRESENT_AFTER: dict[str, re.Pattern[str]] = {
    # The participle the determiner above introduces: -ed or -en.
    "previously": re.compile(
        rf"{_GAP}(?!(?:when|then|even|often)\b)[a-z]{{2,}}(?:ed|en)\b",
        re.IGNORECASE,
    ),
    # "no longer than 72 characters": a length, not a change.
    "no longer": re.compile(r"\s+than\b", re.IGNORECASE),
}


def _is_present_tense(comment: str, match: re.Match[str]) -> bool:
    """True when a DOC-017 phrase is used in the present tense, not as history."""
    phrase = match.group(0).lower()
    before = _PRESENT_BEFORE.get(phrase)
    after = _PRESENT_AFTER.get(phrase)
    is_before = before is not None and before.search(comment, 0, match.start())
    is_after = after is not None and after.match(comment, match.end())
    if phrase == "previously":
        # Both: a determiner before it and a participle after it.
        return bool(is_before and is_after)
    return bool(is_before or is_after)


_JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_HASH_COMMENT = re.compile(r"(?m)(?:^|\s)#[^\n]*")


def _python_comments(text: str) -> list[tuple[int, str]]:
    """Each comment and docstring in Python source, with its first line."""
    out: list[tuple[int, str]] = []
    with suppress(tokenize.TokenError, IndentationError, SyntaxError):
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string))
    with suppress(SyntaxError, ValueError):
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.append((body[0].lineno, body[0].value.value))
    return out


def _other_comments(path: Path, text: str) -> list[tuple[int, str]]:
    """Comments in TypeScript, JavaScript, Astro and shell source."""
    pattern = _HASH_COMMENT if path.suffix == ".sh" else _JS_COMMENT
    return [(_line_of(text, m.start()), m.group(0)) for m in pattern.finditer(text)]


def _comments_of(path: Path, text: str) -> list[tuple[int, str]]:
    """Comments and docstrings in a source file, each with its first line."""
    return (
        _python_comments(text) if path.suffix == ".py" else _other_comments(path, text)
    )


def check_doc_017(repo_path: Path, *, retired: dict | None = None) -> list[Finding]:
    """DOC-017: code comments describe the code as it is, not its history.

    `retired` is the catalog's registry of retired components: a comment
    measuring the code against one is history too.
    """
    CHECK_ID = "DOC-017"
    terms = _retired_terms(retired)

    findings: list[Finding] = []
    for path in _code_files(repo_path):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        comments = _comments_of(path, text)
        hits = sorted(
            [
                (start + comment.count("\n", 0, m.start()), m.group(0))
                for start, comment in comments
                for m in _HISTORY_PHRASES.finditer(comment)
                if not _is_present_tense(comment, m)
            ]
            + [(n, match) for n, match, _t in _retired_comparison_hits(comments, terms)]
        )
        if not hits:
            continue
        rel = path.relative_to(repo_path)
        listed = "; ".join(f'line {n}: "{phrase}"' for n, phrase in hits)
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                "documentation_coverage",
                f"{rel} has comments telling the code's history: {listed}.",
                "Say what the code does now; put what changed, and when, in an "
                "ADR or the CHANGELOG.",
                file=rel,
            )
        )
    return findings


# ── DOC-014: living docs do not describe a retired component as current ─────

#: Words that make a sentence about the retirement rather than a description
#: of the present.
_PROVENANCE = re.compile(
    r"retire|replaced|removed|superseded|deprecated|formerly|previously"
    r"|no longer|used to|moved off|migrated from|merged from|instead of"
    r"|in place of|legacy|historical",
    re.IGNORECASE,
)
#: A sentence ends at `.`, `?` or `!` before whitespace, or at a blank line.
_SENTENCE_END = re.compile(r"[.?!](?=\s|$)|\n[ \t]*\n")
_HISTORICAL_MARKER = "<!-- historical"


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence of `text` holding the span start..end."""
    left = 0
    for boundary in _SENTENCE_END.finditer(text, 0, start):
        left = boundary.end()
    right = _SENTENCE_END.search(text, end)
    return text[left : right.start() if right else len(text)]


def _is_historical_doc(text: str) -> bool:
    for line in text.splitlines():
        if line.strip():
            return line.lstrip().startswith(_HISTORICAL_MARKER)
    return False


def _doc014_texts(
    repo_path: Path,
) -> list[tuple[Path, bool, list[tuple[int, str, bool]]]]:
    """Each living-doc file, whether it is source, and its texts as
    (first line, text, is_key_line).

    README.md, docs/ (not docs/decisions/ or a file marked historical),
    .env.example line by line, and the comments and docstrings of source
    under src/. CHANGELOG.md and tests/ are history and fixtures.
    """
    out: list[tuple[Path, bool, list[tuple[int, str, bool]]]] = []

    def _read(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    readme = repo_path / "README.md"
    if readme.is_file() and (text := _read(readme)) is not None:
        out.append((readme, False, [(1, text, False)]))

    docs = repo_path / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*")):
            if not path.is_file():
                continue
            if path.relative_to(docs).parts[0] == "decisions":
                continue
            text = _read(path)
            if text is None or _is_historical_doc(text):
                continue
            out.append((path, False, [(1, text, False)]))

    env = repo_path / ".env.example"
    if env.is_file() and (text := _read(env)) is not None:
        lines = [
            (n, line, not line.lstrip().startswith("#"))
            for n, line in enumerate(text.splitlines(), start=1)
        ]
        out.append((env, False, lines))

    for path in _code_files(repo_path):
        if path.relative_to(repo_path).parts[0] != "src":
            continue
        text = _read(path)
        if text is None:
            continue
        out.append(
            (path, True, [(start, c, False) for start, c in _comments_of(path, text)])
        )
    return out


def _replaced_by(retired: dict | None, term: str) -> str:
    entry = (retired or {}).get(term)
    return (
        str(entry.get("replaced_by") or "").strip() if isinstance(entry, dict) else ""
    )


def check_doc_014(repo_path: Path, *, retired: dict | None = None) -> list[Finding]:
    """DOC-014: living docs do not describe a retired component as current.

    `retired` is the catalog's registry of retired components, the rule's
    only list of terms. Without it there is nothing to look for.
    """
    CHECK_ID = "DOC-014"
    terms = _retired_terms(retired)
    if not terms:
        return []
    patterns = [(t, re.compile(_retired_term(t), re.IGNORECASE)) for t in terms]

    findings: list[Finding] = []
    for path, is_source, texts in _doc014_texts(repo_path):
        # A line DOC-016 or DOC-017 reports for the same term is left to it.
        left_to_others: set[tuple[int, str]] = set()
        if is_source:
            with suppress(OSError, UnicodeDecodeError):
                source = path.read_text(encoding="utf-8")
                left_to_others = {
                    (n, t) for n, _m, t in _retired_file_hits(source, terms)
                } | {
                    (n, t)
                    for n, _m, t in _retired_comparison_hits(
                        _comments_of(path, source), terms
                    )
                }

        hits: list[tuple[int, str]] = []
        for start, text, is_key_line in texts:
            claimed: list[tuple[int, int]] = []
            for term, pattern in patterns:
                for m in pattern.finditer(text):
                    # Longest term first: a shorter one inside it is the same mention.
                    if any(a <= m.start() and m.end() <= b for a, b in claimed):
                        continue
                    claimed.append((m.start(), m.end()))
                    line = start + text.count("\n", 0, m.start())
                    if (line, term) in left_to_others:
                        continue
                    if not is_key_line and _PROVENANCE.search(
                        _sentence_around(text, m.start(), m.end())
                    ):
                        continue
                    hits.append((line, term))
        if not hits:
            continue
        rel = path.relative_to(repo_path)
        listed = "; ".join(
            f"line {n}: {term} (replaced by: {_replaced_by(retired, term)})"
            for n, term in sorted(set(hits))
        )
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                "documentation_coverage",
                f"{rel} describes a retired component as current: {listed}.",
                "Describe what does the job now, or move the history to an ADR "
                "or the CHANGELOG.",
                file=rel,
            )
        )
    return findings
