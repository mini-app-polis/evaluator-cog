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
