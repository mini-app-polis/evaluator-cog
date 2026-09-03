"""Packaging, entry-point and lockfile-discipline rule checks (CD-016, CD-020).

Both rules here police the seam between what a repository *declares* and
what actually runs or ships. CD-016 is about the process the platform
starts: a Prefect ``serve()`` loop that registers deployments at boot and
dies silently when Prefect Cloud is briefly unreachable, unless it is
wrapped in the shared ``serve_with_retry`` helper. CD-020 is about the
lockfile: a release that bumps the version in ``pyproject.toml`` without
relocking leaves ``uv.lock`` naming the previous version, and every
subsequent install resolves against a stale graph.

Neither check raises. Every file read, parse and subprocess call is
guarded, and an unreadable or unparseable input degrades to "cannot
confirm" rather than to a traceback, because these functions run inside a
batch conformance sweep where one bad repository must not stop the run.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
    _is_checker_self_source,
)

_DIMENSION = "cd_readiness"

# The shared resilience helper CD-016 requires, and the module it must be
# imported from. A locally-defined shim with the same name does not
# satisfy the rule: the point of the standard is that every cog inherits
# the same backoff policy from one place.
_SERVE_HELPER = "serve_with_retry"
_SERVE_HELPER_MODULE = "mini_app_polis.serve_resilience"

# `uv lock --check` resolves against the configured indexes and can block
# on a slow or unreachable network. The conformance sweep is a batch job,
# so the call is bounded and a breach of the bound is treated as "could
# not determine", never as a violation.
_UV_LOCK_CHECK_TIMEOUT_S = 60.0

# Directories that never hold the deployed entry point and whose contents
# would make the CD-016 applicability gate fire on fixtures rather than on
# production code.
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "build",
        "dist",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "tests",
        "test",
    }
)


# --- shared local helpers ----------------------------------------------------


def _rel(path: Path, repo_path: Path) -> str:
    """Repo-relative display path, falling back to the absolute path."""
    try:
        return str(path.relative_to(repo_path))
    except ValueError:
        return str(path)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _load_json(path: Path) -> tuple[Any | None, str | None]:
    text = _read_text(path)
    if text is None:
        return None, "unreadable"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def _load_toml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    text = _read_text(path)
    if text is None:
        return None, "unreadable"
    try:
        return tomllib.loads(text), None
    except tomllib.TOMLDecodeError as exc:
        return None, str(exc)


def _iter_python_sources(repo_path: Path) -> Iterator[Path]:
    """Yield the repository's production Python files.

    ``src/`` is authoritative when it exists — that is the layout PY-005
    mandates — plus any top-level scripts beside it, since a few cogs keep
    a thin ``main.py`` at the root. When there is no ``src/`` the whole
    tree is walked with vendor, cache and test directories pruned.

    Tests are deliberately excluded. A test fixture that constructs a
    ``serve()`` call is not a serve entry point, and letting one satisfy
    the CD-016 applicability gate would resurrect exactly the false ERROR
    the gate exists to prevent.
    """
    roots: list[Path] = []
    src = repo_path / "src"
    if src.is_dir():
        roots.append(src)
        for child in repo_path.iterdir():
            if child.is_file() and child.suffix == ".py":
                yield child
    else:
        roots.append(repo_path)

    for root in roots:
        if not root.is_dir():
            continue
        for py in sorted(root.rglob("*.py")):
            parts = set(py.relative_to(root).parts[:-1])
            if parts & _SKIP_DIRS or any(p.startswith(".") for p in parts):
                continue
            if _is_checker_self_source(py):
                # evaluator-cog scanning itself: the deterministic
                # checkers name these call shapes in their own detection
                # logic, and a self-scan would report the detector.
                continue
            yield py


def _parse_python(path: Path) -> ast.Module | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


# --- CD-016: serve() registration wrapped in serve_with_retry ---------------


def _names_imported_from_prefect_serve(tree: ast.Module) -> set[str]:
    """Local names bound to Prefect's ``serve`` by a from-import."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module != "prefect" and not module.startswith("prefect."):
            continue
        for alias in node.names:
            if alias.name == "serve":
                bound.add(alias.asname or alias.name)
    return bound


def _direct_serve_calls(tree: ast.Module) -> list[str]:
    """Describe every direct Prefect ``serve()`` call in one module.

    Three call shapes register deployments without the shared helper:
    ``prefect.serve(...)``, ``<flow>.serve(...)``, and a bare
    ``serve(...)`` where the name came from ``from prefect import serve``.
    Detection is by AST rather than by substring so that the token
    ``serve(`` inside a comment, a docstring or a checker's own pattern
    string is never mistaken for a call.
    """
    bare_names = _names_imported_from_prefect_serve(tree)
    shapes: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "serve":
            prefix = ""
            if isinstance(func.value, ast.Name):
                prefix = f"{func.value.id}."
            elif isinstance(func.value, ast.Attribute):
                prefix = f"{func.value.attr}."
            shapes.append(f"{prefix}serve()")
        elif isinstance(func, ast.Name) and func.id in bare_names:
            shapes.append(f"{func.id}()")
    return shapes


def _helper_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``serve_with_retry(...)`` call, qualified or bare."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id == _SERVE_HELPER) or (
            isinstance(func, ast.Attribute) and func.attr == _SERVE_HELPER
        ):
            calls.append(node)
    return calls


def _imports_helper_from_shared_library(tree: ast.Module) -> bool:
    """True for ``from mini_app_polis.serve_resilience import serve_with_retry``.

    Also accepts ``import mini_app_polis.serve_resilience`` (the helper is
    then reached as an attribute), because that resolves to the same
    shared implementation.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == _SERVE_HELPER_MODULE and any(
                alias.name == _SERVE_HELPER for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _SERVE_HELPER_MODULE:
                    return True
    return False


def _repo_kwarg_present(call: ast.Call) -> bool:
    """True if the call passes ``repo=`` — or a ``**kwargs`` that may hold it.

    A ``**`` unpacking is accepted rather than flagged: its contents are
    not statically knowable, and an ERROR-severity false positive is the
    more expensive mistake.
    """
    for kw in call.keywords:
        if kw.arg == "repo":
            return True
        if kw.arg is None:
            return True
    return False


def _module_paths_for_dotted(repo_path: Path, dotted: str) -> list[Path]:
    """Candidate files for a dotted module name, src-layout first."""
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return []
    candidates: list[Path] = []
    for prefix in (repo_path / "src", repo_path):
        base = prefix.joinpath(*parts)
        candidates.append(base.with_suffix(".py"))
        candidates.append(base / "__main__.py")
        candidates.append(base / "__init__.py")
    return candidates


def _start_command_candidates(repo_path: Path, command: str) -> list[Path]:
    """Files a Railway ``startCommand`` could be launching.

    Handles the two shapes in ecosystem use: ``python -m pkg.main`` and a
    direct ``python path/to/main.py``, each optionally behind a wrapper
    such as ``uv run``.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    candidates: list[Path] = []
    for index, token in enumerate(tokens):
        if token == "-m" and index + 1 < len(tokens):
            candidates.extend(_module_paths_for_dotted(repo_path, tokens[index + 1]))
        elif token.endswith(".py"):
            candidates.append(repo_path / token)
    return candidates


def _src_package_dir(repo_path: Path) -> Path | None:
    src = repo_path / "src"
    if not src.is_dir():
        return None
    for child in sorted(src.iterdir()):
        if child.is_dir() and child.name not in _SKIP_DIRS:
            return child
    return None


def _resolve_entry_point(repo_path: Path) -> tuple[Path | None, list[str], str]:
    """Locate the module the platform actually starts.

    Returns ``(resolved_path, candidates_tried, source_description)``.

    ``railway.json``'s ``deploy.startCommand`` is consulted first because
    CD-017 makes that field mandatory, so it is normally present and it is
    the only statement of the entry point that the deploy itself honours.
    ``src/<pkg>/main.py`` is the fallback for a repository whose descriptor
    is missing or silent about the start command — which CD-017, not this
    rule, is there to flag.

    Each candidate carries the description of where it came from, so a
    finding can say which statement of the entry point it inspected
    instead of leaving the reader to guess.
    """
    tried: list[tuple[Path, str]] = []

    data, _ = _load_json(repo_path / "railway.json")
    if isinstance(data, dict):
        deploy = data.get("deploy")
        if isinstance(deploy, dict):
            command = deploy.get("startCommand")
            if isinstance(command, str) and command.strip():
                described = f"railway.json deploy.startCommand ({command.strip()})"
                tried.extend(
                    (candidate, described)
                    for candidate in _start_command_candidates(repo_path, command)
                )

    package_dir = _src_package_dir(repo_path)
    if package_dir is not None:
        tried.append((package_dir / "main.py", "the src/<pkg>/main.py convention"))
    tried.append((repo_path / "main.py", "the src/<pkg>/main.py convention"))

    labels = [_rel(path, repo_path) for path, _ in tried]
    for candidate, described in tried:
        if candidate.is_file():
            return candidate, labels, described
    fallback_source = tried[0][1] if tried else "the src/<pkg>/main.py convention"
    return None, labels, fallback_source


def _declares_common_python_utils(repo_path: Path) -> bool | None:
    """True/False if pyproject.toml declares the dependency; None if unreadable."""
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.is_file():
        return None
    data, _ = _load_toml(pyproject)
    if data is None:
        return None

    project = data.get("project")
    project = project if isinstance(project, dict) else {}
    requirements: list[str] = []
    deps = project.get("dependencies")
    if isinstance(deps, list):
        requirements.extend(str(item) for item in deps)
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group in optional.values():
            if isinstance(group, list):
                requirements.extend(str(item) for item in group)
    dependency_groups = data.get("dependency-groups")
    if isinstance(dependency_groups, dict):
        for group in dependency_groups.values():
            if isinstance(group, list):
                requirements.extend(str(item) for item in group)

    return any(
        _canonical_name(_requirement_name(req)) == "common-python-utils"
        for req in requirements
    )


def check_cd_016(repo_path: Path) -> list[Finding]:
    """CD-016: the serve() startup registration is wrapped in serve_with_retry.

    A cog registers its flows once, at boot, by calling Prefect's
    ``serve()``. If Prefect Cloud is unreachable for the few seconds the
    container takes to start — a routine occurrence during a Cloud
    deploy — the call raises, the process exits, and Railway's restart
    policy retries into the same window. ``serve_with_retry`` from
    ``mini_app_polis.serve_resilience`` wraps that registration in shared
    backoff, so the check confirms the entry point uses it rather than
    calling ``serve()`` directly.

    **Step (0) is an applicability gate and is the load-bearing part of
    this rule.** The catalog lists ``trigger-cog`` in ``applies_to``
    because some trigger cogs do register Prefect flows — but a
    trigger-cog running a plain asyncio loop with no ``@flow`` has nothing
    to register and no ``serve()`` call anywhere. Such a repository is
    correct, and the rule simply has no subject in it. So before anything
    else the whole production source tree is scanned for any of
    ``prefect.serve(``, ``<flow>.serve(``, a bare ``serve(`` bound by
    ``from prefect import serve``, or ``serve_with_retry(``; if none of
    those calls exists the function returns ``[]`` and emits nothing.
    Without the gate this check would report a false ERROR against every
    correct non-serving cog in the fleet.

    The scan is AST-based on purpose. Substring matching would let a
    ``serve(`` inside a comment, a docstring or a documentation example
    open the gate, which reintroduces the same false positive by a
    different route.

    Then, in the entry point resolved from ``railway.json`` (falling back
    to ``src/<pkg>/main.py``):

    (1) the module must call ``serve_with_retry(...)`` and must import it
        from ``mini_app_polis.serve_resilience`` — a same-named local shim
        does not inherit the shared backoff policy;
    (2) that call must pass ``repo=``, because the helper cannot infer the
        cog identity and the retry findings it emits are unattributable
        without it;
    (3) ``common-python-utils`` must be a declared dependency, or the
        import cannot resolve at runtime no matter how the code reads.

    Scope filtering by repo type is the dispatcher's job, not this
    function's. The gate above is a different thing: check_notes asks for
    it explicitly because it depends on repository content, which only
    this function can see.
    """
    CHECK_ID = "CD-016"
    findings: list[Finding] = []

    # (0) Applicability gate.
    serves_anywhere = False
    for py in _iter_python_sources(repo_path):
        tree = _parse_python(py)
        if tree is None:
            continue
        if _direct_serve_calls(tree) or _helper_calls(tree):
            serves_anywhere = True
            break
    if not serves_anywhere:
        return findings

    # (1) Resolve the entry point and inspect the registration call.
    entry, tried, source = _resolve_entry_point(repo_path)
    if entry is None:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                (
                    f"This repository registers Prefect deployments but its "
                    f"entry point could not be located from {source}; none of "
                    f"{', '.join(tried) or '(no candidates)'} exists, so the "
                    f"{_SERVE_HELPER} wrapping cannot be verified."
                ),
                (
                    "Point railway.json deploy.startCommand at the module that "
                    "registers the flows (for example 'python -m <pkg>.main'), "
                    "or move that module to src/<pkg>/main.py so the deployed "
                    "entry point is discoverable from the repository."
                ),
            )
        )
        return findings

    entry_rel = _rel(entry, repo_path)
    tree = _parse_python(entry)
    if tree is None:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                (
                    f"Entry point {entry_rel} (resolved from {source}) could not "
                    f"be read or parsed as Python, so its serve() registration "
                    f"cannot be confirmed to use {_SERVE_HELPER}."
                ),
                (
                    f"Fix the syntax of {entry_rel} so the deployed entry point "
                    f"parses, then confirm it registers flows through "
                    f"{_SERVE_HELPER} imported from {_SERVE_HELPER_MODULE}."
                ),
            )
        )
        return findings

    direct = _direct_serve_calls(tree)
    helper_calls = _helper_calls(tree)

    if not helper_calls:
        instead = (
            f"it calls {', '.join(sorted(set(direct)))} directly"
            if direct
            else "and no serve registration call was found in it either"
        )
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                (
                    f"Entry point {entry_rel} (resolved from {source}) does not "
                    f"call {_SERVE_HELPER}() — {instead}. An unwrapped "
                    f"registration exits the process when Prefect Cloud is "
                    f"briefly unreachable at boot."
                ),
                (
                    f"Import {_SERVE_HELPER} from {_SERVE_HELPER_MODULE} in "
                    f"{entry_rel} and register the deployments through it, "
                    f"passing repo= so retry findings are attributable."
                ),
            )
        )
    else:
        if direct:
            findings.append(
                _finding(
                    CHECK_ID,
                    "ERROR",
                    _DIMENSION,
                    (
                        f"Entry point {entry_rel} calls {_SERVE_HELPER}() but "
                        f"also registers deployments directly via "
                        f"{', '.join(sorted(set(direct)))}; the direct call is "
                        f"still unprotected against a boot-time Prefect Cloud "
                        f"outage."
                    ),
                    (
                        f"Route every deployment registration in {entry_rel} "
                        f"through {_SERVE_HELPER} and remove the direct "
                        f"serve() call."
                    ),
                )
            )
        if not _imports_helper_from_shared_library(tree):
            findings.append(
                _finding(
                    CHECK_ID,
                    "ERROR",
                    _DIMENSION,
                    (
                        f"Entry point {entry_rel} calls {_SERVE_HELPER}() but "
                        f"does not import it from {_SERVE_HELPER_MODULE} — a "
                        f"same-named local helper does not inherit the shared "
                        f"backoff policy the standard is there to guarantee."
                    ),
                    (
                        f"Replace the local helper with "
                        f"'from {_SERVE_HELPER_MODULE} import {_SERVE_HELPER}' "
                        f"in {entry_rel} so every cog retries identically."
                    ),
                )
            )

        # (2) repo= keyword.
        if not any(_repo_kwarg_present(call) for call in helper_calls):
            findings.append(
                _finding(
                    CHECK_ID,
                    "ERROR",
                    _DIMENSION,
                    (
                        f"{_SERVE_HELPER}() in {entry_rel} is called without a "
                        f"repo= keyword; the shared helper cannot infer the cog "
                        f"identity, so any finding it emits on retry exhaustion "
                        f"is unattributable."
                    ),
                    (
                        f'Pass repo="{repo_path.name}" to the '
                        f"{_SERVE_HELPER}() call in {entry_rel} so retry and "
                        f"failure findings name the cog that produced them."
                    ),
                )
            )

    # (3) The import has to be able to resolve.
    declares = _declares_common_python_utils(repo_path)
    if declares is False:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                (
                    f"common-python-utils is not declared as a dependency in "
                    f"pyproject.toml, so 'from {_SERVE_HELPER_MODULE} import "
                    f"{_SERVE_HELPER}' cannot resolve at runtime."
                ),
                (
                    "Add common-python-utils to [project].dependencies (with a "
                    "[tool.uv.sources] git entry pinned to a version tag) so "
                    "the shared serve helper is installable in the deployed "
                    "image."
                ),
            )
        )

    return findings


# --- CD-020: the lockfile is released with the version it locks -------------


def _requirement_name(requirement: str) -> str:
    """The distribution name at the head of a PEP 508 requirement string."""
    text = requirement.strip()
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", text)
    return match.group(1) if match else ""


def _canonical_name(name: str) -> str:
    """PEP 503 normalisation, so 'common_python_utils' == 'common-python-utils'."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _requirement_specifiers(requirement: str) -> list[tuple[str, str]]:
    """(operator, version) pairs from a requirement's version specifier.

    Environment markers are cut off first: the version in
    ``pkg; python_version >= "3.11"`` belongs to the marker, not to the
    distribution.
    """
    text = requirement.split(";", 1)[0]
    name = _requirement_name(text)
    tail = text[len(name) :]
    tail = re.sub(r"^\s*\[[^\]]*\]", "", tail)  # drop extras
    tail = re.sub(r"@.*$", "", tail)  # drop direct-reference URLs
    return [
        (op, version.strip())
        for op, version in re.findall(r"(===|==|>=|<=|~=|!=|<|>)\s*([^,\s]+)", tail)
    ]


def _normalise_version(value: str) -> str:
    """Strip the tag's 'v' prefix and a specifier's trailing wildcard."""
    return value.strip().lstrip("vV").rstrip(".*").strip()


def _source_ref(source: dict[str, Any]) -> tuple[str, str] | None:
    """The (kind, value) ref a [tool.uv.sources] entry pins to, if any."""
    for key in ("tag", "rev", "branch"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
    return None


def _project_requirements(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(group label, requirement string) for every declared dependency."""
    project = data.get("project")
    project = project if isinstance(project, dict) else {}
    pairs: list[tuple[str, str]] = []

    deps = project.get("dependencies")
    if isinstance(deps, list):
        pairs.extend(("project.dependencies", str(item)) for item in deps)

    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group, items in optional.items():
            if isinstance(items, list):
                pairs.extend(
                    (f"optional-dependencies.{group}", str(item)) for item in items
                )
    return pairs


def _locked_version(repo_path: Path, package_name: str) -> str:
    """The version ``uv.lock`` records for one package, or 'unknown'."""
    data, _ = _load_toml(repo_path / "uv.lock")
    if not isinstance(data, dict):
        return "unknown"
    packages = data.get("package")
    if not isinstance(packages, list):
        return "unknown"
    wanted = _canonical_name(package_name)
    for entry in packages:
        if not isinstance(entry, dict):
            continue
        if _canonical_name(str(entry.get("name", ""))) == wanted:
            version = entry.get("version")
            if isinstance(version, str):
                return version
    return "unknown"


def _releaserc_plugin_config(data: Any, plugin: str) -> tuple[bool, dict[str, Any]]:
    """(present, config) for one semantic-release plugin.

    Plugins appear either as a bare string or as a ``[name, config]``
    pair, and only the pair form carries the settings this rule reads.
    """
    if not isinstance(data, dict):
        return False, {}
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return False, {}
    for entry in plugins:
        if isinstance(entry, str) and entry == plugin:
            return True, {}
        if isinstance(entry, list) and entry and entry[0] == plugin:
            config = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
            return True, config
    return False, {}


def _check_release_relocks(repo_path: Path) -> list[Finding]:
    """CD-020 (1): the release pipeline relocks *and* commits the lockfile."""
    findings: list[Finding] = []
    releaserc = repo_path / ".releaserc.json"
    remediation = (
        'Add "uv lock" to the @semantic-release/exec prepareCmd (after the '
        'version bump) and add "uv.lock" to the @semantic-release/git assets '
        "array, so each release relocks and commits the lockfile together."
    )

    if not releaserc.is_file():
        findings.append(
            _finding(
                "CD-020",
                "ERROR",
                _DIMENSION,
                (
                    ".releaserc.json is absent, so the release cannot relock "
                    "the project or commit uv.lock; every version bump will "
                    "leave the lockfile naming the previous version."
                ),
                remediation,
            )
        )
        return findings

    data, error = _load_json(releaserc)
    if data is None:
        findings.append(
            _finding(
                "CD-020",
                "ERROR",
                _DIMENSION,
                (
                    f".releaserc.json does not parse as JSON ({error}), so the "
                    f"relock step and the uv.lock release asset cannot be "
                    f"confirmed."
                ),
                remediation,
            )
        )
        return findings

    _, exec_config = _releaserc_plugin_config(data, "@semantic-release/exec")
    prepare_cmd = exec_config.get("prepareCmd")
    prepare_cmd = prepare_cmd if isinstance(prepare_cmd, str) else ""
    relocks = bool(re.search(r"\buv\s+lock\b", prepare_cmd))

    _, git_config = _releaserc_plugin_config(data, "@semantic-release/git")
    assets = git_config.get("assets")
    assets = assets if isinstance(assets, list) else []
    commits_lock = any(
        isinstance(asset, str) and Path(asset).name == "uv.lock" for asset in assets
    )

    if relocks and commits_lock:
        return findings

    missing: list[str] = []
    if not relocks:
        shown = repr(prepare_cmd) if prepare_cmd else "absent"
        missing.append(
            f"@semantic-release/exec prepareCmd does not run 'uv lock' ({shown})"
        )
    if not commits_lock:
        shown = repr(assets) if assets else "absent"
        missing.append(
            f"@semantic-release/git assets does not include 'uv.lock' ({shown})"
        )

    findings.append(
        _finding(
            "CD-020",
            "ERROR",
            _DIMENSION,
            (
                ".releaserc.json: "
                + "; ".join(missing)
                + ". Both steps are needed: relocking without committing "
                "uv.lock throws the new lock away, and committing without "
                "relocking ships the old one, so a repository with only one of "
                "them re-drifts on its next release."
            ),
            remediation,
        )
    )
    return findings


def _check_lock_is_current(repo_path: Path) -> list[Finding]:
    """CD-020 (2): ``uv lock --check`` against the checked-out tree.

    Guarded twice over. The ``uv`` binary is looked up first and the
    sub-check is skipped in silence when it is absent, because a tool
    missing from the evaluator's own environment is not the target
    repository's violation. Nor is a non-zero exit on its own: only the
    lockfile-needs-updating message is read as staleness, because uv
    exits non-zero for environment failures too. The subprocess then
    runs under an explicit timeout — ``uv lock --check`` reaches the configured indexes and would
    otherwise be able to hang the entire conformance sweep on a network
    stall — and a timeout, like any other execution failure, is reported
    as nothing rather than as a violation.
    """
    findings: list[Finding] = []
    uv_binary = shutil.which("uv")
    if uv_binary is None:
        return findings

    try:
        result = subprocess.run(
            [uv_binary, "lock", "--check"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_UV_LOCK_CHECK_TIMEOUT_S,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return findings

    if result.returncode == 0:
        return findings

    # Non-zero is not the same as stale. `uv lock --check` exits 1 with
    # "The lockfile at `uv.lock` needs to be updated" when the lock is
    # genuinely out of date, and exits with other codes when it could
    # not run at all — no usable interpreter, an unreadable .venv, no
    # network to resolve a git dependency. Treating every failure as
    # staleness reported identity's lockfile as out of date when
    # `uv lock --check` on a clean copy of that same tree exits 0; what
    # actually failed was reading a stale .venv.
    #
    # So the definitive message is required, not merely a bad exit code.
    # A false negative here costs little — the other three clauses still
    # catch the drift structurally — while a false positive tells you to
    # regenerate a lockfile that is correct.
    combined = f"{result.stdout or ''}\n{result.stderr or ''}"
    if "needs to be updated" not in combined:
        return findings

    data, _ = _load_toml(repo_path / "pyproject.toml")
    project = data.get("project") if isinstance(data, dict) else None
    project = project if isinstance(project, dict) else {}
    name = str(project.get("name") or repo_path.name)
    declared = str(project.get("version") or "unknown")
    locked = _locked_version(repo_path, name)

    gap = (
        f"pyproject.toml declares {name} {declared} while uv.lock records {locked}"
        if declared != locked
        else f"pyproject.toml and uv.lock agree on {name} {declared}, but the "
        f"resolved dependency graph is stale"
    )
    findings.append(
        _finding(
            "CD-020",
            "ERROR",
            _DIMENSION,
            (
                f"'uv lock --check' exits {result.returncode} — uv.lock is out "
                f"of date with pyproject.toml: {gap}. Installs from this tree "
                f"resolve against a lockfile that no longer matches the "
                f"declared dependencies."
            ),
            (
                "Run 'uv lock' and commit the regenerated uv.lock, then make "
                "the release pipeline relock automatically so the lockfile "
                "cannot drift from the version it locks again."
            ),
        )
    )
    return findings


def _check_source_specifier_agreement(
    data: dict[str, Any],
    sources: dict[str, Any],
) -> list[Finding]:
    """CD-020 (3): a dependency's version specifier must match its source ref.

    ``uv`` resolves a ``[tool.uv.sources]`` dependency from the git ref,
    not from the specifier in the requirement string, so a requirement
    reading ``common-python-utils>=3.0`` beside ``rev = "v4.0.0"`` is a
    statement that is simply false in the built image — and the next
    reader believes it. A bare name with no specifier is the correct
    spelling and is never flagged: it says nothing that the source entry
    can contradict.
    """
    findings: list[Finding] = []
    requirements = _project_requirements(data)

    for source_name, source in sources.items():
        if not isinstance(source, dict):
            continue
        ref = _source_ref(source)
        if ref is None:
            continue
        ref_kind, ref_value = ref
        wanted = _canonical_name(source_name)

        for group, requirement in requirements:
            if _canonical_name(_requirement_name(requirement)) != wanted:
                continue
            for operator, version in _requirement_specifiers(requirement):
                if _normalise_version(version) == _normalise_version(ref_value):
                    continue
                findings.append(
                    _finding(
                        "CD-020",
                        "ERROR",
                        _DIMENSION,
                        (
                            f"pyproject.toml {group} pins '{requirement.strip()}' "
                            f"while [tool.uv.sources].{source_name} resolves "
                            f'{ref_kind} = "{ref_value}"; the specifier '
                            f"'{operator}{version}' names a different version "
                            f"from the one actually installed, so the declared "
                            f"dependency misdescribes the build."
                        ),
                        (
                            f"Drop the version specifier and declare "
                            f"'{source_name}' as a bare requirement, or move "
                            f"[tool.uv.sources].{source_name} to the tag the "
                            f"specifier names — the git ref is what uv "
                            f"installs, so the two must not disagree."
                        ),
                    )
                )
                break

    return findings


def _check_source_refs_are_tags(sources: dict[str, Any]) -> list[Finding]:
    """CD-020 (4): every git source must pin a version tag.

    A branch ref re-resolves on every lock, so two builds of the same
    commit can install different library code; a 40-character SHA pins
    reproducibly but is unreadable and invisible to the library's own
    release process, so a bump is impossible to review. Only a version
    tag — ``tag = "v4.0.0"``, or the same string held in ``rev`` — is both
    stable and legible.
    """
    findings: list[Finding] = []
    tag_shaped = re.compile(r"^v?\d+(\.\d+)*([.\-+A-Za-z0-9]*)$")
    sha_shaped = re.compile(r"^[0-9a-fA-F]{40}$")

    for source_name, source in sources.items():
        if not isinstance(source, dict) or "git" not in source:
            continue
        ref = _source_ref(source)

        if ref is None:
            reason = (
                "no tag, rev or branch is given, so it floats on the default branch"
            )
        else:
            ref_kind, ref_value = ref
            if ref_kind == "branch":
                reason = f'branch = "{ref_value}" re-resolves on every lock'
            elif sha_shaped.match(ref_value):
                reason = (
                    f'{ref_kind} = "{ref_value}" is a 40-character commit SHA, '
                    f"not a version tag"
                )
            elif ref_value.lower() in {"main", "master", "head"}:
                reason = (
                    f'{ref_kind} = "{ref_value}" names a moving branch, not a '
                    f"version tag"
                )
            elif not tag_shaped.match(ref_value):
                reason = f'{ref_kind} = "{ref_value}" is not a version tag'
            else:
                continue

        findings.append(
            _finding(
                "CD-020",
                "ERROR",
                _DIMENSION,
                (
                    f"[tool.uv.sources].{source_name} does not pin a version "
                    f"tag: {reason}. The locked dependency is then either "
                    f"irreproducible or unreviewable, and the lockfile stops "
                    f"describing a releasable state."
                ),
                (
                    f"Pin [tool.uv.sources].{source_name} to a released version "
                    f'tag (for example tag = "v4.0.0") and bump it deliberately, '
                    f"so each lock records a reviewable library release."
                ),
            )
        )
    return findings


def check_cd_020(repo_path: Path) -> list[Finding]:
    """CD-020: the lockfile is released with the version it locks.

    The failure this rule exists for is quiet. semantic-release bumps the
    version in ``pyproject.toml`` and commits; ``uv.lock`` still records
    the previous version and the previous dependency graph; the built
    image installs from the lock and runs code nobody released. Nothing
    fails loudly, so the drift is only visible to a check.

    Four independent conditions, each reported separately so a repository
    learns exactly which of them is still open:

    (1) ``.releaserc.json`` must both relock and commit the lock — the
        ``@semantic-release/exec`` ``prepareCmd`` running ``uv lock``, and
        ``uv.lock`` listed in the ``@semantic-release/git`` assets. Either
        one alone lets the drift come back at the next release, which is
        why one finding names both halves.
    (2) ``uv lock --check`` must pass against the checked-out tree, with
        the finding naming the declared and locked versions rather than
        just asserting a gap.
    (3) A dependency carrying a version specifier must not name a version
        different from the ``[tool.uv.sources]`` ref that actually
        installs it. A bare name is correct and is not flagged.
    (4) Every git source must pin a version tag rather than a branch or a
        raw commit SHA.

    A repository with no ``uv.lock`` is exempt outright: it is not
    uv-managed, there is no lockfile to drift, and PY-001 is the rule that
    speaks to that instead. Returning ``[]`` here keeps this ERROR off
    repositories the rule was never written about.
    """
    CHECK_ID = "CD-020"
    findings: list[Finding] = []

    if not (repo_path / "uv.lock").is_file():
        return findings

    findings.extend(_check_release_relocks(repo_path))
    findings.extend(_check_lock_is_current(repo_path))

    data, _ = _load_toml(repo_path / "pyproject.toml")
    if not isinstance(data, dict):
        return findings

    tool = data.get("tool")
    tool = tool if isinstance(tool, dict) else {}
    uv_config = tool.get("uv")
    uv_config = uv_config if isinstance(uv_config, dict) else {}
    sources = uv_config.get("sources")
    if not isinstance(sources, dict):
        return findings

    findings.extend(_check_source_specifier_agreement(data, sources))
    findings.extend(_check_source_refs_are_tags(sources))
    return findings
