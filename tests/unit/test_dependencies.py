"""XSTACK-007: a repo's pins of the fleet's libraries, judged per repo.

The resolver stands in for PyPI and npm, so these tests never touch the
network; the registry lookup itself is covered separately with respx.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from evaluator_cog.engine.deterministic import dependencies
from evaluator_cog.engine.deterministic.dependencies import (
    Published,
    check_xstack_007,
)

_ORG = "mini-app-polis"


def _resolver(table: dict[tuple[str, str], Published | None]):
    calls: list[tuple[str, str]] = []

    def resolve(registry: str, name: str) -> Published | None:
        calls.append((registry, name))
        return table.get((registry, name))

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


_UTILS = Published("5.13.1", f"{_ORG}/common-python-utils")


def _pyproject(tmp_path: Path, body: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(body)
    return tmp_path


def _consumer(deps: list[str], extra: str = "") -> str:
    return (
        f'[project]\nname = "consumer-cog"\ndependencies = {json.dumps(deps)}\n{extra}'
    )


def test_flags_two_minors_behind(tmp_path: Path) -> None:
    repo = _pyproject(tmp_path, _consumer(["miniapppolis-common-utils>=5.11.0,<6"]))
    resolve = _resolver({("pypi", "miniapppolis-common-utils"): _UTILS})
    findings = check_xstack_007(repo, resolve)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["rule_id"] == "XSTACK-007"
    assert finding["severity"] == "WARN"
    assert "2 minors behind" in finding["finding"]
    assert "mini-app-polis/common-python-utils" in finding["finding"]


def test_ignores_one_minor_or_a_patch_behind(tmp_path: Path) -> None:
    repo = _pyproject(
        tmp_path,
        _consumer(["miniapppolis-common-utils>=5.12.0", "other-lib>=5.13.0"]),
    )
    resolve = _resolver(
        {
            ("pypi", "miniapppolis-common-utils"): _UTILS,
            ("pypi", "other-lib"): Published("5.13.9", f"{_ORG}/other-lib"),
        }
    )
    assert check_xstack_007(repo, resolve) == []


def test_flags_a_major_behind(tmp_path: Path) -> None:
    repo = _pyproject(tmp_path, _consumer(["miniapppolis-common-utils>=4.20"]))
    resolve = _resolver({("pypi", "miniapppolis-common-utils"): _UTILS})
    findings = check_xstack_007(repo, resolve)
    assert len(findings) == 1
    assert "full major behind" in findings[0]["finding"]


def test_skips_packages_outside_the_org(tmp_path: Path) -> None:
    repo = _pyproject(tmp_path, _consumer(["httpx>=0.20"]))
    resolve = _resolver({("pypi", "httpx"): Published("0.28.1", "")})
    assert check_xstack_007(repo, resolve) == []


def test_skips_branch_and_sha_pins(tmp_path: Path) -> None:
    extra = (
        "\n[tool.uv.sources]\n"
        'lib-a = { git = "https://github.com/mini-app-polis/lib-a", branch = "main" }\n'
        'lib-b = { git = "https://github.com/mini-app-polis/lib-b", rev = "'
        + "a" * 40
        + '" }\n'
    )
    repo = _pyproject(tmp_path, _consumer(["lib-a", "lib-b"], extra))
    resolve = _resolver(
        {
            ("pypi", "lib-a"): Published("3.0.0", f"{_ORG}/lib-a"),
            ("pypi", "lib-b"): Published("3.0.0", f"{_ORG}/lib-b"),
        }
    )
    assert check_xstack_007(repo, resolve) == []
    assert resolve.calls == []  # type: ignore[attr-defined]


def test_skips_the_repos_own_package(tmp_path: Path) -> None:
    body = (
        '[project]\nname = "miniapppolis-common-utils"\n'
        'dependencies = ["miniapppolis-common-utils[google]>=1.0"]\n'
    )
    repo = _pyproject(tmp_path, body)
    resolve = _resolver({("pypi", "miniapppolis-common-utils"): _UTILS})
    assert check_xstack_007(repo, resolve) == []


def test_uv_source_tag_is_the_pin(tmp_path: Path) -> None:
    extra = (
        "\n[tool.uv.sources]\n"
        'common-python-utils = { git = "https://github.com/mini-app-polis/'
        'common-python-utils.git", tag = "v5.10.0" }\n'
    )
    repo = _pyproject(tmp_path, _consumer(["common-python-utils"], extra))
    resolve = _resolver(
        {("pypi", "common-python-utils"): Published("5.13.1", _UTILS.org_repo)}
    )
    findings = check_xstack_007(repo, resolve)
    assert len(findings) == 1
    assert "[tool.uv.sources].common-python-utils" in findings[0]["finding"]
    assert "v5.10.0" in findings[0]["finding"]


def test_optional_dependencies_are_read(tmp_path: Path) -> None:
    extra = (
        '\n[project.optional-dependencies]\ndev = ["miniapppolis-common-utils>=5.1"]\n'
    )
    repo = _pyproject(tmp_path, _consumer([], extra))
    resolve = _resolver({("pypi", "miniapppolis-common-utils"): _UTILS})
    findings = check_xstack_007(repo, resolve)
    assert len(findings) == 1
    assert "optional-dependencies.dev" in findings[0]["finding"]


def test_reads_nested_package_json_and_skips_node_modules(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "root", "private": True})
    )
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {"@mini-app-polis/ui": "^1.2.0"}})
    )
    vendored = tmp_path / "node_modules" / "x"
    vendored.mkdir(parents=True)
    (vendored / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"@mini-app-polis/ui": "^0.1.0"}})
    )
    resolve = _resolver(
        {("npm", "@mini-app-polis/ui"): Published("1.6.0", f"{_ORG}/ui")}
    )
    findings = check_xstack_007(tmp_path, resolve)
    assert len(findings) == 1
    assert "apps/web/package.json dependencies" in findings[0]["finding"]


def test_silent_when_the_registry_cannot_answer(tmp_path: Path) -> None:
    repo = _pyproject(tmp_path, _consumer(["miniapppolis-common-utils>=1.0"]))
    assert check_xstack_007(repo, _resolver({})) == []


def test_no_manifests_no_findings(tmp_path: Path) -> None:
    assert check_xstack_007(tmp_path, _resolver({})) == []


# ---------------------------------------------------------------------------
# The registry lookup
# ---------------------------------------------------------------------------


@respx.mock
def test_pypi_project_urls_mark_an_org_package() -> None:
    dependencies._cache.clear()
    respx.get("https://pypi.org/pypi/miniapppolis-common-utils/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "info": {
                    "version": "5.13.1",
                    "project_urls": {
                        "Source": "https://github.com/mini-app-polis/common-python-utils"
                    },
                }
            },
        )
    )
    published = dependencies.registry_resolver("pypi", "miniapppolis-common-utils")
    assert published == Published("5.13.1", "mini-app-polis/common-python-utils")


@respx.mock
def test_npm_repository_url_marks_an_org_package() -> None:
    dependencies._cache.clear()
    respx.get("https://registry.npmjs.org/@mini-app-polis%2Fui/latest").mock(
        return_value=httpx.Response(
            200,
            json={
                "version": "1.6.0",
                "repository": {
                    "type": "git",
                    "url": "git+https://github.com/mini-app-polis/ui.git",
                },
            },
        )
    )
    assert dependencies.registry_resolver("npm", "@mini-app-polis/ui") == Published(
        "1.6.0", "mini-app-polis/ui"
    )


@respx.mock
def test_registry_failure_is_none_and_not_cached() -> None:
    dependencies._cache.clear()
    route = respx.get("https://pypi.org/pypi/flaky/json").mock(
        return_value=httpx.Response(503)
    )
    assert dependencies.registry_resolver("pypi", "flaky") is None
    assert dependencies.registry_resolver("pypi", "flaky") is None
    assert route.call_count == 2
