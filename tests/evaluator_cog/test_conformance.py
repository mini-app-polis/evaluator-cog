"""Tests for run_conformance_check and the post_llm_only posting behaviour."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from evaluator_cog.flows.conformance import run_conformance_check


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
    """When post_llm_only=True and LLM returns no findings, a STATUS INFO is posted."""
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
    assert posted[0]["severity"] == "INFO"
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
