"""Cross-repo coherence checks (XSTACK-006, XSTACK-007).

Why this module looks nothing like its siblings
-----------------------------------------------
Every other module in ``evaluator_cog/engine/deterministic`` exports
``check_*(repo_path: Path)`` functions: one cloned repo goes in, findings
about that repo come out. The two rules implemented here cannot be
expressed that way, and their catalog entries say so — both carry
``applies_to: None``.

``applies_to: None`` is not an oversight. It means
``EvaluatorConfig.resolve_dispatch()`` returns ``SKIP_SCOPE`` for the
rule, so ``runner.py`` will never invoke it on the per-repo path — which
is correct, because neither rule has a per-repo question to ask:

  * **XSTACK-006** asks whether the *set* of GitHub repos carrying an
    ``evaluator.yaml`` is a subset of the *set* of repos registered in
    ``ecosystem.yaml``. Standing inside any one clone, that question is
    unanswerable: a repo cannot see the repos that are missing from the
    registry alongside it.
  * **XSTACK-007** asks whether each consumer's pin of a shared library
    is close enough to that library's latest release. The latest release
    lives in the library's repo, not the consumer's, so the comparison
    needs two read sources at once.

Both therefore run once per flow invocation, from the "applies_to-absent"
lane in ``evaluator_cog/flows/conformance.py::_run_applies_to_absent_checks``
— the same lane that carries EVAL-003, MONO-003 and EVAL-007. That lane
calls checks with keyword-only arguments and no ``repo_path``, so the
functions below are shaped to match it exactly: keyword-only, ``->
list[Finding]``, and returning ``[]`` rather than raising when their
inputs are absent.

The registry is passed in, never re-fetched
-------------------------------------------
XSTACK-006's check_notes are explicit: read the registry *at the version
under evaluation*, not from a cached copy, so that a repo registered in
the very release that adds it is not reported as unregistered. The
``ecosystem`` dict handed to these functions by the flow **is** that
registry — it was fetched once at the top of the run against the version
being evaluated. Re-fetching it here would reintroduce exactly the race
the note warns about, so we do not.

Network failures are never conformance violations
-------------------------------------------------
Both rules read GitHub. A rate limit, a DNS blip or an expired token
must not be reported as "this repo is unregistered" or "this pin is
stale" — those would be fabricated violations against innocent repos.
Following ``check_eval_003`` / ``check_mono_003``, any failure to reach
a required read source produces a single finding tagged with the
``CHECKER`` sentinel rule ID at WARN severity, and the check returns
immediately without emitting any rule findings. ``CHECKER`` is the
house marker for infrastructure errors; EVAL-003 explicitly excludes it
from finding-quality grading, so these do not pollute the corpus.

Nothing in this module raises.
"""

from __future__ import annotations

import json
import os
import re
import tomllib

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)

_GITHUB_API = "https://api.github.com"
_DEFAULT_ORG = "mini-app-polis"
_DIMENSION = "cross_repo_coherence"

# Matches the timeout used by config.py::check_standards_freshness, the
# existing precedent for GitHub API reads from a deterministic check.
_HTTP_TIMEOUT = 20.0

# GitHub's maximum page size for list endpoints. Using the maximum keeps
# the org listing to a single request for any plausible fleet size.
_PER_PAGE = 100

# Hard stop on pagination. A runaway loop against a paginated endpoint is
# the one way this module could hang a flow run; 20 pages is 2000 repos,
# far beyond the org's size, so hitting it means something is wrong.
_MAX_ORG_PAGES = 20

# A bare 40-character commit SHA. Pins of this shape belong to CD-020.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# First MAJOR.MINOR pair anywhere in a ref or specifier: "v4.1.0",
# "^4.1.0", ">=4.1,<5.0" and "4.1" all yield (4, 1).
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")

# Leading distribution name of a PEP 508 requirement string.
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """PEP 503-style normalization, also applied to npm names.

    ``common_python_utils``, ``Common-Python-Utils`` and
    ``common.python.utils`` all name the same distribution. Registry IDs,
    pyproject keys and package.json keys are compared only after passing
    through here so a cosmetic spelling difference cannot hide a pin.
    """
    return re.sub(r"[-_.]+", "-", name.strip().lower()).strip("-")


def _strip_npm_scope(name: str) -> str:
    """Drop an npm scope: ``@mini-app-polis/foo`` -> ``foo``.

    The registry names repos, not npm packages, so a scoped dependency
    key would never match a registry ID without this.
    """
    if name.startswith("@") and "/" in name:
        return name.split("/", 1)[1]
    return name


def _registry_repo_names(ecosystem: dict) -> set[str]:
    """Every repo name ``ecosystem.yaml`` knows about, normalized.

    check_notes for XSTACK-006 names ``repos[].id`` as the registry key.
    The live ``ecosystem.yaml`` spells that list ``services[]`` and adds a
    ``monorepos[]`` list whose entries carry a ``repo`` field; a service
    entry may also carry an explicit ``repo`` that differs from its ``id``.
    All of those are "this repo is registered" for the purposes of this
    rule, so the union is taken rather than a single key. Reading only
    one spelling would report every monorepo in the fleet as unregistered.
    """
    names: set[str] = set()
    for key in ("repos", "services", "monorepos", "libraries"):
        entries = ecosystem.get(key) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for field in ("id", "repo"):
                value = entry.get(field)
                if value:
                    names.add(_normalize(str(value)))
    return names


def _tracked_libraries(ecosystem: dict) -> dict[str, str]:
    """``{normalized_name: repo_name}`` for every ``type: shared-library``.

    XSTACK-007 step (1): the tracked set is defined by the registry, not
    by a hardcoded list, so adding a library to ``ecosystem.yaml`` is all
    it takes to bring its consumers under the rule.
    """
    libs: dict[str, str] = {}
    for key in ("repos", "services", "libraries"):
        entries = ecosystem.get(key) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type") or "") != "shared-library":
                continue
            repo_name = str(entry.get("repo") or entry.get("id") or "").strip()
            if repo_name:
                libs[_normalize(repo_name)] = repo_name
    return libs


def _fleet_repo_names(ecosystem: dict) -> list[str]:
    """Repo names to inspect for dependency declarations, in registry order.

    "Each repo in the fleet" is read as "every repo the registry lists" —
    no status filter is applied, because check_notes names no status
    predicate and inventing one would silently narrow the rule.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for key in ("repos", "services", "monorepos"):
        entries = ecosystem.get(key) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("repo") or entry.get("id") or "").strip()
            if not name:
                continue
            norm = _normalize(name)
            if norm in seen:
                continue
            seen.add(norm)
            ordered.append(name)
    return ordered


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def _resolve_token(github_token: str | None) -> str:
    """Explicit argument wins; otherwise fall back to ``GITHUB_TOKEN``.

    An empty string is a legitimate explicit value (meaning "make
    unauthenticated requests"), so only ``None`` triggers the env read.
    """
    if github_token is not None:
        return github_token
    return os.environ.get("GITHUB_TOKEN", "")


def _gh_headers(token: str) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _list_org_repos(org: str, token: str) -> list[str]:
    """Every repo name in ``org``, following pagination to the last page.

    Pagination is not optional here. GitHub caps a page at 100 entries;
    stopping after the first page would silently drop every repo past
    that boundary, and for XSTACK-006 a dropped repo is precisely the
    unregistered repo the rule exists to surface — a false clean run.

    Raises on any transport or HTTP error so the caller can convert it
    into a CHECKER finding rather than a bogus conformance verdict.
    """
    import httpx

    names: list[str] = []
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        for page in range(1, _MAX_ORG_PAGES + 1):
            response = client.get(
                f"{_GITHUB_API}/orgs/{org}/repos",
                params={"per_page": _PER_PAGE, "page": page, "type": "all"},
                headers=_gh_headers(token),
            )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            for entry in batch:
                if isinstance(entry, dict) and entry.get("name"):
                    names.append(str(entry["name"]))
            if len(batch) < _PER_PAGE:
                break
    return names


def _fetch_repo_file(org: str, repo: str, path: str, token: str) -> str | None:
    """Return the raw text of ``path`` at ``repo``'s default branch.

    ``None`` means the file is definitively absent (HTTP 404) — that is a
    fact about the repo, not a failure, and callers act on it. Every other
    error raises, because "I could not tell" must never be collapsed into
    "the file is not there".
    """
    import httpx

    headers = _gh_headers(token)
    headers["Accept"] = "application/vnd.github.raw"
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        response = client.get(
            f"{_GITHUB_API}/repos/{org}/{repo}/contents/{path}",
            headers=headers,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text


def _latest_release_tag(org: str, repo: str, token: str) -> str | None:
    """The ``tag_name`` of ``repo``'s latest release, or ``None`` if it has none.

    A library with no published release gives XSTACK-007 nothing to
    compare against, so its consumers cannot be stale by this rule's
    definition and it is skipped. A 404 from this endpoint is exactly
    that case; anything else raises.
    """
    import httpx

    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        response = client.get(
            f"{_GITHUB_API}/repos/{org}/{repo}/releases/latest",
            headers=_gh_headers(token),
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json() or {}
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "").strip()
    return tag or None


def _checker_finding(rule_id: str, detail: str) -> Finding:
    """The house sentinel for "the check could not run", not "the repo failed".

    Mirrors ``check_eval_003`` / ``check_mono_003``: rule ID ``CHECKER``,
    WARN severity, the rule's own dimension, and a remediation aimed at
    the operator rather than at any target repo.
    """
    return _finding(
        "CHECKER",
        "WARN",
        _DIMENSION,
        f"{rule_id}: {detail}",
        "Investigate GitHub API connectivity, rate limits and the "
        "GITHUB_TOKEN credential used by evaluator-cog, then re-run the "
        "conformance flow. No conformance verdict was produced for this rule.",
    )


# ---------------------------------------------------------------------------
# Pin parsing
# ---------------------------------------------------------------------------


def _sub_dict(obj: dict, key: str) -> dict:
    """``obj[key]`` when it is a dict, else an empty dict.

    Manifests come off the network and may be any shape; walking them
    with this keeps every access total, so a hand-edited pyproject.toml
    can never make a checker raise.
    """
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def _classify_pin(raw: str) -> tuple[str, tuple[int, int] | None]:
    """Classify a declared dependency ref as version / sha / branch / none.

    Step (5) of XSTACK-007's check_notes: a pin to a branch or a bare
    commit SHA is CD-020's finding, not this rule's. Reporting it here
    too would double-charge the same repo for one defect across two
    rules, so those classifications are returned for the caller to skip
    rather than flag.

    Returns ``(kind, (major, minor))`` where ``kind`` is one of:
      ``"version"`` — a MAJOR.MINOR was extracted and is comparable.
      ``"sha"``     — a bare 40-hex commit SHA; CD-020's territory.
      ``"branch"``  — a ref with no version in it; CD-020's territory.
      ``"none"``    — no pin at all (bare name, ``workspace:*``, ``*``).
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
        # No MAJOR.MINOR anywhere: a branch name ("main", "develop") or a
        # short SHA. Either way there is nothing to compare, and CD-020
        # already owns the complaint.
        return ("branch", None)

    try:
        return ("version", (int(match.group(1)), int(match.group(2))))
    except ValueError:  # pragma: no cover - regex guarantees digits
        return ("branch", None)


def _parse_python_pins(
    text: str, tracked: dict[str, str]
) -> list[tuple[str, str, str]]:
    """Pins for tracked libraries declared in a ``pyproject.toml``.

    Reads the two places check_notes names for Python:
    ``[tool.uv.sources]`` (the ecosystem's normal way of consuming a
    shared library — a git dependency with a ``rev``/``tag``) and
    ``project.dependencies`` (PEP 508 requirement strings).

    Returns ``(normalized_library, raw_pin, source_label)`` triples. An
    unparseable file yields nothing: TOML validity is not this rule's
    business.
    """
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    pins: list[tuple[str, str, str]] = []

    sources = _sub_dict(_sub_dict(_sub_dict(data, "tool"), "uv"), "sources")
    for name, spec in sources.items():
        key = _normalize(str(name))
        if key not in tracked:
            continue
        if isinstance(spec, dict):
            raw = spec.get("rev") or spec.get("tag") or spec.get("branch") or ""
        else:
            raw = spec
        pins.append((key, str(raw), "[tool.uv.sources]"))

    deps = _sub_dict(data, "project").get("dependencies")
    if isinstance(deps, list):
        for dep in deps:
            if not isinstance(dep, str):
                continue
            name_match = _REQ_NAME_RE.match(dep)
            if not name_match:
                continue
            key = _normalize(name_match.group(1))
            if key not in tracked:
                continue
            pins.append((key, dep[name_match.end() :], "project.dependencies"))

    return pins


def _parse_ts_pins(text: str, tracked: dict[str, str]) -> list[tuple[str, str, str]]:
    """Pins for tracked libraries declared in a ``package.json``.

    Reads ``dependencies`` and ``devDependencies``, the two sections
    check_notes names for TypeScript. npm scopes are stripped before
    matching so ``@mini-app-polis/common-typescript-utils`` resolves to
    the registry's ``common-typescript-utils``.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    pins: list[tuple[str, str, str]] = []
    for section in ("dependencies", "devDependencies"):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            key = _normalize(_strip_npm_scope(str(name)))
            if key not in tracked:
                continue
            pins.append((key, str(spec), f"package.json {section}"))
    return pins


# ---------------------------------------------------------------------------
# XSTACK-006
# ---------------------------------------------------------------------------


def check_xstack_006(
    *,
    ecosystem: dict | None = None,
    github_token: str | None = None,
    org: str = "mini-app-polis",
) -> list[Finding]:
    """XSTACK-006: every repo carrying an ``evaluator.yaml`` is registered.

    An ``evaluator.yaml`` at a repo root is that repo declaring itself a
    participant in the conformance system. If the repo is not also listed
    in ``ecosystem.yaml``, the fleet-wide run never enumerates it: it is
    evaluated by nobody, its findings are never posted, and its drift is
    invisible. That gap — a repo that believes it is being graded while
    the grader has never heard of it — is what this rule catches.

    Shape (per check_notes):
      1. Enumerate the repos in the GitHub org (paginated; see
         ``_list_org_repos`` for why that matters here specifically).
      2. Test for ``evaluator.yaml`` at each repo root via the contents
         API — a 404 is a definitive "absent".
      3. Fail where the file exists and the repo name is absent from the
         registry.

    Two deliberate choices:

    *The registry is the ``ecosystem`` argument, never a re-fetch.* The
    caller passes the registry at the version under evaluation. A repo
    added to ``ecosystem.yaml`` in the same release that creates it must
    not be reported, and re-reading ``main`` here would reintroduce that
    race.

    *Only unregistered repos are probed for ``evaluator.yaml``.* A
    registered repo cannot violate this rule no matter what files it
    carries, so its contents call would be pure cost. This makes the
    per-repo request count proportional to the size of the gap, not to
    the size of the org.

    Never raises. If the org listing or a contents probe cannot be
    reached, a single ``CHECKER`` WARN is returned and no XSTACK-006
    findings are emitted — an unreachable GitHub must never be reported
    as an unregistered repo.
    """
    CHECK_ID = "XSTACK-006"

    if ecosystem is None:
        return []

    token = _resolve_token(github_token)
    registered = _registry_repo_names(ecosystem)

    try:
        org_repos = _list_org_repos(org, token)
    except Exception as exc:
        return [
            _checker_finding(
                CHECK_ID,
                f"could not enumerate repos in the '{org}' GitHub org: {exc}",
            )
        ]

    findings: list[Finding] = []
    for repo_name in org_repos:
        if _normalize(repo_name) in registered:
            continue
        try:
            contents = _fetch_repo_file(org, repo_name, "evaluator.yaml", token)
        except Exception as exc:
            return [
                _checker_finding(
                    CHECK_ID,
                    f"could not read evaluator.yaml from '{org}/{repo_name}': {exc}",
                )
            ]
        if contents is None:
            continue

        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"Repo '{org}/{repo_name}' carries an evaluator.yaml at its "
                f"root but '{repo_name}' is absent from the ecosystem.yaml "
                f"registry under evaluation (which lists {len(registered)} "
                f"repo names). The repo declares itself conformance-managed, "
                f"but the fleet-wide run never enumerates it, so it is graded "
                f"by nothing and its drift is invisible.",
                f"Add a registry entry for '{repo_name}' to ecosystem.yaml in "
                f"ecosystem-standards (id, type, status, language) so the "
                f"conformance flow enumerates it — or, if '{repo_name}' is not "
                f"part of the fleet, delete its evaluator.yaml.",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# XSTACK-007
# ---------------------------------------------------------------------------


def check_xstack_007(
    *,
    ecosystem: dict | None = None,
    github_token: str | None = None,
) -> list[Finding]:
    """XSTACK-007: shared-library pins stay within one minor of latest.

    A shared library is only shared if its consumers actually move with
    it. Once a consumer drifts more than one minor release behind, the
    library's authors are maintaining two shapes of the same API at once
    and every cross-repo change becomes a negotiation. This rule measures
    that drift mechanically.

    Shape (per check_notes):
      1. The tracked set is every ``type: shared-library`` entry in
         ``ecosystem.yaml`` — the registry defines it, not a constant here.
      2. Each library's latest release tag is resolved from GitHub. A
         library with no releases is skipped: with no published version,
         no consumer can be measured against it.
      3. Every repo in the fleet has its ``pyproject.toml``
         (``[tool.uv.sources]`` and ``project.dependencies``) and its
         ``package.json`` (``dependencies`` and ``devDependencies``) read
         for declarations of a tracked library.
      4. A pin is stale when its minor is more than one behind the
         library's latest minor. Only MAJOR.MINOR is compared — an
         un-taken patch release does not make a consumer stale, and
         flagging it would make the rule noise. Major skew is reported
         too, following the precedent of ``check_eval_007``'s
         standards-version block, which treats a major behind as
         strictly worse than the minor case it also flags.
      5. Branch pins and bare 40-hex commit SHAs are **skipped**, not
         flagged. check_notes assigns those to CD-020; emitting here as
         well would charge one repo twice for one defect.

    On the reading of range specifiers: ``^4.1.0`` and ``>=4.1,<5.0``
    are read at the minor they name (4.1). The declaration is what a
    reviewer auditing the file sees and what a fresh lockfile resolution
    is bounded by, and check_notes carves out only branch and SHA pins —
    so ranges stay in scope, read at their stated minor.

    Runs from the applies_to-absent lane (see the module docstring): the
    comparison spans a consumer repo and a library repo, so it belongs to
    no single ``repo_path``.

    Never raises. Any unreachable read source yields one ``CHECKER`` WARN
    and no XSTACK-007 findings, so a rate limit can never masquerade as
    fleet-wide staleness.
    """
    CHECK_ID = "XSTACK-007"

    if ecosystem is None:
        return []

    token = _resolve_token(github_token)
    tracked = _tracked_libraries(ecosystem)
    if not tracked:
        return []

    # (2) Resolve each tracked library's latest release.
    latest: dict[str, tuple[int, int]] = {}
    latest_tag: dict[str, str] = {}
    for lib_key, lib_repo in tracked.items():
        try:
            tag = _latest_release_tag(_DEFAULT_ORG, lib_repo, token)
        except Exception as exc:
            return [
                _checker_finding(
                    CHECK_ID,
                    f"could not resolve the latest release of shared library "
                    f"'{lib_repo}': {exc}",
                )
            ]
        if not tag:
            continue
        kind, parsed = _classify_pin(tag)
        if kind != "version" or parsed is None:
            # A release tag we cannot read as MAJOR.MINOR gives us no
            # baseline; skipping is the only honest option.
            continue
        latest[lib_key] = parsed
        latest_tag[lib_key] = tag

    if not latest:
        return []

    findings: list[Finding] = []

    # (3) Read every fleet repo's dependency declarations.
    for repo_name in _fleet_repo_names(ecosystem):
        pins: list[tuple[str, str, str]] = []
        for path, parser in (
            ("pyproject.toml", _parse_python_pins),
            ("package.json", _parse_ts_pins),
        ):
            try:
                text = _fetch_repo_file(_DEFAULT_ORG, repo_name, path, token)
            except Exception as exc:
                return [
                    _checker_finding(
                        CHECK_ID,
                        f"could not read {path} from "
                        f"'{_DEFAULT_ORG}/{repo_name}': {exc}",
                    )
                ]
            if text:
                pins.extend(parser(text, tracked))

        for lib_key, raw_pin, source_label in pins:
            if lib_key not in latest:
                continue
            # A library does not pin itself.
            if _normalize(repo_name) == lib_key:
                continue

            kind, pinned = _classify_pin(raw_pin)
            # (5) Branch / SHA pins belong to CD-020; "none" is not a pin.
            if kind != "version" or pinned is None:
                continue

            latest_major, latest_minor = latest[lib_key]
            pinned_major, pinned_minor = pinned
            lib_repo = tracked[lib_key]
            tag = latest_tag[lib_key]
            declared = raw_pin.strip() or f"{pinned_major}.{pinned_minor}"

            if latest_major > pinned_major:
                reason = (
                    f"a full major behind (pinned major {pinned_major}, "
                    f"latest major {latest_major})"
                )
            elif latest_major == pinned_major and (latest_minor - pinned_minor) > 1:
                reason = (
                    f"{latest_minor - pinned_minor} minors behind "
                    f"(pinned minor {pinned_major}.{pinned_minor}, "
                    f"latest minor {latest_major}.{latest_minor})"
                )
            else:
                continue

            findings.append(
                _finding(
                    CHECK_ID,
                    "WARN",
                    _DIMENSION,
                    f"Repo '{repo_name}' pins shared library '{lib_repo}' at "
                    f"'{declared}' in {source_label}, but the latest release "
                    f"of '{lib_repo}' is '{tag}' — {reason}. Consumers more "
                    f"than one minor behind force the library to keep two API "
                    f"shapes alive at once.",
                    f"Bump the '{lib_repo}' declaration in {source_label} of "
                    f"'{repo_name}' from '{declared}' to '{tag}', run that "
                    f"repo's test suite against the new version, and release "
                    f"the result.",
                )
            )

    return findings
