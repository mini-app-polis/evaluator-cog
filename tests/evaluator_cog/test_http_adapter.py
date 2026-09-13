"""Tests for the HTTP adapter over handler()."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import evaluator_cog.adapters.http as adapter

SECRET = "s3cret-invoke-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EVALUATOR_INVOKE_SECRET", SECRET)
    with (
        patch.object(adapter, "_reset_run_tally"),
        patch.object(adapter, "_get_standards_version", return_value="9.9.9-test"),
    ):
        yield TestClient(adapter.app)


def _headers(token: str = SECRET) -> dict[str, str]:
    return {adapter.SECRET_HEADER: token}


def test_invoke_accepts_and_returns_a_run_id(client) -> None:
    """202 with the run id, before any work has happened."""
    with patch.object(adapter, "handler") as handler:
        response = client.post(
            "/invoke", json={"repo": "watcher-cog", "ref": "v1.2.3"}, headers=_headers()
        )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["repo"] == "watcher-cog"
    assert body["mode"] == "deterministic"
    assert body["run_id"].startswith("deterministic-9.9.9-test-")
    # TestClient runs background tasks after the response is returned.
    assert handler.call_count == 1


def test_invoke_builds_the_event_without_a_registry(client) -> None:
    """The repo's own evaluator.yaml supplies its type; nothing is looked up."""
    with patch.object(adapter, "handler") as handler:
        client.post(
            "/invoke",
            json={
                "repo": "mono",
                "ref": "main",
                "org": "other-org",
                "repo_id": "deejaytools-com-api",
                "mode": "llm",
            },
            headers=_headers(),
        )

    event = handler.call_args.args[0]
    assert event.org == "other-org"
    assert event.repo == "mono"
    assert event.ref == "main"
    assert event.mode == "llm"
    assert event.services == ({"id": "deejaytools-com-api", "repo": "mono"},)
    assert event.monorepo is None


def test_invoke_honours_a_supplied_run_id(client) -> None:
    """A sweep grouping several repositories passes its own."""
    with patch.object(adapter, "handler"):
        response = client.post(
            "/invoke",
            json={"repo": "watcher-cog", "run_id": "sweep-1"},
            headers=_headers(),
        )

    assert response.json()["run_id"] == "sweep-1"


def test_invoke_rejects_a_bad_token(client) -> None:
    with patch.object(adapter, "handler") as handler:
        response = client.post(
            "/invoke", json={"repo": "watcher-cog"}, headers=_headers("wrong")
        )

    assert response.status_code == 401
    assert handler.call_count == 0


def test_invoke_rejects_a_missing_token(client) -> None:
    with patch.object(adapter, "handler") as handler:
        response = client.post("/invoke", json={"repo": "watcher-cog"})

    assert response.status_code == 401
    assert handler.call_count == 0


def test_invoke_refuses_when_no_secret_is_configured(monkeypatch) -> None:
    """Fails closed. This route clones a repo and writes findings.

    The Prefect webhook skips its secret when unset because the worst a
    stranger can do there is post an embed. Here an unset secret is a
    misconfiguration, not a decision.
    """
    monkeypatch.delenv("EVALUATOR_INVOKE_SECRET", raising=False)
    with (
        patch.object(adapter, "_reset_run_tally"),
        patch.object(adapter, "_get_standards_version", return_value="9.9.9-test"),
        patch.object(adapter, "handler") as handler,
    ):
        response = TestClient(adapter.app).post(
            "/invoke", json={"repo": "watcher-cog"}, headers=_headers()
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "not_configured"
    assert handler.call_count == 0


def test_invoke_rejects_an_unknown_mode(client) -> None:
    response = client.post(
        "/invoke", json={"repo": "watcher-cog", "mode": "guess"}, headers=_headers()
    )
    assert response.status_code == 422


def test_evaluation_failure_does_not_escape_the_background_task() -> None:
    """The caller is long gone by then; the log and Sentry are the report."""
    event = MagicMock()
    with patch.object(adapter, "handler", side_effect=RuntimeError("boom")):
        adapter._evaluate(event)  # must not raise


def test_evaluation_asserts_findings_were_delivered() -> None:
    """A run that computed findings and delivered none is a systemic fault."""
    event = MagicMock()
    with (
        patch.object(adapter, "handler"),
        patch.object(adapter, "_assert_findings_were_delivered") as assert_delivered,
    ):
        adapter._evaluate(event)

    assert assert_delivered.call_count == 1


def test_health_does_not_reach_its_dependencies() -> None:
    """A health check that fails when the API is down is a restart loop."""
    with patch.object(adapter, "_get_standards_version", side_effect=AssertionError):
        response = TestClient(adapter.app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
