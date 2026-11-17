"""Deterministic checks for the canonical pipeline: CD-041, CD-042, CD-043.

The pipeline's current major is never a literal in the check: it is read
from ``pipeline.yaml`` in mini-app-polis/.github. These tests serve that
file from memory and move the major between them, so a pin that passes
at one major fails at the next.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from evaluator_cog.engine.deterministic import (
    check_canonical_ci_job_names,
    check_cd_041,
    check_cd_042,
    check_cd_043,
    delivery,
)

_REAL_FETCH = delivery._fetch_pipeline_config
_MAJOR = 5
_PIPELINE = "mini-app-polis/.github/.github/workflows/pipeline.yml"


@pytest.fixture(autouse=True)
def _major(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        delivery, "_fetch_pipeline_config", lambda: {"current_major": _MAJOR}
    )


def _write(repo: Path, rel: str, body: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return repo


def _caller(ref: str = f"@v{_MAJOR}", extra: str = "") -> str:
    return (
        "on:\n  push:\n  pull_request:\n"
        "jobs:\n"
        "  pipeline:\n"
        f"    uses: {_PIPELINE}{ref}\n"
        "    with:\n      profile: pipeline-cog\n"
        "    secrets: inherit\n" + extra
    )


_PROMOTE = (
    "on:\n  push:\n    branches: [dev]\n"
    "jobs:\n  promote:\n"
    "    uses: mini-app-polis/.github/.github/workflows/promote.yml@v3\n"
)
_AGENT = (
    "on:\n  workflow_dispatch:\n"
    "jobs:\n  agent:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - run: echo hi\n"
)


def _passing_repo(tmp_path: Path) -> Path:
    """A thin caller with an after-pipeline job and branch-flow workflows."""
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _caller(
            extra=(
                "  smoke:\n    needs: pipeline\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - run: make smoke\n"
            )
        ),
    )
    _write(tmp_path, ".github/workflows/promote.yml", _PROMOTE)
    _write(tmp_path, ".github/workflows/agent.yml", _AGENT)
    _write(
        tmp_path,
        ".railway/railway.ts",
        'const api = service("api", {\n'
        '  source: github("mini-app-polis/api", {}),\n'
        "});\n",
    )
    return tmp_path


# --- a passing repository ----------------------------------------------------


def test_passing_repo_has_no_canonical_pipeline_findings(tmp_path: Path) -> None:
    repo = _passing_repo(tmp_path)
    assert check_cd_041(repo) == []
    assert check_cd_042(repo) == []
    assert check_cd_043(repo) == []


def test_cd_026_skips_a_pipeline_caller(tmp_path: Path) -> None:
    """A `pipeline` job is CD-041's and CD-042's subject, not a misnamed job."""
    repo = _write(tmp_path, ".github/workflows/ci.yml", _caller())
    assert check_canonical_ci_job_names(repo) == []


# --- CD-041 ------------------------------------------------------------------


def test_cd_041_flags_a_missing_ci_file(tmp_path: Path) -> None:
    findings = check_cd_041(tmp_path)
    assert [f["rule_id"] for f in findings] == ["CD-041"]


def test_cd_041_flags_a_caller_at_an_old_major(tmp_path: Path) -> None:
    repo = _write(tmp_path, ".github/workflows/ci.yml", _caller(f"@v{_MAJOR - 1}"))
    findings = check_cd_041(repo)
    assert [f["rule_id"] for f in findings] == ["CD-041"]
    assert f"@v{_MAJOR - 1}" in findings[0]["finding"]
    assert f"@v{_MAJOR}" in findings[0]["finding"]


def test_cd_041_reads_the_major_from_pipeline_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving current_major in pipeline.yaml moves the expected pin."""
    repo = _write(tmp_path, ".github/workflows/ci.yml", _caller())
    assert check_cd_041(repo) == []
    monkeypatch.setattr(
        delivery, "_fetch_pipeline_config", lambda: {"current_major": _MAJOR + 1}
    )
    assert [f["rule_id"] for f in check_cd_041(repo)] == ["CD-041"]


def test_cd_041_flags_inline_stages(tmp_path: Path) -> None:
    ci = (
        "on:\n  push:\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: pytest\n"
    )
    repo = _write(tmp_path, ".github/workflows/ci.yml", ci)
    findings = check_cd_041(repo)
    assert [f["rule_id"] for f in findings] == ["CD-041"]
    assert "no `jobs.pipeline`" in findings[0]["finding"]


def test_cd_041_flags_steps_on_the_pipeline_job(tmp_path: Path) -> None:
    ci = _caller() + "    steps:\n      - run: echo extra\n"
    repo = _write(tmp_path, ".github/workflows/ci.yml", ci)
    findings = check_cd_041(repo)
    assert [f["rule_id"] for f in findings] == ["CD-041"]
    assert "steps" in findings[0]["finding"]


def test_cd_041_accepts_the_local_call_in_the_pipeline_source(
    tmp_path: Path,
) -> None:
    ci = (
        "on:\n  push:\njobs:\n  pipeline:\n    uses: ./.github/workflows/pipeline.yml\n"
    )
    repo = _write(tmp_path, ".github/workflows/ci.yml", ci)
    assert check_cd_041(repo, repo_type="shared-workflows") == []
    # Anywhere else a local pipeline.yml is not the canonical pipeline.
    assert [f["rule_id"] for f in check_cd_041(repo)] == ["CD-041"]


def test_cd_041_unreadable_config_is_a_checker_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail() -> dict:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(delivery, "_fetch_pipeline_config", _fail)
    repo = _write(tmp_path, ".github/workflows/ci.yml", _caller("@v0"))
    assert [f["rule_id"] for f in check_cd_041(repo)] == ["CHECKER"]


@respx.mock
def test_pipeline_config_is_read_from_the_github_repo() -> None:
    route = respx.get(delivery._PIPELINE_CONFIG_URL).mock(
        return_value=httpx.Response(200, text="current_major: 9\ncanary: x\n")
    )
    assert _REAL_FETCH()["current_major"] == 9
    assert route.called


# --- CD-042 ------------------------------------------------------------------


def test_cd_042_flags_a_ci_job_that_does_not_need_pipeline(tmp_path: Path) -> None:
    ci = _caller(extra="  lint:\n    runs-on: ubuntu-latest\n")
    repo = _write(tmp_path, ".github/workflows/ci.yml", ci)
    findings = check_cd_042(repo)
    assert [f["rule_id"] for f in findings] == ["CD-042"]
    assert "`lint`" in findings[0]["finding"]


@pytest.mark.parametrize("trigger", ["deployment_status", "repository_dispatch"])
def test_cd_042_flags_an_out_of_run_trigger(tmp_path: Path, trigger: str) -> None:
    wf = (
        f"on:\n  {trigger}:\n"
        "jobs:\n  smoke:\n"
        "    uses: mini-app-polis/.github/.github/workflows/evaluate.yml@v3\n"
    )
    repo = _write(_passing_repo(tmp_path), ".github/workflows/deployed.yml", wf)
    findings = check_cd_042(repo)
    assert [f["rule_id"] for f in findings] == ["CD-042"]
    assert "deployed.yml" in findings[0]["finding"]


def test_cd_042_flags_an_inline_job_on_a_commit_trigger(tmp_path: Path) -> None:
    wf = (
        "on:\n  workflow_run:\n    workflows: [CI]\n"
        "jobs:\n  contract:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: pytest tests/contract\n"
    )
    repo = _write(_passing_repo(tmp_path), ".github/workflows/contract.yml", wf)
    findings = check_cd_042(repo)
    assert [f["rule_id"] for f in findings] == ["CD-042"]
    assert "contract.yml" in findings[0]["finding"]
    assert "`contract`" in findings[0]["finding"]


def test_cd_042_skips_reusable_workflows(tmp_path: Path) -> None:
    wf = (
        "on:\n  workflow_call:\n"
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: pytest\n"
    )
    repo = _write(_passing_repo(tmp_path), ".github/workflows/python-test.yml", wf)
    assert check_cd_042(repo) == []


# --- CD-043 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "run",
    [
        "railway up --service api",
        "railway redeploy -y",
        "npx wrangler pages deploy dist --project-name site",
        'curl -X POST "https://api.cloudflare.com/client/v4/pages/webhooks/deploy_hooks/abc"',
    ],
)
def test_cd_043_flags_a_deploy_step_outside_ci(tmp_path: Path, run: str) -> None:
    wf = (
        "on:\n  workflow_dispatch:\n"
        "jobs:\n  ship:\n    runs-on: ubuntu-latest\n"
        f"    steps:\n      - run: '{run}'\n"
    )
    repo = _write(_passing_repo(tmp_path), ".github/workflows/manual.yml", wf)
    findings = check_cd_043(repo)
    assert [f["rule_id"] for f in findings] == ["CD-043"]
    assert "manual.yml" in findings[0]["finding"]


def test_cd_043_flags_lambda_deploy_outside_ci(tmp_path: Path) -> None:
    wf = (
        "on:\n  workflow_dispatch:\n"
        "jobs:\n  deploy:\n"
        "    uses: mini-app-polis/.github/.github/workflows/lambda-deploy.yml@v3\n"
    )
    repo = _write(_passing_repo(tmp_path), ".github/workflows/redeploy.yml", wf)
    findings = check_cd_043(repo)
    assert [f["rule_id"] for f in findings] == ["CD-043"]
    assert "redeploy.yml" in findings[0]["finding"]


def test_cd_043_skips_the_pipeline_sources_reusable_workflows(
    tmp_path: Path,
) -> None:
    wf = (
        "on:\n  workflow_call:\n"
        "jobs:\n  apply:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: railway up\n"
    )
    repo = _write(_passing_repo(tmp_path), ".github/workflows/railway-apply.yml", wf)
    assert check_cd_043(repo, repo_type="shared-workflows") == []


def test_cd_043_flags_a_branch_triggered_railway_service(tmp_path: Path) -> None:
    ts = (
        '// source: github("x/y", { branch: "dev" }) in a comment is ignored\n'
        'const api = service("api-kaianolevine-com", {\n'
        '  source: github("mini-app-polis/api-kaianolevine-com", { branch: "main" }),\n'
        "});\n"
    )
    repo = _write(_passing_repo(tmp_path), ".railway/railway.ts", ts)
    findings = check_cd_043(repo)
    assert [f["rule_id"] for f in findings] == ["CD-043"]
    assert ".railway/railway.ts" in findings[0]["finding"]
    assert "api-kaianolevine-com" in findings[0]["finding"]


_PAGES = """
resource "cloudflare_pages_project" "site" {{
  name = "site"
  source = {{
    type = "github"
    config = {{
      production_deployments_enabled = {prod}
      preview_deployment_setting     = "{preview}"
    }}
  }}
}}
"""


def test_cd_043_flags_a_pages_project_that_builds_on_push(tmp_path: Path) -> None:
    repo = _write(tmp_path, "pages.tf", _PAGES.format(prod="true", preview="all"))
    findings = check_cd_043(repo, repo_type="infrastructure")
    assert [f["rule_id"] for f in findings] == ["CD-043"]
    assert "cloudflare_pages_project.site" in findings[0]["finding"]
    assert "pages.tf" in findings[0]["finding"]


def test_cd_043_accepts_a_pages_project_with_builds_off(tmp_path: Path) -> None:
    repo = _write(tmp_path, "pages.tf", _PAGES.format(prod="false", preview="none"))
    assert check_cd_043(repo, repo_type="infrastructure") == []
