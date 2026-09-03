"""Tests for the cross-repo coherence checks (XSTACK-006, XSTACK-007).

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
import pytest
import respx

from evaluator_cog.engine.deterministic.crossrepo import (
    check_xstack_006,
    check_xstack_007,
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


def _mock_release(repo: str, tag: str | None) -> None:
    url = f"{_API}/repos/{_ORG}/{repo}/releases/latest"
    if tag is None:
        respx.get(url=url).mock(return_value=httpx.Response(404, json={}))
    else:
        respx.get(url=url).mock(
            return_value=httpx.Response(200, json={"tag_name": tag})
        )


def _pyproject_with_rev(rev: str) -> str:
    return (
        "[project]\n"
        'name = "consumer"\n'
        'dependencies = ["common-python-utils"]\n'
        "\n"
        "[tool.uv.sources]\n"
        'common-python-utils = { git = "https://github.com/'
        'mini-app-polis/common-python-utils.git", rev = "' + rev + '" }\n'
    )


_LIB_SERVICE = {
    "id": "common-python-utils",
    "type": "shared-library",
    "status": "active",
    "language": "python",
}
_CONSUMER_SERVICE = {
    "id": "consumer-cog",
    "type": "pipeline-cog",
    "status": "active",
    "language": "python",
}


# ---------------------------------------------------------------------------
# XSTACK-006
# ---------------------------------------------------------------------------


@respx.mock
def test_xstack_006_no_ecosystem_returns_empty() -> None:
    """No registry means no comparison — and no network call at all.

    Mirrors check_mono_003's guard. respx is active with zero routes, so
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
def test_xstack_006_registers_monorepo_repo_field() -> None:
    """A monorepo is registered under monorepos[].repo, not services[].id."""
    _mock_org_repos(["deejaytools-com"])
    ecosystem = {
        "services": [],
        "monorepos": [{"id": "deejaytools-com", "repo": "deejaytools-com"}],
    }
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


# ---------------------------------------------------------------------------
# XSTACK-007
# ---------------------------------------------------------------------------


@respx.mock
def test_xstack_007_no_ecosystem_returns_empty() -> None:
    assert check_xstack_007(ecosystem=None, github_token="t") == []


@respx.mock
def test_xstack_007_no_tracked_libraries_returns_empty() -> None:
    """Nothing is typed shared-library, so there is nothing to track."""
    ecosystem = _ecosystem([_CONSUMER_SERVICE])
    assert check_xstack_007(ecosystem=ecosystem, github_token="t") == []


def _run_007_with_pin(rev: str, latest_tag: str) -> list[dict]:
    """Fleet of one library + one consumer; consumer pins ``rev``."""
    _mock_release("common-python-utils", latest_tag)
    _mock_contents("common-python-utils", "pyproject.toml", None)
    _mock_contents("common-python-utils", "package.json", None)
    _mock_contents("consumer-cog", "pyproject.toml", _pyproject_with_rev(rev))
    _mock_contents("consumer-cog", "package.json", None)
    ecosystem = _ecosystem([_LIB_SERVICE, _CONSUMER_SERVICE])
    return check_xstack_007(ecosystem=ecosystem, github_token="t")


@respx.mock
def test_xstack_007_clean_fleet_returns_empty() -> None:
    assert _run_007_with_pin("v4.1.0", "v4.1.0") == []


@respx.mock
def test_xstack_007_flags_two_minors_behind() -> None:
    findings = _run_007_with_pin("v4.1.0", "v4.3.0")

    assert len(findings) == 1
    f = findings[0]
    assert f["rule_id"] == "XSTACK-007"
    assert f["severity"] == "WARN"
    assert f["dimension"] == "cross_repo_coherence"
    # The finding must name the repo, the library, and both versions.
    assert "consumer-cog" in f["finding"]
    assert "common-python-utils" in f["finding"]
    assert "v4.1.0" in f["finding"]
    assert "v4.3.0" in f["finding"]
    assert len(f["suggestion"]) >= 40


@respx.mock
def test_xstack_007_one_minor_behind_is_not_flagged() -> None:
    assert _run_007_with_pin("v4.1.0", "v4.2.0") == []


@respx.mock
def test_xstack_007_patch_behind_is_not_flagged() -> None:
    """Compare minors only — an un-taken patch release is not staleness."""
    assert _run_007_with_pin("v4.1.0", "v4.1.9") == []


@respx.mock
def test_xstack_007_flags_major_skew() -> None:
    findings = _run_007_with_pin("v3.9.0", "v5.0.0")
    assert [f["rule_id"] for f in findings] == ["XSTACK-007"]
    assert "major" in findings[0]["finding"]


@pytest.mark.parametrize(
    "rev",
    [
        "main",
        "develop",
        "a" * 40,
        "0123456789abcdef0123456789abcdef01234567",
    ],
)
@respx.mock
def test_xstack_007_skips_branch_and_sha_pins(rev: str) -> None:
    """Branch and bare-SHA pins are CD-020's finding, not this rule's.

    Skipping rather than flagging is what keeps one defect from being
    charged twice across two rules.
    """
    assert _run_007_with_pin(rev, "v9.9.0") == []


@respx.mock
def test_xstack_007_reads_typescript_package_json() -> None:
    _mock_release("common-typescript-utils", "v4.4.0")
    _mock_contents("common-typescript-utils", "pyproject.toml", None)
    _mock_contents("common-typescript-utils", "package.json", None)
    _mock_contents("web-app", "pyproject.toml", None)
    _mock_contents(
        "web-app",
        "package.json",
        '{"dependencies": {"@mini-app-polis/common-typescript-utils": "^4.1.0"}}',
    )

    ecosystem = _ecosystem(
        [
            {
                "id": "common-typescript-utils",
                "type": "shared-library",
                "status": "active",
                "language": "typescript",
            },
            {"id": "web-app", "type": "react-app", "status": "active"},
        ]
    )
    findings = check_xstack_007(ecosystem=ecosystem, github_token="t")

    assert [f["rule_id"] for f in findings] == ["XSTACK-007"]
    assert "package.json dependencies" in findings[0]["finding"]


@respx.mock
def test_xstack_007_library_without_release_is_skipped() -> None:
    """No published release means no baseline, so no consumer is stale."""
    _mock_release("common-python-utils", None)

    ecosystem = _ecosystem([_LIB_SERVICE, _CONSUMER_SERVICE])
    assert check_xstack_007(ecosystem=ecosystem, github_token="t") == []


@respx.mock
def test_xstack_007_release_failure_yields_checker_not_violations() -> None:
    respx.get(url=f"{_API}/repos/{_ORG}/common-python-utils/releases/latest").mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )

    ecosystem = _ecosystem([_LIB_SERVICE, _CONSUMER_SERVICE])
    findings = check_xstack_007(ecosystem=ecosystem, github_token="t")

    assert len(findings) == 1
    assert findings[0]["rule_id"] == "CHECKER"
    assert findings[0]["severity"] == "WARN"
    assert not any(f["rule_id"] == "XSTACK-007" for f in findings)


@respx.mock
def test_xstack_007_manifest_failure_yields_checker_not_violations() -> None:
    """A rate-limited manifest read must not look like a fleet-wide clean run
    nor like a violation — it is a CHECKER."""
    _mock_release("common-python-utils", "v9.9.0")
    _mock_contents("common-python-utils", "pyproject.toml", None)
    _mock_contents("common-python-utils", "package.json", None)
    respx.get(url=f"{_API}/repos/{_ORG}/consumer-cog/contents/pyproject.toml").mock(
        return_value=httpx.Response(403, json={"message": "rate limited"})
    )

    ecosystem = _ecosystem([_LIB_SERVICE, _CONSUMER_SERVICE])
    findings = check_xstack_007(ecosystem=ecosystem, github_token="t")

    assert [f["rule_id"] for f in findings] == ["CHECKER"]
