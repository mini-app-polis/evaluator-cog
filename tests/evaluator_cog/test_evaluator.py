from pathlib import Path

from evaluator_cog.engine.llm import build_conformance_prompt


def test_build_conformance_prompt_includes_evaluator_yaml_exemptions(
    tmp_path: Path,
) -> None:
    """evaluator.yaml content and exemption instructions appear in the LLM prompt."""
    (tmp_path / "evaluator.yaml").write_text(
        "type: pipeline-cog\n"
        "exemptions:\n"
        "  - rule: CD-015\n"
        "    reason: Not applicable for this service.\n"
    )
    prompt = build_conformance_prompt(
        repo_id="eval-test-repo",
        service_type="worker",
        dod_type="new_cog",
        language="python",
        standards_version="2.5.0",
        deterministic_findings=[],
        standards_rules=[],
        repo_path=tmp_path,
    )
    assert "CD-015" in prompt
    assert "Repo Evaluation Configuration" in prompt
    assert (
        "do not raise findings for these rule IDs under any circumstances".lower()
        in prompt.lower()
    )
