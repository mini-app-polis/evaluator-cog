"""CD-031: every change requests its own conformance evaluation, after CI
and before the release."""

from __future__ import annotations

from pathlib import Path

from evaluator_cog.engine.deterministic import check_cd_031

_TEST = """
  test:
    runs-on: ubuntu-latest
    steps:
      - run: uv run pytest
  security:
    uses: mini-app-polis/.github/.github/workflows/security.yml@v3
"""

_RELEASE = """
  release:
    needs: [test, security]
    runs-on: ubuntu-latest
    steps:
      - run: npx semantic-release
"""

_EVALUATE = """
  evaluate:
    needs: {needs}
    if: github.ref == 'refs/heads/dev' && github.event_name == 'push'
    uses: mini-app-polis/.github/.github/workflows/evaluate.yml@v3
    with:
      ref: dev{extra}
    secrets:
      api-key: ${{{{ secrets.CI_VALIDATOR_API_KEY }}}}
"""

_FLEET = "\n      scope: fleet"


def _evaluate(needs: str, extra: str = "") -> str:
    return _EVALUATE.format(needs=needs, extra=extra)


def _ci(tmp_path: Path, jobs: str) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "name: CI\non:\n  push:\n    branches: [main, dev]\njobs:" + jobs
    )
    return tmp_path


def test_passes_an_evaluate_job_after_ci_and_before_the_release(
    tmp_path: Path,
) -> None:
    repo = _ci(tmp_path, _TEST + _RELEASE + _evaluate("[test, security]"))
    assert check_cd_031(repo) == []


def test_passes_a_repo_with_no_release(tmp_path: Path) -> None:
    assert check_cd_031(_ci(tmp_path, _TEST + _evaluate("[test, security]"))) == []


def test_flags_an_evaluate_job_after_the_release(tmp_path: Path) -> None:
    """A change that cuts no release would never be graded."""
    repo = _ci(tmp_path, _TEST + _RELEASE + _evaluate("release"))
    findings = check_cd_031(repo)
    assert len(findings) == 1
    assert "after the release" in findings[0]["finding"]


def test_flags_an_evaluate_job_after_a_deploy(tmp_path: Path) -> None:
    deploy = """
  deploy:
    needs: [release]
    runs-on: ubuntu-latest
    steps:
      - run: ./deploy.sh
"""
    repo = _ci(tmp_path, _TEST + _RELEASE + deploy + _evaluate("deploy"))
    assert "after the release" in check_cd_031(repo)[0]["finding"]


def test_flags_an_evaluate_job_that_does_not_wait_for_ci(tmp_path: Path) -> None:
    repo = _ci(tmp_path, _TEST + _evaluate("[security]"))
    findings = check_cd_031(repo)
    assert len(findings) == 1
    assert "without waiting for CI" in findings[0]["finding"]


def test_flags_a_repo_with_no_evaluate_job(tmp_path: Path) -> None:
    findings = check_cd_031(_ci(tmp_path, _TEST + _RELEASE))
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "CD-031"
    assert "evaluate.yml" in findings[0]["suggestion"]


def test_flags_a_repo_with_no_workflows(tmp_path: Path) -> None:
    assert len(check_cd_031(tmp_path)) == 1


def test_passes_a_fleet_pass_after_the_release(tmp_path: Path) -> None:
    """ecosystem-standards and evaluator-cog grade the fleet with what they
    just released."""
    repo = _ci(tmp_path, _TEST + _RELEASE + _evaluate("release", _FLEET))
    assert check_cd_031(repo) == []


def test_flags_a_fleet_pass_before_the_release(tmp_path: Path) -> None:
    repo = _ci(tmp_path, _TEST + _RELEASE + _evaluate("[test, security]", _FLEET))
    assert "does not run after the change ships" in check_cd_031(repo)[0]["finding"]


def test_passes_the_shared_workflows_repo_calling_its_own_copy(
    tmp_path: Path,
) -> None:
    """mini-app-polis/.github owns evaluate.yml and calls it by local path."""
    local = _evaluate("[test, security]").replace(
        "mini-app-polis/.github/.github/workflows/evaluate.yml@v3",
        "./.github/workflows/evaluate.yml",
    )
    assert check_cd_031(_ci(tmp_path, _TEST + _RELEASE + local)) == []


def test_passes_an_evaluate_job_before_the_shared_release_workflow(
    tmp_path: Path,
) -> None:
    release = """
  release:
    needs: [test, security]
    uses: mini-app-polis/.github/.github/workflows/release.yml@v3
"""
    repo = _ci(tmp_path, _TEST + release + _evaluate("[test, security]"))
    assert check_cd_031(repo) == []


def test_flags_an_evaluate_job_after_the_shared_release_workflow(
    tmp_path: Path,
) -> None:
    release = """
  release:
    needs: [test, security]
    uses: mini-app-polis/.github/.github/workflows/release.yml@v3
"""
    repo = _ci(tmp_path, _TEST + release + _evaluate("release"))
    assert "after the release" in check_cd_031(repo)[0]["finding"]


def test_flags_an_evaluate_job_after_a_job_using_the_shared_release_action(
    tmp_path: Path,
) -> None:
    release = """
  release:
    needs: [test]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: mini-app-polis/.github/.github/actions/semantic-release@v3
"""
    repo = _ci(tmp_path, _TEST + release + _evaluate("release"))
    assert "after the release" in check_cd_031(repo)[0]["finding"]


_APPLY = """
  plan:
    runs-on: ubuntu-latest
    steps:
      - run: terraform plan -out tfplan
  apply:
    needs: plan
    runs-on: ubuntu-latest
    steps:
      - run: |
          terraform plan -out tfplan
          terraform apply tfplan
"""


def test_passes_a_terraform_root_evaluating_after_its_apply(tmp_path: Path) -> None:
    """mini-app-polis/infra has no releases; a change ships when it is applied."""
    repo = _ci(tmp_path, _APPLY + _evaluate("[plan, apply]"))
    assert check_cd_031(repo) == []


def test_flags_a_terraform_root_evaluating_after_only_the_plan(
    tmp_path: Path,
) -> None:
    """A plan changes nothing, so evaluating after it grades the old state."""
    repo = _ci(tmp_path, _APPLY + _evaluate("plan"))
    findings = check_cd_031(repo)
    assert len(findings) == 1
    assert "terraform apply" in findings[0]["finding"]
