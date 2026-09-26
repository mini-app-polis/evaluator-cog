"""Tests for run_conformance_check and the post_llm_only posting behaviour."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import evaluator_cog.flows.conformance as conf_mod
from evaluator_cog.engine.deterministic import CheckResult
from evaluator_cog.engine.evaluator_config import EvaluatorConfig
from evaluator_cog.flows.conformance import (
    RunContext,
    _fetch_full_rule_catalog,
    _fetch_standards_for_service,
    _run_standalone_deterministic,
    run_conformance_check,
)

#: The sweep takes its logger rather than resolving one. Tests that drive it
#: only need the calls not to fail.
_LOG = logging.getLogger("test-sweep")


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
        patch.object(conf_mod, "log", MagicMock()),
    ):
        mock_client.from_env.return_value = api
        result = run_conformance_check(
            ctx=RunContext(),
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
        patch.object(conf_mod, "log", MagicMock()),
    ):
        mock_client.from_env.return_value = api
        run_conformance_check(
            ctx=RunContext(),
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
        patch.object(conf_mod, "log", MagicMock()),
    ):
        mock_client.from_env.return_value = api
        run_conformance_check(
            ctx=RunContext(),
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


@pytest.mark.parametrize(
    "llm_patch",
    [
        {"return_value": "Here is my assessment: everything looks fine."},
        {"side_effect": RuntimeError("404 Not Found")},
    ],
    ids=["unreadable-reply", "request-failed"],
)
def test_llm_not_assessed_never_posts_success(monkeypatch, llm_patch) -> None:
    """An LLM half that produced no assessment is not a pass."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test.example.com")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=MagicMock(return_value={}))
    report = MagicMock()

    with (
        patch(
            "evaluator_cog.flows.conformance._anthropic_messages_create",
            **llm_patch,
        ),
        patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as mock_client,
        patch.object(conf_mod, "log", MagicMock()),
    ):
        mock_client.from_env.return_value = api
        run_conformance_check(
            ctx=RunContext(report=report),
            repo_id="test-repo",
            repo_path=_minimal_repo(),
            standards_version="2.5.1",
            post=True,
            post_llm_only=True,
            run_id="conformance-2.5.1-test",
        )

    assert len(posted) == 1
    assert posted[0]["severity"] == "WARN"
    assert "not assessed against the LLM checks" in posted[0]["finding"]
    assert "passed" not in posted[0]["finding"]
    report.issue.assert_called_once()
    assert report.issue.call_args.args[:2] == ("llm_assessment_failed", "test-repo")


def test_llm_skipped_without_key_never_posts_success(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://test.example.com")

    posted: list[dict] = []

    def _fake_post(path: str, payload: dict) -> dict:
        posted.append(payload)
        return {}

    api = SimpleNamespace(post=_fake_post, get=MagicMock(return_value={}))
    with (
        patch("evaluator_cog.engine.api_client.CommonPythonApiClient") as mock_client,
        patch.object(conf_mod, "log", MagicMock()),
    ):
        mock_client.from_env.return_value = api
        run_conformance_check(
            ctx=RunContext(),
            repo_id="test-repo",
            repo_path=_minimal_repo(),
            standards_version="2.5.1",
            post=True,
            post_llm_only=True,
            run_id="conformance-2.5.1-test",
        )

    assert [p["severity"] for p in posted] == ["WARN"]
    assert "ANTHROPIC_API_KEY is not set" in posted[0]["finding"]


def test_run_conformance_check_posts_with_conformance_llm_source(monkeypatch) -> None:
    """run_conformance_check() posts all findings with source='conformance_llm'.

    Note: this helper is used by the LLM path (mode='llm'). The
    deterministic-only path goes through
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
        patch.object(conf_mod, "log", MagicMock()),
    ):
        mock_client.from_env.return_value = api
        run_conformance_check(
            ctx=RunContext(),
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
        rules = _fetch_standards_for_service(service, cfg, ctx=RunContext())
    assert "PIPELINE-RULE" in {r["id"] for r in rules}


def test_fetch_standards_falls_back_to_dod_type_when_no_evaluator_cfg() -> None:
    """With no evaluator config, applies_to matches on the legacy dod_type."""
    service = {"id": "x", "dod_type": "new_cog"}
    with _patch_catalog(_FAKE_CATALOG):
        rules = _fetch_standards_for_service(service, None, ctx=RunContext())
    assert "LEGACY-COG-RULE" in {r["id"] for r in rules}


def test_fetch_standards_includes_all_scoped_rules_regardless_of_type() -> None:
    """`[all]` is the catalog's default posture and matches every repo."""
    cfg = EvaluatorConfig(repo_type="static-site")
    with _patch_catalog(_FAKE_CATALOG):
        rules = _fetch_standards_for_service({"id": "x"}, cfg, ctx=RunContext())
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
        ctx = RunContext()
        scoped = _fetch_standards_for_service({"id": "x"}, None, ctx=ctx)
        full = _fetch_full_rule_catalog(ctx=ctx)

    assert {r["id"] for r in scoped} == {"LIVE-001"}
    assert set(full) == {"LIVE-001"}


def test_fetch_standards_rejects_invalid_rule_status() -> None:
    """Only requirement / convention / gap are valid statuses."""
    catalog = _catalog([_rule("BAD-001", status="advisory")])
    with _patch_catalog(catalog), pytest.raises(ValueError, match="invalid status"):
        _fetch_standards_for_service(
            {"id": "x"}, EvaluatorConfig(repo_type="pipeline-cog"), ctx=RunContext()
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
        schema = _fetch_catalog_schema(ctx=RunContext())

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
        schema = _fetch_catalog_schema(ctx=RunContext())

    assert schema["traits"] == {}
    assert schema["repo_types"] == set()
    assert schema["statuses"] == set()


def test_fetch_full_rule_catalog_captures_applies_to_and_modifies() -> None:
    """Dispatch metadata comes through, with applies_to None for non-scans."""
    catalog = _catalog(
        [
            _rule(
                "MOD-001",
                applies_to=["api-service", "react-app"],
                modifies=["XSTACK-001"],
                dimension="cross_repo_coherence",
            ),
            # Not a repo-source scan: the compiler collapses both an absent
            # and an explicitly empty applies_to to None (ADR-004).
            _rule("EVAL-003", applies_to=None, dimension="standards_currency"),
            _rule("EVAL-005", applies_to=["all"], check_mode="llm"),
        ]
    )
    with _patch_catalog(catalog):
        full = _fetch_full_rule_catalog(ctx=RunContext())

    assert full["MOD-001"]["applies_to"] == ["api-service", "react-app"]
    assert full["MOD-001"]["modifies"] == ["XSTACK-001"]
    assert full["MOD-001"]["dimension"] == "cross_repo_coherence"
    assert full["EVAL-003"]["applies_to"] is None
    # check_mode arrives resolved rather than parsed out of check_notes.
    assert full["EVAL-005"]["check_mode"] == "llm"
    assert full["MOD-001"]["check_mode"] == "deterministic"


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
            ctx=RunContext(),
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


def _posted_findings(post_calls: list) -> list[dict]:
    """Flatten every finding handed to post_findings across all calls."""
    out: list[dict] = []
    for kwargs in post_calls:
        out.extend(kwargs.get("findings") or [])
    return out


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

    got = conf._fetch_zipball("http://x", {}, 60.0, "deejay-cog", ctx=conf.RunContext())
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

    assert (
        conf._fetch_zipball("http://x", {}, 60.0, "gone-cog", ctx=conf.RunContext())
        is None
    )
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


# ---------------------------------------------------------------------------
# Delivering a service's findings
# ---------------------------------------------------------------------------


def test_post_service_findings_substitutes_the_success_row() -> None:
    """Evaluated-and-clean must not look like never-evaluated."""
    import evaluator_cog.flows.conformance as conf

    with patch.object(conf, "_post_tracked") as post:
        conf._post_service_findings(
            "some-repo",
            [],
            ctx=conf.RunContext(),
            standards_version="9.9.9",
            run_id="r",
            flow_name="deterministic-conformance",
            prefect_log=MagicMock(),
        )

    findings = post.call_args.kwargs["findings"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "SUCCESS"
    assert "some-repo" in findings[0]["finding"]


# ---------------------------------------------------------------------------
# handler — the unit of work
# ---------------------------------------------------------------------------


def _svc(service_id: str, **extra) -> dict:
    service = {
        "id": service_id,
        "repo": service_id,
        "status": "active",
        "type": "api-service",
        "language": "python",
    }
    service.update(extra)
    return service


def _findings(*texts: str):
    def _run(*args, **kwargs):
        result = MagicMock()
        result.findings = [
            {
                "rule_id": "X-001",
                "dimension": "structural_conformance",
                "severity": "WARN",
                "finding": text,
                "suggestion": "",
            }
            for text in texts
        ]
        result.checked_rule_ids = set()
        return result

    return _run


def test_handler_evaluates_a_standalone_repo(tmp_path) -> None:
    """One repository, one service, findings delivered."""
    import evaluator_cog.flows.conformance as conf

    def download(repo_name, tmp_dir, branch="main", org="mini-app-polis", **_kw):
        root = Path(tmp_dir) / repo_name
        root.mkdir(parents=True, exist_ok=True)
        return root

    posted: list[dict] = []

    def capture(**kwargs):
        posted.append(kwargs)
        return conf.PostResult(attempted=1, posted=1)

    event = conf.EvaluationEvent(
        org="mini-app-polis",
        repo="watcher-cog",
        ref="main",
        services=(_svc("watcher-cog"),),
        run_id="r-1",
    )

    with (
        patch.object(conf, "_fetch_catalog", return_value=_FAKE_CATALOG),
        patch.object(conf, "_get_standards_version", return_value="9.9.9-test"),
        patch.object(conf, "_download_repo", side_effect=download),
        patch.object(conf, "run_all_checks", side_effect=_findings("something")),
        patch.object(conf, "post_findings", side_effect=capture),
    ):
        result = conf.handler(event, log=MagicMock(), ctx=conf.RunContext())

    assert result.evaluated == ["watcher-cog"]
    assert result.not_evaluated == []
    assert [c["repo"] for c in posted] == ["watcher-cog"]
    assert posted[0]["flow_name"] == "deterministic-conformance"


def test_handler_reports_every_service_when_the_download_fails() -> None:
    """A failed download hides each service, so each gets its own row.

    The repository is not something the report has a column for — the
    services it hid are what went unevaluated.
    """
    import evaluator_cog.flows.conformance as conf

    posted: list[dict] = []

    def capture(**kwargs):
        posted.append(kwargs)
        return conf.PostResult(attempted=1, posted=1)

    event = conf.EvaluationEvent(
        org="mini-app-polis",
        repo="mono",
        ref="main",
        services=(_svc("app-a"), _svc("app-b")),
        run_id="r-2",
    )

    with (
        patch.object(conf, "_fetch_catalog", return_value=_FAKE_CATALOG),
        patch.object(conf, "_get_standards_version", return_value="9.9.9-test"),
        patch.object(conf, "_download_repo", return_value=None),
        patch.object(conf, "post_findings", side_effect=capture),
    ):
        result = conf.handler(event, log=MagicMock(), ctx=conf.RunContext())

    assert result.not_evaluated == ["app-a", "app-b"]
    assert result.evaluated == []
    assert {c["repo"] for c in posted} == {"app-a", "app-b"}


# ---------------------------------------------------------------------------
# _get_with_retry — the catalog and ecosystem.yaml fetches
# ---------------------------------------------------------------------------


class _Responses:
    """Stands in for httpx.get: hands out one scripted outcome per call."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.urls: list[str] = []

    def __call__(self, url: str, **_kw: object) -> object:
        self.urls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(status: int) -> object:
    import httpx

    return httpx.Response(status, request=httpx.Request("GET", "https://x"))


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr(conf_mod.time, "sleep", delays.append)
    return delays


def test_get_with_retry_retries_a_503(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    fake = _Responses(_response(503), _response(200))
    monkeypatch.setattr(conf_mod.httpx, "get", fake)
    assert conf_mod._get_with_retry("https://x", timeout=1).status_code == 200
    assert len(fake.urls) == 2
    assert no_sleep == [2.0]


def test_get_with_retry_returns_a_404_at_once(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    fake = _Responses(_response(404))
    monkeypatch.setattr(conf_mod.httpx, "get", fake)
    assert conf_mod._get_with_retry("https://x", timeout=1).status_code == 404
    assert no_sleep == []


def test_get_with_retry_returns_the_last_failure(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    fake = _Responses(_response(502), _response(502), _response(502))
    monkeypatch.setattr(conf_mod.httpx, "get", fake)
    assert conf_mod._get_with_retry("https://x", timeout=1).status_code == 502
    assert len(fake.urls) == 3


def test_get_with_retry_raises_the_last_transport_error(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    import httpx

    fake = _Responses(*(httpx.ConnectError("down") for _ in range(3)))
    monkeypatch.setattr(conf_mod.httpx, "get", fake)
    with pytest.raises(httpx.ConnectError):
        conf_mod._get_with_retry("https://x", timeout=1)
    assert len(fake.urls) == 3


def test_fetch_catalog_survives_one_transient_failure(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    import httpx

    ok = httpx.Response(
        200,
        json={"data": {"version": "7.0.0", "rules": [{"id": "X-1"}]}},
        request=httpx.Request("GET", "https://x"),
    )
    monkeypatch.setattr(conf_mod.httpx, "get", _Responses(_response(503), ok))
    ctx = conf_mod.RunContext()
    assert conf_mod._fetch_catalog(ctx=ctx)["version"] == "7.0.0"
