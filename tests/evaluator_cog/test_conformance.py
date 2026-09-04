"""Tests for run_conformance_check and the post_llm_only posting behaviour."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from evaluator_cog.engine.deterministic import CheckResult
from evaluator_cog.engine.evaluator_config import EvaluatorConfig
from evaluator_cog.flows.conformance import (
    _declared_branch,
    _fetch_standards_for_service,
    _run_standalone_deterministic,
    conformance_check_flow,
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
    assert posted[0]["source"] == "conformance_llm"


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
    assert posted[0]["source"] == "conformance_llm"


def test_run_conformance_check_posts_with_conformance_llm_source(monkeypatch) -> None:
    """run_conformance_check() posts all findings with source='conformance_llm'.

    Note: this helper is used by the LLM path of conformance_check_flow
    (run_llm=True). The deterministic-only path goes through
    _run_standalone_deterministic instead, which posts with
    source='conformance_deterministic'.
    """
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

    assert all(p["source"] == "conformance_llm" for p in posted)


def _fake_fetch_standards(url: str) -> dict:
    if url.endswith("/pipeline.yaml"):
        return {
            "standards": [
                {
                    "id": "PIPELINE-RULE",
                    "status": "requirement",
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
                    "status": "convention",
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


def test_fetch_standards_rejects_invalid_rule_status() -> None:
    """v4.0.0 catalog allows only requirement / convention / gap.
    A rule with status 'advisory' or 'idea' must be rejected."""

    def _fake_fetch(url: str) -> dict:
        if url.endswith("/principles.yaml"):
            return {
                "standards": [
                    {
                        "id": "PRIN-999",
                        "status": "advisory",  # invalid in v4.0.0
                        "checkable": True,
                        "applies_to": ["all"],
                        "title": "Legacy advisory rule",
                        "severity": "INFO",
                        "check_notes": "DETERMINISTIC CHECK. ...",
                    },
                ]
            }
        return {"standards": []}

    service = {"id": "x", "dod_type": "new_cog"}
    cfg = EvaluatorConfig(repo_type="pipeline-cog")
    with (
        patch(
            "evaluator_cog.flows.conformance._fetch_yaml",
            side_effect=_fake_fetch,
        ),
        pytest.raises(ValueError, match="invalid status"),
    ):
        _fetch_standards_for_service(service, cfg)


def test_fetch_catalog_schema_parses_v4_shape() -> None:
    """_fetch_catalog_schema returns traits with structured exempts/downgrades,
    repo_types set, and statuses set from index.yaml."""
    fake_index = {
        "statuses": {
            "requirement": {"description": "must comply"},
            "convention": {"description": "should comply"},
            "gap": {"description": "tracked deficiency"},
        },
        "schema": {
            "repo_types": {
                "pipeline-cog": "desc",
                "api-service": "desc",
            },
            "traits": {
                "logger-primitive": {
                    "description": "is the logger",
                    "exempts": ["CD-009"],
                },
                "multi-flow": {
                    "description": "multi-flow",
                    "downgrades": [
                        {"rule": "CD-015", "to": "INFO", "reason": "scanner limit"},
                    ],
                },
            },
        },
    }
    from evaluator_cog.flows.conformance import _fetch_catalog_schema

    with patch(
        "evaluator_cog.flows.conformance._fetch_yaml",
        return_value=fake_index,
    ):
        schema = _fetch_catalog_schema()

    assert schema["statuses"] == {"requirement", "convention", "gap"}
    assert schema["repo_types"] == {"pipeline-cog", "api-service"}
    assert "logger-primitive" in schema["traits"]
    assert schema["traits"]["logger-primitive"]["exempts"] == ["CD-009"]
    assert schema["traits"]["logger-primitive"]["downgrades"] == []
    assert schema["traits"]["multi-flow"]["exempts"] == []
    assert schema["traits"]["multi-flow"]["downgrades"] == [
        {"rule": "CD-015", "to": "INFO", "reason": "scanner limit"},
    ]


def test_fetch_catalog_schema_returns_empty_on_fetch_failure() -> None:
    """When _fetch_yaml returns {}, _fetch_catalog_schema returns a dict
    with empty traits/repo_types/statuses rather than raising."""
    from evaluator_cog.flows.conformance import _fetch_catalog_schema

    with patch(
        "evaluator_cog.flows.conformance._fetch_yaml",
        return_value={},
    ):
        schema = _fetch_catalog_schema()

    assert schema["traits"] == {}
    assert schema["repo_types"] == set()
    assert schema["statuses"] == set()


def test_fetch_full_rule_catalog_captures_applies_to_and_modifies() -> None:
    """_fetch_full_rule_catalog returns per-rule applies_to, modifies, status.
    Rules omitting applies_to have it stored as None."""

    def _fake_fetch(url: str) -> dict:
        if url.endswith("/index.yaml"):
            return {
                "files": [
                    {"file": "standards/monorepo.yaml", "domain": "monorepo"},
                    {"file": "standards/evaluation.yaml", "domain": "evaluation"},
                ],
            }
        if url.endswith("/monorepo.yaml"):
            return {
                "standards": [
                    {
                        "id": "MONO-001",
                        "checkable": True,
                        "applies_to": ["api-service", "react-app"],
                        "modifies": ["XSTACK-001"],
                        "status": "requirement",
                    },
                    {
                        "id": "MONO-003",
                        "checkable": True,
                        "status": "requirement",
                        # no applies_to
                    },
                ]
            }
        if url.endswith("/evaluation.yaml"):
            return {
                "standards": [
                    {
                        "id": "EVAL-003",
                        "checkable": True,
                        "status": "requirement",
                        # no applies_to
                    },
                ]
            }
        return {"standards": []}

    from evaluator_cog.flows.conformance import _fetch_full_rule_catalog

    with patch(
        "evaluator_cog.flows.conformance._fetch_yaml",
        side_effect=_fake_fetch,
    ):
        catalog = _fetch_full_rule_catalog()

    assert catalog["MONO-001"]["applies_to"] == ["api-service", "react-app"]
    assert catalog["MONO-001"]["modifies"] == ["XSTACK-001"]
    assert catalog["MONO-003"]["applies_to"] is None
    assert catalog["MONO-003"]["modifies"] == []
    assert catalog["EVAL-003"]["applies_to"] is None


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
    mock_run_all.assert_called_once()
    call_args, call_kwargs = mock_run_all.call_args
    assert call_args == (tmp_path,)
    # `progress` is a closure over the run's logger, so it is compared by
    # kind rather than by value — asserting it is passed at all is the
    # point: without it a check that stalls has nothing to name it, which
    # is how a 129-second CD-005 hid behind one log line for nine minutes.
    progress = call_kwargs.pop("progress", None)
    assert callable(progress), "run_all_checks must receive a progress sink"
    assert call_kwargs == {
        "language": "python",
        "service_type": "worker",
        "dod_type": "new_cog",
        "cog_subtype": None,
        "check_exceptions": [],
        "exception_reasons": {},
        "monorepo_root": None,
        "workspace_package_json_text": None,
        "evaluator_config": cfg,
        "rule_catalog": None,
        "catalog_schema": None,
    }
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


def test_conformance_monorepo_service_failure_does_not_abort_flow(
    monkeypatch,
) -> None:
    """PRIN-002: a single bad service record in a monorepo must not
    crash the whole flow — the remaining siblings must still run.

    Uses a minimal fake ecosystem with two monorepo apps. The first raises
    during per-service setup; the second must still reach run_all_checks.
    """
    import evaluator_cog.flows.conformance as conf

    ecosystem = {
        "services": [
            {
                "id": "app-a",
                "repo": "mono",
                "status": "active",
                "type": "api",
                "language": "typescript",
                "monorepo": "mono-1",
                "monorepo_path": "apps/a",
                "check_exceptions": "INVALID_NOT_A_LIST",
            },
            {
                "id": "app-b",
                "repo": "mono",
                "status": "active",
                "type": "api",
                "language": "typescript",
                "monorepo": "mono-1",
                "monorepo_path": "apps/b",
                "check_exceptions": [],
            },
        ],
        "monorepos": [
            {
                "id": "mono-1",
                "repo": "mono",
                "apps": [
                    {"service_id": "app-a", "path": "apps/a"},
                    {"service_id": "app-b", "path": "apps/b"},
                ],
            }
        ],
    }

    def fake_download_repo(repo_name, tmp_dir, branch="main"):
        root = Path(tmp_dir) / repo_name
        (root / "apps" / "a").mkdir(parents=True, exist_ok=True)
        (root / "apps" / "b").mkdir(parents=True, exist_ok=True)
        return root

    monkeypatch.setenv("STANDARDS_VERSION", "9.9.9-test")

    parse_calls: list = []
    _original_parse = conf._parse_check_exceptions

    def tracking_parse(raw):
        parse_calls.append(raw)
        if isinstance(raw, str) and raw == "INVALID_NOT_A_LIST":
            raise ValueError("bad check_exceptions shape")
        if not isinstance(raw, list):
            raw = []
        return _original_parse(raw)

    run_all_calls: list = []

    def fake_run_all_checks(*args, **kwargs):
        run_all_calls.append(kwargs)
        result = MagicMock()
        result.findings = []
        result.checked_rule_ids = set()
        return result

    with (
        patch.object(conf, "_get_standards_version", return_value="9.9.9-test"),
        patch.object(conf, "_fetch_yaml", return_value=ecosystem),
        patch.object(conf, "_download_repo", side_effect=fake_download_repo),
        patch.object(conf, "_parse_check_exceptions", side_effect=tracking_parse),
        patch.object(conf, "run_all_checks", side_effect=fake_run_all_checks),
        # Return a real PostResult, not a bare MagicMock: the flow now
        # tallies delivery outcomes and fails the run when nothing
        # reached the API, so a double that does not answer "how many
        # posted?" is not a faithful stand-in for the real function.
        patch.object(conf, "post_findings", return_value=conf.PostResult()),
        patch.object(conf, "_fetch_standards_for_service", return_value=[]),
    ):
        conformance_check_flow(run_llm=False)

    assert len(run_all_calls) == 1


def test_declared_branch_defaults_to_main() -> None:
    """Only a registry entry that says otherwise reads a different ref."""
    assert _declared_branch(None) == "main"
    assert _declared_branch({"id": "watcher-cog"}) == "main"
    assert _declared_branch({"id": "x", "branch": ""}) == "main"
    assert _declared_branch({"id": "x", "branch": "  "}) == "main"


def test_declared_branch_reads_the_registry() -> None:
    """deejaytools-com develops on dev, ten commits ahead of main.

    The run kept reading main and reported the repo for security
    workflows it had on dev — findings no change to the repo could clear.
    """
    assert _declared_branch({"id": "deejaytools-com", "branch": "dev"}) == "dev"
