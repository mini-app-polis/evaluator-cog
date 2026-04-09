"""Tests that evaluation output payloads conform to the required field contract.

Closes TEST-004: assert output structure or schema of evaluation payloads.
Verifies that Finding and ConformanceResult models carry all fields required
by the POST /v1/evaluations API contract, and that api_client.post_findings
actually delivers those fields in the wire payload.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from evaluator_cog.models import ConformanceResult, Finding

# ---------------------------------------------------------------------------
# Model field-level assertions
# ---------------------------------------------------------------------------


def test_finding_model_has_all_required_fields() -> None:
    """Finding has all fields required by the POST /v1/evaluations contract."""
    f = Finding(
        rule_id="CD-010",
        dimension="structural_conformance",
        severity="ERROR",
        finding="Sentry initialisation not found at main.py entry point.",
        suggestion="Add sentry_sdk.init() at the top of main.py.",
    )
    assert isinstance(f.rule_id, str)
    assert isinstance(f.dimension, str)
    assert isinstance(f.severity, str)
    assert f.severity in {"INFO", "WARN", "ERROR"}
    assert isinstance(f.finding, str)
    assert len(f.finding) > 0
    # suggestion is optional — must be str or None
    assert f.suggestion is None or isinstance(f.suggestion, str)


def test_finding_model_severity_values() -> None:
    """All three valid severity levels are accepted by the Finding model."""
    for sev in ("INFO", "WARN", "ERROR"):
        f = Finding(dimension="structural_conformance", severity=sev, finding="ok")
        assert f.severity == sev


def test_finding_model_optional_rule_id_defaults_to_empty_string() -> None:
    """rule_id defaults to empty string when omitted (STATUS findings)."""
    f = Finding(dimension="structural_conformance", severity="INFO", finding="Passed.")
    assert f.rule_id == ""


def test_finding_model_optional_suggestion_defaults_to_none() -> None:
    """suggestion defaults to None when omitted."""
    f = Finding(dimension="structural_conformance", severity="INFO", finding="Passed.")
    assert f.suggestion is None


def test_conformance_result_model_has_all_required_fields() -> None:
    """ConformanceResult carries all fields needed to reconstruct a run summary."""
    finding = Finding(
        rule_id="STATUS",
        dimension="structural_conformance",
        severity="INFO",
        finding="Repo passed all checks.",
    )
    result = ConformanceResult(
        repo_id="evaluator-cog",
        standards_version="3.0.1",
        findings=[finding],
        deterministic_count=0,
        llm_count=1,
    )
    assert isinstance(result.repo_id, str)
    assert isinstance(result.standards_version, str)
    assert isinstance(result.findings, list)
    assert all(isinstance(f, Finding) for f in result.findings)
    assert isinstance(result.deterministic_count, int)
    assert isinstance(result.llm_count, int)


# ---------------------------------------------------------------------------
# Wire payload assertions (post_findings -> /v1/evaluations)
# ---------------------------------------------------------------------------


def test_post_payload_contains_all_required_contract_fields(monkeypatch) -> None:
    """The dict posted to /v1/evaluations contains every required contract field."""
    from evaluator_cog.engine.api_client import post_findings

    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test.example.com")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=lambda *_, **__: None)

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as mock_client:
        mock_client.from_env.return_value = api
        post_findings(
            findings=[
                {
                    "rule_id": "CD-010",
                    "dimension": "structural_conformance",
                    "severity": "ERROR",
                    "finding": "Sentry not initialised at entry point.",
                    "suggestion": "Add sentry_sdk.init() to main.py.",
                }
            ],
            run_id="conformance-3.0.1-test-uuid",
            repo="evaluator-cog",
            flow_name="conformance",
            source="conformance_check",
            standards_version="3.0.1",
        )
        mock_client.from_env.assert_called_once()

    assert len(posted) == 1, "Expected exactly one finding to be posted"
    payload = posted[0]

    required_fields = {
        "run_id",
        "repo",
        "dimension",
        "severity",
        "finding",
        "standards_version",
        "source",
    }
    for field in required_fields:
        assert field in payload, f"Missing required contract field: {field}"
        assert payload[field] is not None, f"Contract field '{field}' must not be None"

    assert payload["severity"] in {"INFO", "WARN", "ERROR"}, (
        f"severity must be INFO/WARN/ERROR, got: {payload['severity']!r}"
    )
    assert payload["repo"] == "evaluator-cog"
    assert payload["run_id"] == "conformance-3.0.1-test-uuid"
    assert payload["source"] == "conformance_check"
    assert payload["standards_version"] == "3.0.1"
    assert isinstance(payload["finding"], str)
    assert len(payload["finding"]) > 0


def test_post_payload_severity_normalisation(monkeypatch) -> None:
    """post_findings normalises 'WARNING' -> 'WARN' in the wire payload."""
    from evaluator_cog.engine.api_client import post_findings

    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test.example.com")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=lambda *_, **__: None)

    with patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as mock_client:
        mock_client.from_env.return_value = api
        post_findings(
            findings=[
                {
                    "dimension": "pipeline_consistency",
                    "severity": "WARNING",  # non-canonical — should be normalised
                    "finding": "Something is off.",
                    "suggestion": "",
                }
            ],
            run_id="run-normalise-test",
            repo="deejay-cog",
            flow_name=None,
            source="flow_inline",
            standards_version="3.0.1",
        )

    assert len(posted) == 1
    assert posted[0]["severity"] == "WARN", (
        "post_findings must normalise 'WARNING' to 'WARN'"
    )


def test_post_payload_source_field_is_preserved(monkeypatch) -> None:
    """The source field in the wire payload matches what post_findings received."""
    from evaluator_cog.engine.api_client import post_findings

    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test.example.com")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=lambda *_, **__: None)

    for source_val in (
        "conformance_deterministic",
        "conformance_check",
        "prefect_webhook",
    ):
        posted.clear()
        with patch(
            "evaluator_cog.engine.api_client.CommonPythonApiClient"
        ) as mock_client:
            mock_client.from_env.return_value = api
            post_findings(
                findings=[
                    {
                        "dimension": "structural_conformance",
                        "severity": "INFO",
                        "finding": "All good.",
                        "suggestion": None,
                    }
                ],
                run_id=f"run-{source_val}",
                repo="evaluator-cog",
                flow_name="conformance-check",
                source=source_val,
                standards_version="3.0.1",
            )

        assert len(posted) == 1
        assert posted[0]["source"] == source_val, (
            f"Expected source='{source_val}', got '{posted[0]['source']}'"
        )
