"""Tests for the cross-repo coherence checks (XSTACK-006, XSTACK-008).

Both checks read GitHub. Every test here mocks the transport with respx
— no test may make a real HTTP call, both because the suite must be
hermetic and because an unauthenticated GitHub request is rate-limited
per IP and would make CI flaky in a way that looks like a rule failure.

Several tests deliberately register *no* route for an endpoint the code
must not touch: with ``@respx.mock`` active, an unmocked request raises,
so "returns [] without calling the network" is asserted by construction
rather than by inspection.
"""

import httpx
import respx

from evaluator_cog.engine.deterministic.crossrepo import (
    check_xstack_006,
    check_xstack_008,
)

_ORG = "mini-app-polis"
_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _ecosystem(services: list[dict]) -> dict:
    return {"services": services}


def _mock_org_repos(names: list[str], status: int = 200) -> None:
    """Mock the paginated org listing with a single short page."""
    if status != 200:
        respx.get(url__regex=rf"{_API}/orgs/{_ORG}/repos.*").mock(
            return_value=httpx.Response(status, json={"message": "boom"})
        )
        return
    respx.get(url__regex=rf"{_API}/orgs/{_ORG}/repos.*").mock(
        return_value=httpx.Response(200, json=[{"name": n} for n in names])
    )


def _mock_contents(repo: str, path: str, text: str | None) -> None:
    """Mock a contents read: ``None`` means the file is absent (404)."""
    url = f"{_API}/repos/{_ORG}/{repo}/contents/{path}"
    if text is None:
        respx.get(url=url).mock(return_value=httpx.Response(404, json={}))
    else:
        respx.get(url=url).mock(return_value=httpx.Response(200, text=text))


# ---------------------------------------------------------------------------
# XSTACK-006
# ---------------------------------------------------------------------------


@respx.mock
def test_xstack_006_no_ecosystem_returns_empty() -> None:
    """No registry means no comparison — and no network call at all.

    respx is active with zero routes, so
    any HTTP request would raise; returning [] proves none was made.
    """
    assert check_xstack_006(ecosystem=None, github_token="t") == []


@respx.mock
def test_xstack_006_clean_fleet_returns_empty() -> None:
    """Every org repo is registered, so nothing is probed and nothing fires."""
    _mock_org_repos(["evaluator-cog", "common-python-utils"])
    ecosystem = _ecosystem(
        [
            {"id": "evaluator-cog", "type": "pipeline-cog", "status": "active"},
            {"id": "common-python-utils", "type": "shared-library", "status": "active"},
        ]
    )
    assert check_xstack_006(ecosystem=ecosystem, github_token="t") == []


@respx.mock
def test_xstack_006_flags_unregistered_repo_with_evaluator_yaml() -> None:
    _mock_org_repos(["evaluator-cog", "ghost-cog"])
    _mock_contents("ghost-cog", "evaluator.yaml", "type: pipeline-cog\n")

    ecosystem = _ecosystem(
        [{"id": "evaluator-cog", "type": "pipeline-cog", "status": "active"}]
    )
    findings = check_xstack_006(ecosystem=ecosystem, github_token="t")

    assert len(findings) == 1
    f = findings[0]
    assert f["rule_id"] == "XSTACK-006"
    assert f["severity"] == "ERROR"
    assert f["dimension"] == "cross_repo_coherence"
    assert "ghost-cog" in f["finding"]
    assert len(f["suggestion"]) >= 40


@respx.mock
def test_xstack_006_ignores_unregistered_repo_without_evaluator_yaml() -> None:
    """An unregistered repo that never opted in is not this rule's business."""
    _mock_org_repos(["evaluator-cog", "some-scratch-repo"])
    _mock_contents("some-scratch-repo", "evaluator.yaml", None)

    ecosystem = _ecosystem(
        [{"id": "evaluator-cog", "type": "pipeline-cog", "status": "active"}]
    )
    assert check_xstack_006(ecosystem=ecosystem, github_token="t") == []


@respx.mock
def test_xstack_006_follows_pagination() -> None:
    """A repo past the first page must still be enumerated.

    Missing page two would silently drop exactly the violations this rule
    exists to catch, so the pagination loop gets its own test: page one is
    a full 100 entries (all registered), page two carries the offender.
    """
    page_one = [{"name": f"repo-{i:03d}"} for i in range(100)]
    page_two = [{"name": "late-ghost-cog"}]

    route = respx.get(url__regex=rf"{_API}/orgs/{_ORG}/repos.*")
    route.side_effect = [
        httpx.Response(200, json=page_one),
        httpx.Response(200, json=page_two),
    ]
    _mock_contents("late-ghost-cog", "evaluator.yaml", "type: pipeline-cog\n")

    ecosystem = _ecosystem(
        [
            {"id": f"repo-{i:03d}", "type": "pipeline-cog", "status": "active"}
            for i in range(100)
        ]
    )
    findings = check_xstack_006(ecosystem=ecosystem, github_token="t")

    assert [f["rule_id"] for f in findings] == ["XSTACK-006"]
    assert "late-ghost-cog" in findings[0]["finding"]


@respx.mock
def test_xstack_006_network_failure_yields_checker_not_violations() -> None:
    """A 500 from the org listing must not read as "nothing is registered"."""
    _mock_org_repos([], status=500)

    ecosystem = _ecosystem(
        [{"id": "evaluator-cog", "type": "pipeline-cog", "status": "active"}]
    )
    findings = check_xstack_006(ecosystem=ecosystem, github_token="t")

    assert len(findings) == 1
    assert findings[0]["rule_id"] == "CHECKER"
    assert findings[0]["severity"] == "WARN"
    assert not any(f["rule_id"] == "XSTACK-006" for f in findings)


@respx.mock
def test_xstack_006_contents_failure_yields_checker_not_violations() -> None:
    """A non-404 error on the evaluator.yaml probe is also infrastructure."""
    _mock_org_repos(["ghost-cog"])
    respx.get(url=f"{_API}/repos/{_ORG}/ghost-cog/contents/evaluator.yaml").mock(
        return_value=httpx.Response(403, json={"message": "rate limited"})
    )

    findings = check_xstack_006(ecosystem=_ecosystem([]), github_token="t")

    assert [f["rule_id"] for f in findings] == ["CHECKER"]


# ---------------------------------------------------------------- XSTACK-008
#
# This check makes no HTTP calls at all — it reads the run's own download
# record — so unlike its neighbours above it needs no respx mocking.


def test_xstack_008_clean_run_reports_nothing() -> None:
    """Nothing 404'd, so there is nothing to say."""
    assert check_xstack_008(unresolved=[]) == []
    assert check_xstack_008(unresolved=None) == []
    assert check_xstack_008() == []


def test_xstack_008_reports_the_org_repo_and_branch_attempted() -> None:
    """The finding must say where it looked, not just that it failed."""
    findings = check_xstack_008(
        unresolved=[
            {
                "label": "mini-app-polis/website-astro-wcs",
                "url": "https://api.github.com/repos/mini-app-polis/website-astro-wcs/zipball/main",
            }
        ]
    )
    assert len(findings) == 1
    f = findings[0]
    assert f["rule_id"] == "XSTACK-008"
    assert f["severity"] == "ERROR"
    assert f["dimension"] == "cross_repo_coherence"
    assert "mini-app-polis/website-astro-wcs" in f["finding"]
    assert "main" in f["finding"]
    assert "ecosystem.yaml" in f["suggestion"]


def test_xstack_008_one_finding_per_unresolved_entry() -> None:
    findings = check_xstack_008(
        unresolved=[
            {"label": "o/a", "url": "https://api.github.com/repos/o/a/zipball/main"},
            {"label": "o/b", "url": "https://api.github.com/repos/o/b/zipball/dev"},
        ]
    )
    assert len(findings) == 2
    assert "dev" in findings[1]["finding"]


def test_xstack_008_survives_an_unparseable_url() -> None:
    """A malformed record still reports — degraded, never dropped."""
    findings = check_xstack_008(unresolved=[{"label": "o/c", "url": "not-a-url"}])
    assert len(findings) == 1
    assert "o/c" in findings[0]["finding"]
