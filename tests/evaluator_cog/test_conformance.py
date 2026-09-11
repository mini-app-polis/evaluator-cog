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
    _declared_org,
    _fetch_full_rule_catalog,
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


def _catalog(rules: list[dict], **extra) -> dict:
    """A catalog document in the shape the API serves it.

    Only the fields a test asserts on need setting. The evaluator reads one
    of these per flow run instead of assembling one from seventeen YAML
    files, so this is the seam every catalog-shaped test patches.
    """
    doc: dict = {"version": "9.9.9-test", "rule_count": len(rules), "rules": rules}
    doc.update(extra)
    return doc


def _rule(rule_id: str, **overrides) -> dict:
    """One compiled rule, with the fields the compiler always emits."""
    rule = {
        "id": rule_id,
        "domain": "pipeline",
        "title": f"{rule_id} title",
        "status": "requirement",
        "severity": "ERROR",
        "checkable": True,
        "check_mode": "deterministic",
        "check_notes": "DETERMINISTIC CHECK. Scan something.",
        "applies_to": ["all"],
        "modifies": [],
    }
    rule.update(overrides)
    return rule


#: The default catalog for tests that need one but do not assert on it.
_FAKE_CATALOG = _catalog(
    [
        _rule("PIPELINE-RULE", applies_to=["pipeline-cog"]),
        _rule("LEGACY-COG-RULE", applies_to=["new_cog"]),
        _rule("EVERYWHERE-RULE", applies_to=["all"]),
    ]
)


def _patch_catalog(catalog: dict):
    """Patch the one fetch every catalog-derived helper goes through."""
    return patch("evaluator_cog.flows.conformance._fetch_catalog", return_value=catalog)


def test_fetch_standards_matches_new_repo_type() -> None:
    """A rule whose applies_to names the repo's type is in scope."""
    service = {"id": "x", "dod_type": "new_cog"}
    cfg = EvaluatorConfig(repo_type="pipeline-cog")
    with _patch_catalog(_FAKE_CATALOG):
        rules = _fetch_standards_for_service(service, cfg)
    assert "PIPELINE-RULE" in {r["id"] for r in rules}


def test_fetch_standards_falls_back_to_dod_type_when_no_evaluator_cfg() -> None:
    """With no evaluator config, applies_to matches on the legacy dod_type."""
    service = {"id": "x", "dod_type": "new_cog"}
    with _patch_catalog(_FAKE_CATALOG):
        rules = _fetch_standards_for_service(service, None)
    assert "LEGACY-COG-RULE" in {r["id"] for r in rules}


def test_fetch_standards_includes_all_scoped_rules_regardless_of_type() -> None:
    """`[all]` is the catalog's default posture and matches every repo."""
    cfg = EvaluatorConfig(repo_type="static-site")
    with _patch_catalog(_FAKE_CATALOG):
        rules = _fetch_standards_for_service({"id": "x"}, cfg)
    ids = {r["id"] for r in rules}
    assert "EVERYWHERE-RULE" in ids
    assert "PIPELINE-RULE" not in ids


def test_unchecked_rules_are_filtered_from_every_catalog_view() -> None:
    """`checkable: false` rules ship in the catalog but are never evaluated.

    They are carried so they stay readable and joinable to the findings of
    versions that did check them. The evaluator has no check to run and
    emits nothing for them — see the `gap` status in index.yaml.
    """
    catalog = _catalog(
        [
            _rule("LIVE-001"),
            _rule("RETIRED-001", checkable=False, check_mode=None, check_notes=""),
        ]
    )
    with _patch_catalog(catalog):
        scoped = _fetch_standards_for_service({"id": "x"}, None)
        full = _fetch_full_rule_catalog()

    assert {r["id"] for r in scoped} == {"LIVE-001"}
    assert set(full) == {"LIVE-001"}


def test_fetch_standards_rejects_invalid_rule_status() -> None:
    """Only requirement / convention / gap are valid statuses."""
    catalog = _catalog([_rule("BAD-001", status="advisory")])
    with _patch_catalog(catalog), pytest.raises(ValueError, match="invalid status"):
        _fetch_standards_for_service(
            {"id": "x"}, EvaluatorConfig(repo_type="pipeline-cog")
        )


def test_fetch_catalog_schema_parses_v4_shape() -> None:
    """Traits keep their structured exempts and downgrades."""
    from evaluator_cog.flows.conformance import _fetch_catalog_schema

    catalog = _catalog(
        [_rule("X-001")],
        statuses={
            "requirement": {"description": "must comply"},
            "convention": {"description": "should comply"},
            "gap": {"description": "tracked deficiency"},
        },
        schema={
            "repo_types": {"pipeline-cog": "desc", "api-service": "desc"},
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
    )
    with _patch_catalog(catalog):
        schema = _fetch_catalog_schema()

    assert schema["repo_types"] == {"pipeline-cog", "api-service"}
    assert schema["statuses"] == {"requirement", "convention", "gap"}
    assert schema["traits"]["logger-primitive"]["exempts"] == ["CD-009"]
    assert schema["traits"]["multi-flow"]["downgrades"] == [
        {"rule": "CD-015", "to": "INFO", "reason": "scanner limit"}
    ]


def test_fetch_catalog_schema_tolerates_a_catalog_without_schema_blocks() -> None:
    """Missing blocks yield empty structures rather than raising.

    A catalog that cannot be fetched at all is a different case and raises —
    see test_conformance_helpers.
    """
    from evaluator_cog.flows.conformance import _fetch_catalog_schema

    with _patch_catalog(_catalog([_rule("X-001")])):
        schema = _fetch_catalog_schema()

    assert schema["traits"] == {}
    assert schema["repo_types"] == set()
    assert schema["statuses"] == set()


def test_fetch_full_rule_catalog_captures_applies_to_and_modifies() -> None:
    """Dispatch metadata comes through, with applies_to None for non-scans."""
    catalog = _catalog(
        [
            _rule(
                "MONO-001",
                applies_to=["api-service", "react-app"],
                modifies=["XSTACK-001"],
                dimension="monorepo_coherence",
            ),
            # Not a repo-source scan: the compiler collapses both an absent
            # and an explicitly empty applies_to to None (ADR-004).
            _rule("MONO-003", applies_to=None, dimension="monorepo_coherence"),
            _rule("EVAL-005", applies_to=["all"], check_mode="llm"),
        ]
    )
    with _patch_catalog(catalog):
        full = _fetch_full_rule_catalog()

    assert full["MONO-001"]["applies_to"] == ["api-service", "react-app"]
    assert full["MONO-001"]["modifies"] == ["XSTACK-001"]
    assert full["MONO-001"]["dimension"] == "monorepo_coherence"
    assert full["MONO-003"]["applies_to"] is None
    # check_mode arrives resolved rather than parsed out of check_notes.
    assert full["EVAL-005"]["check_mode"] == "llm"
    assert full["MONO-001"]["check_mode"] == "deterministic"


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

    def fake_download_repo(repo_name, tmp_dir, branch="main", org="mini-app-polis"):
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
        patch.object(conf, "_fetch_catalog", return_value=_FAKE_CATALOG),
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


def test_declared_org_defaults_to_the_fleet_org() -> None:
    """Almost every repo omits the field and resolves under mini-app-polis."""
    assert _declared_org(None) == "mini-app-polis"
    assert _declared_org({"id": "watcher-cog"}) == "mini-app-polis"
    assert _declared_org({"id": "x", "org": ""}) == "mini-app-polis"
    assert _declared_org({"id": "x", "org": "  "}) == "mini-app-polis"


def test_declared_org_reads_the_registry() -> None:
    """website-astro-wcs lives in a personal org, not the fleet org.

    With the org hardcoded, its download 404'd on every run: registered,
    carrying an evaluator.yaml, and never once evaluated.
    """
    assert (
        _declared_org({"id": "website-astro-wcs", "org": "kaianolevine"})
        == "kaianolevine"
    )


def _posted_findings(post_calls: list) -> list[dict]:
    """Flatten every finding handed to post_findings across all calls."""
    out: list[dict] = []
    for kwargs in post_calls:
        out.extend(kwargs.get("findings") or [])
    return out


def test_undownloadable_repo_is_reported_not_silently_skipped(monkeypatch) -> None:
    """A service that cannot be cloned must still produce a row.

    It used to produce nothing at all: the flow logged "could not clone"
    and moved on, so the service vanished from the report. A reader then
    saw only the services that *did* evaluate — every one of them green —
    and had no way to tell that one had never been looked at. Absence is
    not a pass.
    """
    import evaluator_cog.flows.conformance as conf

    ecosystem = {
        "services": [
            {
                "id": "reachable",
                "repo": "reachable",
                "status": "active",
                "type": "api-service",
                "language": "python",
            },
            {
                "id": "gone",
                "repo": "gone",
                "status": "active",
                "type": "api-service",
                "language": "python",
            },
        ]
    }

    def fake_download_repo(repo_name, tmp_dir, branch="main", org="mini-app-polis"):
        if repo_name == "gone":
            return None
        root = Path(tmp_dir) / repo_name
        root.mkdir(parents=True, exist_ok=True)
        return root

    def fake_run_all_checks(*args, **kwargs):
        result = MagicMock()
        result.findings = []
        result.checked_rule_ids = set()
        return result

    post_calls: list = []

    def tracking_post(**kwargs):
        post_calls.append(kwargs)
        return conf.PostResult()

    monkeypatch.setenv("STANDARDS_VERSION", "9.9.9-test")
    with (
        patch.object(conf, "_get_standards_version", return_value="9.9.9-test"),
        patch.object(conf, "_fetch_yaml", return_value=ecosystem),
        patch.object(conf, "_fetch_catalog", return_value=_FAKE_CATALOG),
        patch.object(conf, "_download_repo", side_effect=fake_download_repo),
        patch.object(conf, "run_all_checks", side_effect=fake_run_all_checks),
        patch.object(conf, "post_findings", side_effect=tracking_post),
        patch.object(conf, "_fetch_standards_for_service", return_value=[]),
    ):
        conformance_check_flow(run_llm=False)

    findings = _posted_findings(post_calls)
    gone = [f for f in findings if "gone" in f.get("finding", "")]
    assert gone, "the unreachable service posted nothing at all"
    assert gone[0]["severity"] == "ERROR"
    assert "not evaluated" in gone[0]["finding"]

    # The reachable one still reports normally — the new row must not
    # replace or suppress the ordinary path.
    assert any(
        f.get("severity") == "SUCCESS" and "reachable" in f.get("finding", "")
        for f in findings
    )


def test_failed_checks_are_reported_not_silently_skipped(monkeypatch) -> None:
    """A service whose checks raise must produce a row saying so.

    Same failure shape as an unreachable repo, one step later: the flow
    caught the exception, logged it, and returned without posting, so a
    crashing check made a service disappear rather than fail.
    """
    import evaluator_cog.flows.conformance as conf

    ecosystem = {
        "services": [
            {
                "id": "explodes",
                "repo": "explodes",
                "status": "active",
                "type": "api-service",
                "language": "python",
            },
        ]
    }

    def fake_download_repo(repo_name, tmp_dir, branch="main", org="mini-app-polis"):
        root = Path(tmp_dir) / repo_name
        root.mkdir(parents=True, exist_ok=True)
        return root

    def boom(*args, **kwargs):
        raise RuntimeError("check exploded")

    post_calls: list = []

    def tracking_post(**kwargs):
        post_calls.append(kwargs)
        return conf.PostResult()

    monkeypatch.setenv("STANDARDS_VERSION", "9.9.9-test")
    with (
        patch.object(conf, "_get_standards_version", return_value="9.9.9-test"),
        patch.object(conf, "_fetch_yaml", return_value=ecosystem),
        patch.object(conf, "_fetch_catalog", return_value=_FAKE_CATALOG),
        patch.object(conf, "_download_repo", side_effect=fake_download_repo),
        patch.object(conf, "run_all_checks", side_effect=boom),
        patch.object(conf, "post_findings", side_effect=tracking_post),
        patch.object(conf, "_fetch_standards_for_service", return_value=[]),
    ):
        conformance_check_flow(run_llm=False)

    findings = _posted_findings(post_calls)
    assert findings, "a raising check posted nothing at all"
    assert findings[0]["severity"] == "ERROR"
    assert "not evaluated" in findings[0]["finding"]
    assert "RuntimeError" in findings[0]["finding"]


def test_transient_download_failure_is_retried(monkeypatch) -> None:
    """A 403 that clears must not cost a service its whole run.

    GitHub's secondary rate limit returns 403 and lifts in seconds. The
    single-attempt download treated that as fatal, so a service dropped
    out of the report entirely — and the failures arrived in contiguous
    blocks, four consecutive services in one run, which is what a burst
    limit looks like rather than a broken repo.
    """
    import evaluator_cog.flows.conformance as conf

    calls: list[int] = []

    class _Resp:
        def __init__(self, status: int, content: bytes = b"") -> None:
            self.status_code = status
            self.content = content
            self.headers: dict[str, str] = {}

        @property
        def is_success(self) -> bool:
            return 200 <= self.status_code < 300

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get(self, _url, headers=None):  # noqa: ARG002
            calls.append(1)
            # Throttled twice, then it clears — exactly the shape the
            # secondary rate limit produces.
            if len(calls) < 3:
                return _Resp(403)
            return _Resp(200, b"zipbytes")

    monkeypatch.setattr(conf.httpx, "Client", lambda **_kw: _Client())
    monkeypatch.setattr(conf.time, "sleep", lambda _s: None)

    got = conf._fetch_zipball("http://x", {}, 60.0, "deejay-cog")
    assert got == b"zipbytes"
    assert len(calls) == 3


def test_missing_repo_is_not_retried(monkeypatch) -> None:
    """A 404 is a wrong name or branch — retrying cannot change it."""
    import evaluator_cog.flows.conformance as conf

    calls: list[int] = []

    class _Resp:
        status_code = 404
        content = b""
        headers: dict[str, str] = {}
        is_success = False

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get(self, _url, headers=None):  # noqa: ARG002
            calls.append(1)
            return _Resp()

    monkeypatch.setattr(conf.httpx, "Client", lambda **_kw: _Client())
    monkeypatch.setattr(conf.time, "sleep", lambda _s: None)

    assert conf._fetch_zipball("http://x", {}, 60.0, "gone-cog") is None
    assert len(calls) == 1


def test_retry_delay_honours_retry_after(monkeypatch) -> None:
    """Guessing shorter than the server asked for is how storms start."""
    import evaluator_cog.flows.conformance as conf

    class _Resp:
        headers = {"Retry-After": "7"}

    assert conf._retry_delay(_Resp(), 0) == 7.0

    class _Huge:
        headers = {"Retry-After": "99999"}

    # Capped, so one long value cannot stall the run.
    assert conf._retry_delay(_Huge(), 0) == conf._DOWNLOAD_BACKOFF_CAP_SECONDS
    # No header: exponential backoff.
    assert conf._retry_delay(None, 0) == conf._DOWNLOAD_BACKOFF_SECONDS
    assert conf._retry_delay(None, 1) == conf._DOWNLOAD_BACKOFF_SECONDS * 2
