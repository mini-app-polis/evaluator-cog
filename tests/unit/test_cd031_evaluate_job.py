"""CD-031: every release requests its own conformance evaluation."""

from __future__ import annotations

from pathlib import Path

from evaluator_cog.engine.deterministic import check_cd_031

_RELEASE = """
  release:
    runs-on: ubuntu-latest
    steps:
      - run: npx semantic-release
"""

_EVALUATE = """
  evaluate:
    needs: {needs}
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    uses: mini-app-polis/.github/.github/workflows/evaluate.yml@v3
    secrets:
      api-key: ${{{{ secrets.CI_VALIDATOR_API_KEY }}}}
"""


def _ci(tmp_path: Path, jobs: str) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "name: CI\non:\n  push:\n    branches: [main]\njobs:" + jobs
    )
    return tmp_path


def test_passes_an_evaluate_job_after_the_release(tmp_path: Path) -> None:
    repo = _ci(tmp_path, _RELEASE + _EVALUATE.format(needs="release"))
    assert check_cd_031(repo) == []


def test_passes_an_evaluate_job_after_a_deploy_that_needs_the_release(
    tmp_path: Path,
) -> None:
    """Lambda cogs evaluate after the deploy, which itself needs the release."""
    deploy = """
  deploy:
    needs: [release]
    runs-on: ubuntu-latest
    steps:
      - run: ./deploy.sh
"""
    repo = _ci(tmp_path, _RELEASE + deploy + _EVALUATE.format(needs="deploy"))
    assert check_cd_031(repo) == []


def test_flags_a_repo_with_no_evaluate_job(tmp_path: Path) -> None:
    findings = check_cd_031(_ci(tmp_path, _RELEASE))
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "CD-031"
    assert "evaluate.yml" in findings[0]["suggestion"]


def test_flags_an_evaluate_job_that_does_not_wait_for_the_release(
    tmp_path: Path,
) -> None:
    test_job = """
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test
"""
    repo = _ci(tmp_path, _RELEASE + test_job + _EVALUATE.format(needs="test"))
    findings = check_cd_031(repo)
    assert len(findings) == 1
    assert "does not run after the release" in findings[0]["finding"]


def test_flags_a_repo_with_no_workflows(tmp_path: Path) -> None:
    assert len(check_cd_031(tmp_path)) == 1


def test_passes_the_shared_workflows_repo_calling_its_own_copy(
    tmp_path: Path,
) -> None:
    """mini-app-polis/.github owns evaluate.yml and calls it by local path."""
    local = _EVALUATE.format(needs="release").replace(
        "mini-app-polis/.github/.github/workflows/evaluate.yml@v3",
        "./.github/workflows/evaluate.yml",
    )
    assert check_cd_031(_ci(tmp_path, _RELEASE + local)) == []


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
    repo = _ci(tmp_path, _APPLY + _EVALUATE.format(needs="[plan, apply]"))
    assert check_cd_031(repo) == []


def test_flags_a_terraform_root_evaluating_after_only_the_plan(
    tmp_path: Path,
) -> None:
    """A plan changes nothing, so evaluating after it grades the old state."""
    repo = _ci(tmp_path, _APPLY + _EVALUATE.format(needs="plan"))
    findings = check_cd_031(repo)
    assert len(findings) == 1
    assert "terraform apply" in findings[0]["finding"]


def test_passes_an_evaluate_job_after_the_shared_release_workflow(
    tmp_path: Path,
) -> None:
    """A release job that calls the shared release.yml is the release (VER-006)."""
    release = """
  release:
    needs: [test, security]
    uses: mini-app-polis/.github/.github/workflows/release.yml@v3
"""
    repo = _ci(tmp_path, release + _EVALUATE.format(needs="release"))
    assert check_cd_031(repo) == []


def test_passes_an_evaluate_job_after_a_deploy_after_the_shared_release_workflow(
    tmp_path: Path,
) -> None:
    release = """
  release:
    uses: mini-app-polis/.github/.github/workflows/release.yml@v3
  deploy:
    needs: [release]
    runs-on: ubuntu-latest
    steps:
      - run: ./deploy.sh
"""
    repo = _ci(tmp_path, release + _EVALUATE.format(needs="deploy"))
    assert check_cd_031(repo) == []


def test_passes_an_evaluate_job_after_a_job_using_the_shared_release_action(
    tmp_path: Path,
) -> None:
    """A release job of its own that uses the semantic-release action as a step."""
    release = """
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: mini-app-polis/.github/.github/actions/semantic-release@v3
"""
    repo = _ci(tmp_path, release + _EVALUATE.format(needs="release"))
    assert check_cd_031(repo) == []


def test_passes_the_github_repos_own_local_release_workflow(tmp_path: Path) -> None:
    release = """
  release:
    uses: ./.github/workflows/release.yml
"""
    repo = _ci(tmp_path, release + _EVALUATE.format(needs="release"))
    assert check_cd_031(repo) == []


def test_still_flags_an_evaluate_job_after_an_unrelated_reusable_workflow(
    tmp_path: Path,
) -> None:
    other = """
  release:
    uses: mini-app-polis/.github/.github/workflows/python-test.yml@v3
"""
    findings = check_cd_031(_ci(tmp_path, other + _EVALUATE.format(needs="release")))
    assert [f["rule_id"] for f in findings] == ["CD-031"]
