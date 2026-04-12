"""Tests for run_conformance_check and the post_llm_only posting behaviour."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from evaluator_cog.engine.deterministic import CheckResult
from evaluator_cog.engine.evaluator_config import EvaluatorConfig
from evaluator_cog.flows.conformance import (
    _fetch_standards_for_service,
    _run_standalone_deterministic,
    run_conformance_check,
)


def _minimal_repo() -> Path:
    """Create a minimal repo directory that won't crash run_all_checks."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "README.md").write_text("# Test repo\n")
    return tmp


def test_post_llm_only_posts_only_llm_findings(monkeypatch) -> None:
    """When post_llm_only=True, only LLM findings are posted, not deterministic ones."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test.example.com")

    repo_path = _minimal_repo()
    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    llm_finding = {
        "rule_id": "DOC-006",
        "dimension": "documentation_coverage",
        "severity": "WARN",
        "finding": "Public functions lack docstrings.",
        "suggestion": "Add docstrings.",
    }

    api = SimpleNamespace(post=_fake_post, get=MagicMock(return_value={}))

    with (
        patch(
            "evaluator_cog.flows.conformance._anthropic_messages_create",
            return_value='{"findings":[{"rule_id":"DOC-006","dimension":"documentation_coverage","severity":"WARN","finding":"Public functions lack docstrings.","suggestion":"Add docstrings."}]}',
        ),
        patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as mock_client,
        patch(
            "evaluator_cog.flows.conformance.get_run_logger", return_value=MagicMock()
        ),
    ):
        mock_client.from_env.return_value = api
        result = run_conformance_check(
            repo_id="test-repo",
            repo_path=repo_path,
            standards_version="2.5.1",
            post=True,
            post_llm_only=True,
            run_id="conformance-2.5.1-test",
        )

    # Result contains all findings (deterministic + LLM)
    assert any(f.get("rule_id") == "DOC-006" for f in result)

    # But only the LLM finding was posted
    assert len(posted) == 1
    assert posted[0]["finding"] == llm_finding["finding"]
    assert posted[0]["source"] == "conformance_check"


def test_post_llm_only_false_posts_all_findings(monkeypatch) -> None:
    """When post_llm_only=False, both deterministic and LLM findings are posted."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test.example.com")

    repo_path = _minimal_repo()
    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=MagicMock(return_value={}))

    with (
        patch(
            "evaluator_cog.flows.conformance._anthropic_messages_create",
            return_value='{"findings":[{"rule_id":"DOC-006","dimension":"documentation_coverage","severity":"WARN","finding":"LLM finding.","suggestion":""}]}',
        ),
        patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as mock_client,
        patch(
            "evaluator_cog.flows.conformance.get_run_logger", return_value=MagicMock()
        ),
    ):
        mock_client.from_env.return_value = api
        run_conformance_check(
            repo_id="test-repo",
            repo_path=repo_path,
            standards_version="2.5.1",
            post=True,
            post_llm_only=False,
            run_id="conformance-2.5.1-test",
        )

    # Both deterministic and LLM findings posted
    assert len(posted) >= 1
    findings_text = [p["finding"] for p in posted]
    assert any("LLM finding" in t for t in findings_text)


def test_post_llm_only_empty_llm_posts_status(monkeypatch) -> None:
    """When post_llm_only=True and LLM returns no findings, a STATUS SUCCESS is posted."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test.example.com")

    repo_path = _minimal_repo()
    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=MagicMock(return_value={}))

    with (
        patch(
            "evaluator_cog.flows.conformance._anthropic_messages_create",
            return_value='{"findings":[]}',
        ),
        patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as mock_client,
        patch(
            "evaluator_cog.flows.conformance.get_run_logger", return_value=MagicMock()
        ),
    ):
        mock_client.from_env.return_value = api
        run_conformance_check(
            repo_id="test-repo",
            repo_path=repo_path,
            standards_version="2.5.1",
            post=True,
            post_llm_only=True,
            run_id="conformance-2.5.1-test",
        )

    assert len(posted) == 1
    assert posted[0]["severity"] == "SUCCESS"
    assert "passed all LLM checks" in posted[0]["finding"]
    assert posted[0]["source"] == "conformance_check"


def test_run_llm_false_source_is_conformance_deterministic(monkeypatch) -> None:
    """Deterministic-only run posts with source='conformance_deterministic'."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test.example.com")
    # No ANTHROPIC_API_KEY set — LLM should be skipped

    repo_path = _minimal_repo()
    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=MagicMock(return_value={}))

    with (
        patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as mock_client,
        patch(
            "evaluator_cog.flows.conformance.get_run_logger", return_value=MagicMock()
        ),
    ):
        mock_client.from_env.return_value = api
        run_conformance_check(
            repo_id="test-repo",
            repo_path=repo_path,
            standards_version="2.5.1",
            post=True,
            post_llm_only=False,
            run_id="deterministic-2.5.1-test",
        )

    assert all(p["source"] == "conformance_check" for p in posted)


def _fake_fetch_standards(url: str) -> dict:
    if url.endswith("/pipeline.yaml"):
        return {
            "standards": [
                {
                    "id": "PIPELINE-RULE",
                    "checkable": True,
                    "applies_to": ["pipeline-cog"],
                    "title": "Pipeline-only",
                    "severity": "WARN",
                    "check_notes": "Only for pipeline cogs.",
                },
            ]
        }
    if url.endswith("/python.yaml"):
        return {
            "standards": [
                {
                    "id": "LEGACY-COG-RULE",
                    "checkable": True,
                    "applies_to": ["new_cog"],
                    "title": "Legacy cog",
                    "severity": "INFO",
                    "check_notes": "Matches dod_type.",
                },
            ]
        }
    return {"standards": []}


def test_fetch_standards_matches_new_repo_type() -> None:
    """Rules whose applies_to includes pipeline-cog are included when repo_type matches."""
    service = {"id": "x", "dod_type": "new_cog"}
    cfg = EvaluatorConfig(repo_type="pipeline-cog")
    with patch(
        "evaluator_cog.flows.conformance._fetch_yaml",
        side_effect=_fake_fetch_standards,
    ):
        rules = _fetch_standards_for_service(service, cfg)
    ids = {r["id"] for r in rules}
    assert "PIPELINE-RULE" in ids


def test_fetch_standards_falls_back_to_dod_type_when_no_evaluator_cfg() -> None:
    """When evaluator_cfg is None, applies_to matches on legacy dod_type."""
    service = {"id": "x", "dod_type": "new_cog"}
    with patch(
        "evaluator_cog.flows.conformance._fetch_yaml",
        side_effect=_fake_fetch_standards,
    ):
        rules = _fetch_standards_for_service(service, None)
    ids = {r["id"] for r in rules}
    assert "LEGACY-COG-RULE" in ids


def test_run_standalone_deterministic_calls_load_evaluator_config(
    tmp_path: Path,
) -> None:
    """Standalone deterministic pass loads config from the cloned repo path."""
    cfg = EvaluatorConfig(repo_type="pipeline-cog")
    (tmp_path / "README.md").write_text("# ok\n")

    with (
        patch(
            "evaluator_cog.flows.conformance.load_evaluator_config",
        ) as mock_load,
        patch(
            "evaluator_cog.flows.conformance.run_all_checks",
        ) as mock_run_all,
        patch(
            "evaluator_cog.flows.conformance.post_findings",
        ) as mock_post,
    ):
        mock_load.return_value = cfg
        mock_run_all.return_value = CheckResult(findings=[], checked_rule_ids=set())
        service = {"id": "svc-test", "type": "worker", "dod_type": "new_cog"}
        prefect_log = MagicMock()
        _run_standalone_deterministic(
            service,
            tmp_path,
            "2.5.0",
            "deterministic-2.5.0-unit",
            prefect_log,
            monorepo_root=None,
        )

    mock_load.assert_called()
    assert mock_load.call_args_list[0][0][0] == tmp_path
    mock_run_all.assert_called_once_with(
        tmp_path,
        language="python",
        service_type="worker",
        dod_type="new_cog",
        cog_subtype=None,
        check_exceptions=[],
        exception_reasons={},
        monorepo_root=None,
        workspace_package_json_text=None,
        evaluator_config=cfg,
    )
    # run_all_checks returns empty findings, so _run_standalone_deterministic
    # substitutes a STATUS SUCCESS finding before posting.
    mock_post.assert_called_once_with(
        findings=[
            {
                "rule_id": "STATUS",
                "dimension": "structural_conformance",
                "severity": "SUCCESS",
                "finding": "svc-test passed all deterministic checks for standards v2.5.0.",
                "suggestion": "",
            }
        ],
        run_id="deterministic-2.5.0-unit",
        repo="svc-test",
        flow_name="deterministic-conformance",
        source="conformance_deterministic",
        standards_version="2.5.0",
    )
