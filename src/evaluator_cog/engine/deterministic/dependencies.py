"""XSTACK-007: a repo's pins of the fleet's own libraries stay within a minor.

A per-repo check. It reads this repository's dependency declarations and
nothing of any other repository's: whether a dependency is one of the
fleet's libraries, and what its latest release is, are asked of the
package registry the dependency is published to (PyPI, npm). A package
counts as the fleet's own when the registry records its source repository
under ``github.com/mini-app-polis/``.

That keeps each evaluation independent (ADR-010) and puts the finding on
the repository that has to fix it, where its own release re-evaluates it.
The rule used to run from the fleet-wide lane, reading every repo's
manifest and a shared-library list from the registry, and filed its
findings under ecosystem-standards — so a repo that bumped its pin passed
its own evaluation and still showed the warning until the next fleet run.

A registry that cannot be reached is not a finding. A lookup that fails
leaves that dependency unjudged, and the check says nothing about it:
an outage at PyPI must never read as a stale pin.
"""

from __future__ import annotations

import json
import re
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import Finding, _finding

CHECK_ID = "XSTACK-007"
_DIMENSION = "cross_repo_coherence"

#: Where a package's source must live for it to count as the fleet's own.
_ORG_REPO = re.compile(r"github\.com[/:]mini-app-polis/([\w.-]+?)(?:\.git)?/?$")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

_HTTP_TIMEOUT = 10.0
_CACHE_SECONDS = 15 * 60


@dataclass(frozen=True)
class Published:
    """What a package registry says about a package."""

    latest: str
    #: ``mini-app-polis/<repo>`` when the source lives in the org, else "".
    org_repo: str


#: ``(registry, name) -> Published | None``. ``registry`` is "pypi" or "npm".
Resolver = Callable[[str, str], Published | None]


# ---------------------------------------------------------------------------
# Reading the registries
# ---------------------------------------------------------------------------


def _org_repo(urls: list[str]) -> str:
    for url in urls:
        match = _ORG_REPO.search(str(url).strip())
        if match:
            return f"mini-app-polis/{match.group(1)}"
    return ""


def _fetch(registry: str, name: str) -> Published | None:
    """One registry lookup. Raises on transport errors; None when absent."""
    import httpx

    if registry == "pypi":
        url = f"https://pypi.org/pypi/{name}/json"
    else:
        # The ``/latest`` document, not the full packument: it carries the
        # version and the repository and is a few kilobytes, where the
        # packument of a popular package runs to megabytes.
        url = f"https://registry.npmjs.org/{name.replace('/', '%2F')}/latest"
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        response = client.get(url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json() or {}
    if registry == "pypi":
        info = data.get("info") or {}
        urls = list((info.get("project_urls") or {}).values())
        urls.append(info.get("home_page") or "")
        return Published(str(info.get("version") or ""), _org_repo(urls))
    repository = data.get("repository")
    if isinstance(repository, dict):
        repository = repository.get("url")
    return Published(
        str(data.get("version") or ""),
        _org_repo([repository or "", data.get("homepage") or ""]),
    )


_cache: dict[tuple[str, str], tuple[float, Published | None]] = {}
_cache_lock = threading.Lock()


def registry_resolver(registry: str, name: str) -> Published | None:
    """The default resolver: the public registries, cached per process.

    A fleet run evaluates every repository in one process, and most of
    them depend on the same packages, so each package is asked about once
    per quarter hour rather than once per repository. The cache is short
    because a release is exactly the event that should be seen.

    A failed lookup returns None and is not cached, so the next repository
    tries again.
    """
    key = (registry, name.lower())
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_SECONDS:
            return hit[1]
    try:
        published = _fetch(registry, name)
    except Exception:
        return None
    with _cache_lock:
        _cache[key] = (now, published)
    return published


# ---------------------------------------------------------------------------
# Reading this repository's declarations
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """PEP 503 normalization: ``Foo_Bar.baz`` and ``foo-bar-baz`` are one name."""
    return re.sub(r"[-_.]+", "-", name.strip().lower()).strip("-")


def _classify_pin(raw: str) -> tuple[str, tuple[int, int] | None]:
    """Classify a declared ref as ``version`` / ``sha`` / ``branch`` / ``none``.

    Branch and bare-SHA pins are CD-020's finding, not this rule's, and are
    returned for the caller to skip. ``workspace:*``, ``*`` and a bare name
    are no pin at all.
    """
    ref = (raw or "").strip()
    if not ref:
        return ("none", None)
    # "github:org/repo#v1.2.3" and "git+https://...#main" carry the ref
    # after the fragment marker; the path before it is never a version.
    if "#" in ref:
        ref = ref.split("#", 1)[1].strip()
    lowered = ref.lower()
    if lowered.startswith("workspace:") or lowered in {"*", "latest", "next"}:
        return ("none", None)
    if _SHA_RE.match(lowered):
        return ("sha", None)
    match = _VERSION_RE.search(ref)
    if not match:
        return ("branch", None)
    return ("version", (int(match.group(1)), int(match.group(2))))


def _table(obj: object, key: str) -> dict:
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class _Pin:
    registry: str
    name: str
    spec: str
    where: str


def _python_pins(repo_path: Path) -> tuple[str, list[_Pin]]:
    """``(own distribution name, pins)`` from the root ``pyproject.toml``."""
    path = repo_path / "pyproject.toml"
    if not path.is_file():
        return "", []
    try:
        data = tomllib.loads(path.read_text(errors="replace"))
    except (tomllib.TOMLDecodeError, ValueError):
        return "", []
    project = _table(data, "project")
    own = str(project.get("name") or "")

    sources = _table(_table(_table(data, "tool"), "uv"), "sources")
    requirements: list[tuple[str, str]] = []
    deps = project.get("dependencies")
    if isinstance(deps, list):
        requirements += [
            (d, "project.dependencies") for d in deps if isinstance(d, str)
        ]
    for extra, group in _table(project, "optional-dependencies").items():
        if isinstance(group, list):
            where = f"project.optional-dependencies.{extra}"
            requirements += [(d, where) for d in group if isinstance(d, str)]

    pins: list[_Pin] = []
    for requirement, where in requirements:
        match = _REQ_NAME_RE.match(requirement)
        if not match:
            continue
        name = match.group(1)
        spec = requirement[match.end() :]
        # A [tool.uv.sources] override is what uv actually resolves — a git
        # tag, say — so it is the pin that counts when present.
        source = sources.get(name) or sources.get(_normalize(name))
        if isinstance(source, dict):
            spec = str(
                source.get("tag") or source.get("rev") or source.get("branch") or spec
            )
            where = f"[tool.uv.sources].{name}"
        pins.append(_Pin("pypi", name.split("[", 1)[0], spec, where))
    return own, pins


def _npm_pins(repo_path: Path) -> tuple[set[str], list[_Pin]]:
    """``(own package names, pins)`` from every ``package.json`` in the repo.

    Every manifest, not only the root's: a workspace declares its
    dependencies in its packages, and a monorepo's root often declares
    none.
    """
    own: set[str] = set()
    pins: list[_Pin] = []
    for path in sorted(repo_path.rglob("package.json")):
        rel_parts = path.relative_to(repo_path).parts
        if "node_modules" in rel_parts or any(
            p.startswith(".") for p in rel_parts[:-1]
        ):
            continue
        try:
            data = json.loads(path.read_text(errors="replace"))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("name"):
            own.add(str(data["name"]))
        rel = path.relative_to(repo_path).as_posix()
        for section in ("dependencies", "devDependencies"):
            for name, spec in _table(data, section).items():
                pins.append(_Pin("npm", str(name), str(spec), f"{rel} {section}"))
    return own, pins


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def check_xstack_007(
    repo_path: Path, resolver: Resolver | None = None
) -> list[Finding]:
    """XSTACK-007: this repo's pins of the fleet's libraries stay within a minor.

    For each declared dependency, the registry it is published to says
    whether its source lives in mini-app-polis and what its latest release
    is. A pin whose minor is more than one behind that release is stale; a
    major behind is reported too. Only MAJOR.MINOR is compared — an
    untaken patch does not make a consumer stale. A library does not pin
    itself, and branch and SHA pins are CD-020's.
    """
    resolve = resolver or registry_resolver
    own_python, python_pins = _python_pins(repo_path)
    own_npm, npm_pins = _npm_pins(repo_path)
    own = {_normalize(own_python)} | {_normalize(n) for n in own_npm}

    findings: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for pin in [*python_pins, *npm_pins]:
        if _normalize(pin.name) in own:
            continue
        kind, pinned = _classify_pin(pin.spec)
        if kind != "version" or pinned is None:
            continue
        key = (pin.registry, _normalize(pin.name), pin.where)
        if key in seen:
            continue
        seen.add(key)

        published = resolve(pin.registry, pin.name)
        if published is None or not published.org_repo:
            continue
        latest_kind, latest = _classify_pin(published.latest)
        if latest_kind != "version" or latest is None:
            continue

        (pin_major, pin_minor), (top_major, top_minor) = pinned, latest
        if top_major > pin_major:
            reason = f"a full major behind (pinned {pin_major}.x, latest {top_major}.x)"
        elif top_major == pin_major and top_minor - pin_minor > 1:
            reason = (
                f"{top_minor - pin_minor} minors behind (pinned "
                f"{pin_major}.{pin_minor}, latest {top_major}.{top_minor})"
            )
        else:
            continue

        declared = pin.spec.strip()
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"{pin.where} pins '{pin.name}' ({published.org_repo}) at "
                f"'{declared}', but its latest release is {published.latest} — "
                f"{reason}. Consumers more than one minor behind force the "
                f"library to keep two API shapes alive at once.",
                f"Raise the floor of '{pin.name}' in {pin.where} to "
                f"{published.latest}, run the test suite against it, and "
                f"release.",
            )
        )
    return findings
