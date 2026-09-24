"""Cross-repo coherence checks (XSTACK-006, XSTACK-008).

Why this module looks nothing like its siblings
-----------------------------------------------
Every other module in ``evaluator_cog/engine/deterministic`` exports
``check_*(repo_path: Path)`` functions: one cloned repo goes in, findings
about that repo come out. The two rules implemented here cannot be
expressed that way, and their catalog entries say so — both carry
``applies_to: None``. Both are about the registry itself, so their findings
belong to ecosystem-standards.

``applies_to: None`` is not an oversight. It means
``EvaluatorConfig.resolve_dispatch()`` returns ``SKIP_SCOPE`` for the
rule, so ``runner.py`` will never invoke it on the per-repo path — which
is correct, because neither rule has a per-repo question to ask:

  * **XSTACK-006** asks whether the *set* of GitHub repos carrying an
    ``evaluator.yaml`` is a subset of the *set* of repos registered in
    ``ecosystem.yaml``. Standing inside any one clone, that question is
    unanswerable: a repo cannot see the repos that are missing from the
    registry alongside it.
  * **XSTACK-008** asks whether every repo the registry lists resolves
    where the listing says — a question about the registry, answered from
    the run's own download results.

XSTACK-007 (shared-library pins) used to live here. It is a question about
one repository's dependencies, so it moved to the per-repo checks in
``dependencies.py``, where the repository that has to fix it owns the
finding and its own release re-evaluates it.

Both therefore run once per flow invocation, from the "applies_to-absent"
lane in ``evaluator_cog/flows/conformance.py::_run_applies_to_absent_checks``
— the same lane that carries EVAL-003 and EVAL-007. That lane
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
Following ``check_eval_003``, any failure to reach
a required read source produces a single finding tagged with the
``CHECKER`` sentinel rule ID at WARN severity, and the check returns
immediately without emitting any rule findings. ``CHECKER`` is the
house marker for infrastructure errors; EVAL-003 explicitly excludes it
from finding-quality grading, so these do not pollute the corpus.

Nothing in this module raises.
"""

from __future__ import annotations

import os
import re

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


def _registry_repo_names(ecosystem: dict) -> set[str]:
    """Every repo name ``ecosystem.yaml`` knows about, normalized.

    check_notes for XSTACK-006 names ``repos[].id`` as the registry key.
    The live ``ecosystem.yaml`` spells that list ``services[]``, and a
    service entry may also carry an explicit ``repo`` that differs from
    its ``id``. All of those are "this repo is registered" for the
    purposes of this rule, so the union is taken rather than a single key.
    """
    names: set[str] = set()
    for key in ("repos", "services", "libraries"):
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


def _checker_finding(rule_id: str, detail: str) -> Finding:
    """The house sentinel for "the check could not run", not "the repo failed".

    Mirrors ``check_eval_003``: rule ID ``CHECKER``,
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


_ZIPBALL_RE = re.compile(
    r"/repos/(?P<org>[^/]+)/(?P<repo>[^/]+)/zipball/(?P<branch>.+)$"
)


def check_xstack_008(
    *, unresolved: list[dict[str, str]] | None = None
) -> list[Finding]:
    """XSTACK-008: every registered repo resolved where the registry says.

    The other half of XSTACK-006. That rule asks whether a repo which
    declares itself governed appears in the registry; this one asks
    whether a repo the registry lists exists where the listing says it
    does. A registry entry that does not resolve is the worse of the two
    states: the repo is counted as governed, it carries an
    ``evaluator.yaml`` saying so, and no rule has ever run against it.
    The belief is not merely absent — it is false, and the registry is
    what makes it false.

    Costs nothing. The conformance run already downloads every registered
    repo, so this reads the downloads that came back 404 rather than
    asking GitHub a second time whether each repo exists.

    Only a 404 is reported. A download that failed on a 403, 429, 5xx,
    timeout or connection error means the run could not tell whether the
    repo is there, and "I could not tell" must never be collapsed into
    "it is not there" — the caller records those separately and they
    never reach this list.
    """
    CHECK_ID = "XSTACK-008"
    findings: list[Finding] = []
    for entry in unresolved or []:
        url = entry.get("url", "")
        label = entry.get("label") or "unknown"
        match = _ZIPBALL_RE.search(url)
        if match:
            org, repo, branch = (
                match.group("org"),
                match.group("repo"),
                match.group("branch"),
            )
            where = f"{org}/{repo} at branch {branch}"
            suggestion = (
                f"Either the repo does not exist under {org}, or the registry "
                f"entry is stale. Correct `org:`, `repo:` or `branch:` on the "
                f"{repo} entry in ecosystem.yaml, or remove the entry if the "
                f"repo is gone."
            )
        else:
            where = label
            suggestion = (
                "Correct the org, repo name or branch for this entry in "
                "ecosystem.yaml, or remove the entry if the repo is gone."
            )
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                "cross_repo_coherence",
                f"Registered repo did not resolve: {where} returned 404. It is "
                f"counted as governed and carries a registry entry, but no rule "
                f"ran against it this run.",
                suggestion,
            )
        )
    return findings
