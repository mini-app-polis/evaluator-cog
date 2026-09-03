"""Deterministic checks: CD-016 (serve_with_retry wrapping), CD-020 (lockfile release discipline)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evaluator_cog.engine.deterministic import packaging
from evaluator_cog.engine.deterministic.packaging import check_cd_016, check_cd_020


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _texts(findings: list[dict[str, Any]]) -> str:
    return " ".join(f["finding"] for f in findings)


# --- CD-016 fixtures ---------------------------------------------------------


_WRAPPED_MAIN = (
    "from mini_app_polis.serve_resilience import serve_with_retry\n"
    "from prefect.flows import flow as prefect_flow\n"
    "\n"
    "def main() -> None:\n"
    "    deployment = prefect_flow.from_source(source='.', entrypoint='f.py:f')\n"
    '    serve_with_retry(deployment.to_deployment(name="x"), repo="demo-cog")\n'
)

_DIRECT_MAIN = (
    "from prefect import serve\n"
    "from prefect.flows import flow as prefect_flow\n"
    "\n"
    "def main() -> None:\n"
    "    deployment = prefect_flow.from_source(source='.', entrypoint='f.py:f')\n"
    '    serve(deployment.to_deployment(name="x"))\n'
)


def _pyproject(
    *,
    common_utils: bool = True,
    specifier: str = "",
    source_ref: str = 'rev = "v4.0.0"',
    optional_specifier: str = "",
    include_sources: bool = True,
) -> str:
    dep = f'    "common-python-utils{specifier}",\n' if common_utils else ""
    optional = (
        f'dev = ["pytest>=8.0", "common-python-utils{optional_specifier}"]\n'
        if optional_specifier
        else 'dev = ["pytest>=8.0"]\n'
    )
    sources = (
        "[tool.uv.sources]\n"
        "common-python-utils = { git = "
        '"https://github.com/mini-app-polis/common-python-utils.git", '
        f"{source_ref} }}\n"
        if include_sources
        else ""
    )
    return (
        "[project]\n"
        'name = "demo-cog"\n'
        'version = "1.2.3"\n'
        "dependencies = [\n"
        f"{dep}"
        '    "prefect>=3.0,<4.0",\n'
        "]\n"
        "\n"
        "[project.optional-dependencies]\n"
        f"{optional}"
        "\n"
        f"{sources}"
    )


def _railway(start_command: str) -> str:
    return json.dumps(
        {
            "deploy": {
                "startCommand": start_command,
                "restartPolicyType": "ON_FAILURE",
                "restartPolicyMaxRetries": 10,
            }
        }
    )


def _compliant_serving_repo(repo: Path) -> None:
    _write(repo, "railway.json", _railway("python -m demo_cog.main"))
    _write(repo, "pyproject.toml", _pyproject())
    _write(repo, "src/demo_cog/__init__.py", "")
    _write(repo, "src/demo_cog/main.py", _WRAPPED_MAIN)


# --- CD-016: the applicability gate -----------------------------------------


def test_cd016_returns_empty_on_repo_with_no_serve_call(tmp_path: Path) -> None:
    """The gate: a trigger-cog with a plain asyncio loop has nothing to register.

    trigger-cog is in CD-016's applies_to because some trigger cogs do
    serve Prefect flows, but one that polls in an asyncio loop with no
    @flow has no serve() entry point at all. The rule has no subject in
    such a repo, and flagging it would be a false ERROR on correct code.
    """
    _write(tmp_path, "railway.json", _railway("python -m demo_cog.main"))
    _write(tmp_path, "pyproject.toml", _pyproject(common_utils=False))
    _write(
        tmp_path,
        "src/demo_cog/main.py",
        "import asyncio\n"
        "\n"
        "async def loop() -> None:\n"
        "    while True:\n"
        "        await asyncio.sleep(30)\n"
        "\n"
        "def main() -> None:\n"
        "    asyncio.run(loop())\n",
    )
    assert check_cd_016(tmp_path) == []


def test_cd016_gate_ignores_serve_in_comments_and_docstrings(tmp_path: Path) -> None:
    """A `serve(` in prose is not a call — the gate is AST-based, not substring."""
    _write(tmp_path, "pyproject.toml", _pyproject(common_utils=False))
    _write(
        tmp_path,
        "src/demo_cog/main.py",
        '"""This cog does not use prefect.serve( at all."""\n'
        "\n"
        "# historical note: we used to call serve( here\n"
        'DOC = "flow.serve( is the pattern other cogs use"\n'
        "\n"
        "def main() -> None:\n"
        "    return None\n",
    )
    assert check_cd_016(tmp_path) == []


def test_cd016_gate_ignores_serve_calls_in_tests(tmp_path: Path) -> None:
    """A serve() call in a test fixture is not a deployed entry point."""
    _write(tmp_path, "pyproject.toml", _pyproject(common_utils=False))
    _write(tmp_path, "src/demo_cog/main.py", "def main() -> None:\n    return None\n")
    _write(tmp_path, "tests/test_serve.py", _DIRECT_MAIN)
    assert check_cd_016(tmp_path) == []


# --- CD-016: the rule proper -------------------------------------------------


def test_cd016_passes_on_wrapped_entry_point(tmp_path: Path) -> None:
    _compliant_serving_repo(tmp_path)
    assert check_cd_016(tmp_path) == []


def test_cd016_flags_bare_prefect_serve_import_call(tmp_path: Path) -> None:
    _compliant_serving_repo(tmp_path)
    _write(tmp_path, "src/demo_cog/main.py", _DIRECT_MAIN)
    findings = check_cd_016(tmp_path)
    assert findings
    assert all(f["rule_id"] == "CD-016" for f in findings)
    assert all(f["severity"] == "ERROR" for f in findings)
    assert all(f["dimension"] == "cd_readiness" for f in findings)
    assert "serve_with_retry" in _texts(findings)
    assert all(len(f["suggestion"]) >= 40 for f in findings)


def test_cd016_flags_qualified_prefect_serve(tmp_path: Path) -> None:
    _compliant_serving_repo(tmp_path)
    _write(
        tmp_path,
        "src/demo_cog/main.py",
        "import prefect\n\ndef main() -> None:\n    prefect.serve()\n",
    )
    findings = check_cd_016(tmp_path)
    assert any("prefect.serve()" in f["finding"] for f in findings)


def test_cd016_flags_flow_serve_attribute_call(tmp_path: Path) -> None:
    _compliant_serving_repo(tmp_path)
    _write(
        tmp_path,
        "src/demo_cog/main.py",
        "from prefect import flow\n"
        "\n"
        "@flow\n"
        "def my_flow() -> None:\n"
        "    return None\n"
        "\n"
        "def main() -> None:\n"
        "    my_flow.serve(name='x')\n",
    )
    findings = check_cd_016(tmp_path)
    assert any("my_flow.serve()" in f["finding"] for f in findings)


def test_cd016_flags_missing_repo_kwarg(tmp_path: Path) -> None:
    _compliant_serving_repo(tmp_path)
    _write(
        tmp_path,
        "src/demo_cog/main.py",
        "from mini_app_polis.serve_resilience import serve_with_retry\n"
        "\n"
        "def main() -> None:\n"
        '    serve_with_retry("deployment")\n',
    )
    findings = check_cd_016(tmp_path)
    assert len(findings) == 1
    assert "repo=" in findings[0]["finding"]


def test_cd016_accepts_kwargs_unpacking_for_repo(tmp_path: Path) -> None:
    """A ** unpacking may carry repo=; an ERROR false positive is the costlier error."""
    _compliant_serving_repo(tmp_path)
    _write(
        tmp_path,
        "src/demo_cog/main.py",
        "from mini_app_polis.serve_resilience import serve_with_retry\n"
        "\n"
        "OPTS = {'repo': 'demo-cog'}\n"
        "\n"
        "def main() -> None:\n"
        '    serve_with_retry("deployment", **OPTS)\n',
    )
    assert check_cd_016(tmp_path) == []


def test_cd016_flags_local_helper_not_from_shared_library(tmp_path: Path) -> None:
    _compliant_serving_repo(tmp_path)
    _write(
        tmp_path,
        "src/demo_cog/main.py",
        "from demo_cog.local_retry import serve_with_retry\n"
        "\n"
        "def main() -> None:\n"
        '    serve_with_retry("deployment", repo="demo-cog")\n',
    )
    findings = check_cd_016(tmp_path)
    assert len(findings) == 1
    assert "mini_app_polis.serve_resilience" in findings[0]["finding"]


def test_cd016_flags_direct_serve_alongside_wrapped_call(tmp_path: Path) -> None:
    _compliant_serving_repo(tmp_path)
    _write(
        tmp_path,
        "src/demo_cog/main.py",
        "from mini_app_polis.serve_resilience import serve_with_retry\n"
        "from prefect import serve\n"
        "\n"
        "def main() -> None:\n"
        '    serve_with_retry("a", repo="demo-cog")\n'
        '    serve("b")\n',
    )
    findings = check_cd_016(tmp_path)
    assert len(findings) == 1
    assert "also registers deployments directly" in findings[0]["finding"]


def test_cd016_flags_missing_common_python_utils_dependency(tmp_path: Path) -> None:
    _compliant_serving_repo(tmp_path)
    _write(tmp_path, "pyproject.toml", _pyproject(common_utils=False))
    findings = check_cd_016(tmp_path)
    assert len(findings) == 1
    assert "common-python-utils" in findings[0]["finding"]


def test_cd016_falls_back_to_src_pkg_main_without_railway_json(tmp_path: Path) -> None:
    _compliant_serving_repo(tmp_path)
    (tmp_path / "railway.json").unlink()
    assert check_cd_016(tmp_path) == []
    _write(tmp_path, "src/demo_cog/main.py", _DIRECT_MAIN)
    findings = check_cd_016(tmp_path)
    assert any(
        "src/demo_cog/main.py" in f["finding"].replace("\\", "/") for f in findings
    )


def test_cd016_start_command_wins_over_main_py_convention(tmp_path: Path) -> None:
    """railway.json names the deployed module; a stale main.py is not the subject."""
    _compliant_serving_repo(tmp_path)
    _write(tmp_path, "railway.json", _railway("python -m demo_cog.worker"))
    _write(tmp_path, "src/demo_cog/worker.py", _WRAPPED_MAIN)
    _write(tmp_path, "src/demo_cog/main.py", _DIRECT_MAIN)
    assert check_cd_016(tmp_path) == []


def test_cd016_resolves_script_path_start_command(tmp_path: Path) -> None:
    _compliant_serving_repo(tmp_path)
    _write(tmp_path, "railway.json", _railway("uv run python src/demo_cog/boot.py"))
    _write(tmp_path, "src/demo_cog/boot.py", _DIRECT_MAIN)
    findings = check_cd_016(tmp_path)
    assert any(
        "src/demo_cog/boot.py" in f["finding"].replace("\\", "/") for f in findings
    )


def test_cd016_flags_unresolvable_entry_point(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", _pyproject())
    _write(tmp_path, "railway.json", _railway("python -m demo_cog.nowhere"))
    _write(tmp_path, "src/demo_cog/flows.py", _DIRECT_MAIN)
    findings = check_cd_016(tmp_path)
    assert len(findings) == 1
    assert "entry point could not be located" in findings[0]["finding"]


# --- CD-020 fixtures ---------------------------------------------------------


def _releaserc(*, prepare_cmd: str | None, assets: list[str] | None) -> str:
    plugins: list[Any] = ["@semantic-release/commit-analyzer"]
    if prepare_cmd is not None:
        plugins.append(["@semantic-release/exec", {"prepareCmd": prepare_cmd}])
    if assets is not None:
        plugins.append(["@semantic-release/git", {"assets": assets}])
    return json.dumps({"branches": ["main"], "plugins": plugins})


def _uv_managed_repo(repo: Path, *, pyproject: str | None = None) -> None:
    _write(
        repo,
        "uv.lock",
        'version = 1\n\n[[package]]\nname = "demo-cog"\nversion = "1.2.3"\n',
    )
    _write(
        repo,
        ".releaserc.json",
        _releaserc(
            prepare_cmd="uv version ${nextRelease.version} && uv lock",
            assets=["CHANGELOG.md", "pyproject.toml", "uv.lock"],
        ),
    )
    _write(repo, "pyproject.toml", pyproject if pyproject is not None else _pyproject())


def _disable_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence sub-check (2) so the other three are tested in isolation."""
    monkeypatch.setattr(packaging.shutil, "which", lambda _name: None)


# --- CD-020: exemption and the happy path ------------------------------------


def test_cd020_returns_empty_without_uv_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No uv.lock means the repo is not uv-managed — PY-001 is the rule for that."""
    _disable_uv(monkeypatch)
    _write(tmp_path, "pyproject.toml", _pyproject(source_ref='rev = "main"'))
    _write(tmp_path, ".releaserc.json", _releaserc(prepare_cmd=None, assets=None))
    assert check_cd_020(tmp_path) == []


def test_cd020_passes_on_compliant_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path)
    assert check_cd_020(tmp_path) == []


# --- CD-020 (1): the release relocks and commits the lock --------------------


def test_cd020_flags_absent_releaserc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path)
    (tmp_path / ".releaserc.json").unlink()
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert ".releaserc.json is absent" in findings[0]["finding"]
    assert findings[0]["severity"] == "ERROR"
    assert findings[0]["dimension"] == "cd_readiness"
    assert len(findings[0]["suggestion"]) >= 40


def test_cd020_flags_prepare_cmd_without_uv_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path)
    _write(
        tmp_path,
        ".releaserc.json",
        _releaserc(
            prepare_cmd="uv version ${nextRelease.version}",
            assets=["CHANGELOG.md", "uv.lock"],
        ),
    )
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "prepareCmd" in findings[0]["finding"]
    assert "re-drifts" in findings[0]["finding"]


def test_cd020_flags_git_assets_without_uv_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path)
    _write(
        tmp_path,
        ".releaserc.json",
        _releaserc(
            prepare_cmd="uv version ${nextRelease.version} && uv lock",
            assets=["CHANGELOG.md", "pyproject.toml"],
        ),
    )
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "assets" in findings[0]["finding"]


def test_cd020_flags_unparseable_releaserc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path)
    _write(tmp_path, ".releaserc.json", "{not json,")
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "does not parse as JSON" in findings[0]["finding"]


# --- CD-020 (2): uv lock --check, and its guards ------------------------------


class _Result:
    """Stands in for `uv lock --check`.

    The default stderr is uv's real wording, verified against uv 0.9 on
    a deliberately staled lockfile. The invented phrasing this carried
    before ("The lockfile is out of date") let the check pass its tests
    while reading any non-zero exit as staleness — including the
    environment failures uv also exits non-zero for.
    """

    def __init__(self, returncode: int, stderr: str | None = None) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = (
            "error: The lockfile at `uv.lock` needs to be updated, but "
            "`--check` was provided."
            if stderr is None
            else stderr
        )


def test_cd020_flags_stale_lock_and_names_both_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _uv_managed_repo(tmp_path)
    _write(
        tmp_path,
        "uv.lock",
        'version = 1\n\n[[package]]\nname = "demo-cog"\nversion = "1.2.2"\n',
    )
    monkeypatch.setattr(packaging.shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(
        packaging.subprocess, "run", lambda *_a, **_k: _Result(returncode=1)
    )
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "1.2.3" in findings[0]["finding"]
    assert "1.2.2" in findings[0]["finding"]


def test_cd020_skips_lock_check_when_uv_binary_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool missing from the evaluator's environment is not the repo's violation."""
    _uv_managed_repo(tmp_path)
    monkeypatch.setattr(packaging.shutil, "which", lambda _name: None)

    def _explode(*_a: Any, **_k: Any) -> None:
        raise AssertionError("subprocess must not run when uv is unavailable")

    monkeypatch.setattr(packaging.subprocess, "run", _explode)
    assert check_cd_020(tmp_path) == []


def test_cd020_lock_check_timeout_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _uv_managed_repo(tmp_path)
    monkeypatch.setattr(packaging.shutil, "which", lambda _name: "/usr/bin/uv")

    def _timeout(*_a: Any, **_k: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="uv lock --check", timeout=60)

    monkeypatch.setattr(packaging.subprocess, "run", _timeout)
    assert check_cd_020(tmp_path) == []


def test_cd020_lock_check_passes_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _uv_managed_repo(tmp_path)
    monkeypatch.setattr(packaging.shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(
        packaging.subprocess, "run", lambda *_a, **_k: _Result(returncode=0)
    )
    assert check_cd_020(tmp_path) == []


# --- CD-020 (3): specifier vs. source ref ------------------------------------


def test_cd020_does_not_flag_bare_requirement_without_specifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare name says nothing the git source can contradict — never flagged."""
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path, pyproject=_pyproject(specifier=""))
    assert check_cd_020(tmp_path) == []


def test_cd020_flags_specifier_disagreeing_with_rev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path, pyproject=_pyproject(specifier=">=3.0"))
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "project.dependencies" in findings[0]["finding"]
    assert "v4.0.0" in findings[0]["finding"]


def test_cd020_accepts_specifier_matching_the_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path, pyproject=_pyproject(specifier="==4.0.0"))
    assert check_cd_020(tmp_path) == []


def test_cd020_flags_specifier_in_optional_dependency_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(
        tmp_path,
        pyproject=_pyproject(specifier="", optional_specifier="~=3.1"),
    )
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "optional-dependencies.dev" in findings[0]["finding"]


def test_cd020_ignores_environment_marker_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(
        tmp_path,
        pyproject=_pyproject(specifier='; python_version >= "3.11"'),
    )
    assert check_cd_020(tmp_path) == []


# --- CD-020 (4): the git ref must be a version tag ---------------------------


def test_cd020_accepts_rev_holding_a_version_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path, pyproject=_pyproject(source_ref='rev = "v4.0.0"'))
    assert check_cd_020(tmp_path) == []


def test_cd020_accepts_tag_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path, pyproject=_pyproject(source_ref='tag = "v4.0.0"'))
    assert check_cd_020(tmp_path) == []


def test_cd020_flags_rev_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path, pyproject=_pyproject(source_ref='rev = "main"'))
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "moving branch" in findings[0]["finding"]
    assert findings[0]["severity"] == "ERROR"


def test_cd020_flags_rev_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path, pyproject=_pyproject(source_ref='rev = "master"'))
    assert len(check_cd_020(tmp_path)) == 1


def test_cd020_flags_forty_character_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "a" * 8 + "1234567890" * 3 + "bc"
    assert len(sha) == 40
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path, pyproject=_pyproject(source_ref=f'rev = "{sha}"'))
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "commit SHA" in findings[0]["finding"]


def test_cd020_flags_all_digit_forty_character_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digits-only SHA is version-tag-shaped by regex — the SHA test runs first."""
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path, pyproject=_pyproject(source_ref=f'rev = "{"1" * 40}"'))
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "commit SHA" in findings[0]["finding"]


def test_cd020_flags_branch_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(
        tmp_path, pyproject=_pyproject(source_ref='branch = "feature/new-api"')
    )
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "re-resolves on every lock" in findings[0]["finding"]


def test_cd020_flags_git_source_with_no_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    no_ref = (
        "[project]\n"
        'name = "demo-cog"\n'
        'version = "1.2.3"\n'
        'dependencies = ["common-python-utils"]\n'
        "\n"
        "[tool.uv.sources]\n"
        "common-python-utils = { git = "
        '"https://github.com/mini-app-polis/common-python-utils.git" }\n'
    )
    _uv_managed_repo(tmp_path, pyproject=no_ref)
    findings = check_cd_020(tmp_path)
    assert len(findings) == 1
    assert "floats on the default branch" in findings[0]["finding"]


def test_cd020_ignores_repo_without_uv_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_uv(monkeypatch)
    _uv_managed_repo(tmp_path, pyproject=_pyproject(include_sources=False))
    assert check_cd_020(tmp_path) == []


# --- CD-020: a failed subprocess is not a stale lockfile ---------------------


def _uv_result(returncode: int, stderr: str = "", stdout: str = ""):
    from types import SimpleNamespace

    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _uv_repo(tmp_path: Path) -> Path:
    (tmp_path / "uv.lock").write_text('version = 1\n[[package]]\nname = "x"\n')
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1.0.0"\ndependencies = []\n'
    )
    (tmp_path / ".releaserc.json").write_text(
        '{"plugins": [["@semantic-release/exec", {"prepareCmd": "uv lock"}],'
        ' ["@semantic-release/git", {"assets": ["uv.lock"]}]]}'
    )
    return tmp_path


def test_cd020_does_not_call_a_broken_uv_a_stale_lockfile(tmp_path: Path) -> None:
    """uv exits non-zero when it cannot run, not only when the lock is stale.

    On identity, `uv lock --check` failed reading a stale .venv and the
    check reported the lockfile as out of date — while the same tree,
    copied without that .venv, checked clean. Environment failures are
    not the repository's violation.
    """
    from unittest.mock import patch

    from evaluator_cog.engine.deterministic import check_cd_020

    repo = _uv_repo(tmp_path)
    broken = _uv_result(
        2, stderr="error: Failed to query Python interpreter\n  Permission denied"
    )
    with (
        patch("shutil.which", return_value="/usr/bin/uv"),
        patch("subprocess.run", return_value=broken),
    ):
        findings = check_cd_020(repo)
    stale = [f for f in findings if "out of date" in f["finding"]]
    assert stale == [], f"a broken uv was read as a stale lockfile: {stale}"


def test_cd020_still_flags_a_genuinely_stale_lockfile(tmp_path: Path) -> None:
    """The true positive: uv's own message, verified against real uv output."""
    from unittest.mock import patch

    from evaluator_cog.engine.deterministic import check_cd_020

    repo = _uv_repo(tmp_path)
    stale = _uv_result(
        1,
        stderr=(
            "error: The lockfile at `uv.lock` needs to be updated, but "
            "`--check` was provided.\n\nhint: To update the lockfile, run `uv lock`."
        ),
    )
    with (
        patch("shutil.which", return_value="/usr/bin/uv"),
        patch("subprocess.run", return_value=stale),
    ):
        findings = check_cd_020(repo)
    assert any("out of date" in f["finding"] for f in findings)
