"""The checks that belong to no repository, and how they get run now.

Six of them — EVAL-003, MONO-003, XSTACK-006, XSTACK-007, XSTACK-008 and
EVAL-007 — carry ``applies_to: None``. They grade the inventory, the
stored findings and the catalog, so there is no per-repository invocation
any of them belongs to. They ran at the tail of ``run_fleet_sweep``
because that was the one place in the old design that happened once per
pass.

Fan-out removed that place. Deleting the sweep without replacing it would
have stopped all six silently: nothing goes red, no repository's record
changes, and six checks that grade whether the registry and the catalog
still agree just stop — which looks exactly like all six passing.

XSTACK-008 is the one that does not decouple cleanly, and most of what is
pinned here is about it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from evaluator_cog.adapters import queue as q
from evaluator_cog.flows import conformance as c


def _body(**payload) -> str:
    return json.dumps(
        {
            "type": q.TYPE_INTROSPECTION,
            "version": q.MESSAGE_VERSION,
            "payload": payload,
        }
    )


@pytest.fixture(autouse=True)
def _no_notifications(monkeypatch):
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.test")
    sent = []

    def _capture(message, **_kwargs) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr("mini_app_polis.pipeline_status._deliver", _capture)
    return sent


# ── the message ──────────────────────────────────────────────────────────


def test_an_introspection_message_runs_the_checks() -> None:
    with (
        patch.object(q, "run_introspection") as run,
        patch.object(q, "_assert_findings_were_delivered"),
    ):
        q.process_message(_body(run_id="introspection-7.0.0-abc"))

    assert run.call_args.kwargs["run_id"] == "introspection-7.0.0-abc"


def test_a_pass_to_grade_is_forwarded() -> None:
    """Only XSTACK-008 reads it, and only to say which registered
    repositories did not resolve in that pass."""
    with (
        patch.object(q, "run_introspection") as run,
        patch.object(q, "_assert_findings_were_delivered"),
    ):
        q.process_message(
            _body(
                run_id="introspection-7.0.0-abc",
                pass_run_id="deterministic-7.0.0-xyz",
            )
        )

    assert run.call_args.kwargs["pass_run_id"] == "deterministic-7.0.0-xyz"


def test_an_introspection_message_without_a_run_id_is_refused() -> None:
    """Findings need a run to be filed under. Minting one here would put
    them in a run the dispatcher never named and cannot query."""
    with pytest.raises(q.UnprocessableMessage, match="names no run_id"):
        q.process_message(_body(pass_run_id="deterministic-7.0.0-xyz"))


def test_an_introspection_run_reports_its_outcome(_no_notifications) -> None:
    """These six are the checks nobody would notice had stopped — no
    release goes red when they quietly do nothing."""
    with (
        patch.object(q, "run_introspection"),
        patch.object(q, "_assert_findings_were_delivered"),
    ):
        q.process_message(_body(run_id="introspection-7.0.0-abc"))

    assert len(_no_notifications) == 1


# ── XSTACK-008's input, rebuilt from the table ───────────────────────────


def _rows(*findings: str) -> dict:
    return {"data": [{"finding": f} for f in findings]}


def test_a_404_row_is_recognised_and_rebuilt() -> None:
    """The check parses org, repo and branch back out of a zipball URL, so
    it is handed the shape it already understands rather than taught about
    rows."""
    api = MagicMock()
    api.get.return_value = _rows(
        c._NOT_FOUND_REASON_FMT.format(
            org="mini-app-polis", repo="ghost-cog", ref="main"
        )
    )

    with patch("mini_app_polis.api.KaianoApiClient.from_env", return_value=api):
        found = c._unresolved_from_run("run-1", log=MagicMock())

    assert found == [
        {
            "label": "mini-app-polis/ghost-cog",
            "url": "https://api.github.com/repos/mini-app-polis/ghost-cog/zipball/main",
        }
    ]


def test_an_unreachable_repo_is_not_reported_as_missing() -> None:
    """The invariant this whole path exists to preserve.

    A 403, 429, 5xx or timeout means the run could not tell whether the
    repository is there. XSTACK-008 reports a registry entry that does not
    resolve, and 'I could not tell' must never be collapsed into 'it is
    not there' — that would file an ERROR against a repository whose only
    crime was being downloaded during a GitHub incident.
    """
    api = MagicMock()
    api.get.return_value = _rows(
        "watcher-cog was declared active but was not evaluated in this run: "
        "the repository could not be downloaded (watcher-cog@main). Its "
        "conformance is unknown, not clean."
    )

    with patch("mini_app_polis.api.KaianoApiClient.from_env", return_value=api):
        assert c._unresolved_from_run("run-1", log=MagicMock()) == []


def test_ordinary_findings_are_not_mistaken_for_missing_repos() -> None:
    api = MagicMock()
    api.get.return_value = _rows(
        "CD-026: the release job does not depend on the security job",
        "STATUS: deejay-cog evaluated clean",
    )

    with patch("mini_app_polis.api.KaianoApiClient.from_env", return_value=api):
        assert c._unresolved_from_run("run-1", log=MagicMock()) == []


def test_the_same_repo_twice_is_one_entry() -> None:
    """A monorepo posts one not-evaluated row per app, all naming the same
    repository. Reporting it twice would put two identical XSTACK-008
    findings in one run."""
    reason = c._NOT_FOUND_REASON_FMT.format(
        org="mini-app-polis", repo="ghost-mono", ref="main"
    )
    api = MagicMock()
    api.get.return_value = _rows(reason, reason)

    with patch("mini_app_polis.api.KaianoApiClient.from_env", return_value=api):
        assert len(c._unresolved_from_run("run-1", log=MagicMock())) == 1


def test_no_pass_to_grade_means_no_query() -> None:
    """Five of the six checks need nothing from any run, so an
    introspection without a pass is valid rather than an error."""
    with patch("mini_app_polis.api.KaianoApiClient.from_env") as client:
        assert c._unresolved_from_run("", log=MagicMock()) == []

    assert not client.called


def test_an_unreadable_run_does_not_take_the_other_checks_down() -> None:
    """Weakness worth naming rather than hiding: this path can only
    under-report. An empty list is also what a healthy fleet produces, so
    a failure here is invisible in the findings — which is why it logs."""
    log = MagicMock()
    with patch(
        "mini_app_polis.api.KaianoApiClient.from_env", side_effect=OSError("down")
    ):
        assert c._unresolved_from_run("run-1", log=log) == []

    assert log.warning.called


# ── the flow ─────────────────────────────────────────────────────────────


def test_the_unresolved_list_reaches_the_checks() -> None:
    ctx = c.RunContext.for_run()
    rebuilt = [{"label": "mini-app-polis/ghost-cog", "url": "https://x/zipball/main"}]

    with (
        patch.object(c, "_get_standards_version", return_value="7.0.0"),
        patch.object(c, "_fetch_full_rule_catalog", return_value={"CD-026": {}}),
        patch.object(c, "_fetch_yaml", return_value={"services": []}),
        patch.object(c, "_unresolved_from_run", return_value=rebuilt),
        patch.object(c, "_run_applies_to_absent_checks", return_value=6) as checks,
    ):
        c.run_introspection(
            run_id="introspection-7.0.0-abc",
            pass_run_id="deterministic-7.0.0-xyz",
            log=MagicMock(),
            ctx=ctx,
        )

    assert ctx.unresolved_downloads == rebuilt
    assert checks.call_args.kwargs["run_id"] == "introspection-7.0.0-abc"


def test_a_pinned_version_is_used_instead_of_resolving_one() -> None:
    """Same argument as the fan-out: the dispatcher resolved a catalog
    version for this pass, and resolving a second one here could grade
    against something the pass never saw."""
    ctx = c.RunContext.for_run()

    with (
        patch.object(c, "_get_standards_version", side_effect=AssertionError),
        patch.object(c, "_fetch_full_rule_catalog", return_value={}),
        patch.object(c, "_fetch_yaml", return_value={"services": []}),
        patch.object(c, "_unresolved_from_run", return_value=[]),
        patch.object(c, "_run_applies_to_absent_checks", return_value=6) as checks,
    ):
        c.run_introspection(
            run_id="introspection-7.0.0-abc",
            standards_version="7.0.0",
            log=MagicMock(),
            ctx=ctx,
        )

    assert checks.call_args.kwargs["standards_version"] == "7.0.0"


# ── the writing half of the invariant ────────────────────────────────────


def _handler_download_failure(*, was_404: bool) -> str:
    """Run handler against a repo whose download failed, return the reason
    it recorded."""
    ctx = c.RunContext.for_run()
    event = c.EvaluationEvent(
        org="mini-app-polis",
        repo="ghost-cog",
        ref="main",
        services=({"id": "ghost-cog", "repo": "ghost-cog"},),
        run_id="deterministic-7.0.0-abc",
        standards_version="7.0.0",
    )

    def _download(repo, tmp_dir, ref, org, *, ctx):
        # What _fetch_zipball does on a 404, and does not do on anything
        # else: a 403, 429, 5xx or timeout leaves this list untouched.
        if was_404:
            ctx.unresolved_downloads.append(
                {"label": f"{org}/{repo}", "url": "https://x/zipball/main"}
            )
        return None

    captured: list[str] = []

    def _capture(_repo_id, reason, **_kwargs) -> None:
        captured.append(reason)

    with (
        patch.object(c, "_fetch_catalog_schema", return_value={}),
        patch.object(c, "_fetch_full_rule_catalog", return_value={}),
        patch.object(c, "_download_repo", _download),
        patch.object(c, "_report_issue"),
        patch.object(c, "_post_not_evaluated", side_effect=_capture),
    ):
        c.handler(event, log=MagicMock(), ctx=ctx)

    assert len(captured) == 1
    return captured[0]


def test_a_404_is_recorded_as_a_missing_repository() -> None:
    """The row has to carry the 404, because nothing else will. Under
    fan-out each repository is its own job with its own context, so the
    in-memory list the sweep relied on is gone by the time anything reads
    it."""
    reason = _handler_download_failure(was_404=True)

    assert c._NOT_FOUND_RE.search(reason)
    assert "mini-app-polis/ghost-cog@main" in reason


def test_an_unreachable_repo_is_not_recorded_as_missing() -> None:
    """The other side of the same invariant, and the one that costs
    something when it breaks.

    Collapse these two and XSTACK-008 files an ERROR against every
    repository that happened to be downloaded during a GitHub incident,
    saying the registry points somewhere that does not exist. Revert the
    404 check in handler and this fails; the reader tests above do not,
    because they only see rows that were already written correctly.
    """
    reason = _handler_download_failure(was_404=False)

    assert not c._NOT_FOUND_RE.search(reason)
    assert "could not be downloaded" in reason


# ── the report says whose run it was, and what it did ────────────────────


def _introspect(*, completed: int = 6):
    ctx = c.RunContext.for_run("introspection")
    with (
        patch.object(c, "_get_standards_version", return_value="7.0.0"),
        patch.object(c, "_fetch_full_rule_catalog", return_value={}),
        patch.object(c, "_fetch_yaml", return_value={"services": []}),
        patch.object(c, "_unresolved_from_run", return_value=[]),
        patch.object(c, "_run_applies_to_absent_checks", return_value=completed),
    ):
        c.run_introspection(run_id="introspection-7.0.0-abc", log=MagicMock(), ctx=ctx)
    return ctx


def test_the_report_is_attributed_to_this_run() -> None:
    """Without it RunReport falls back to get_run_id(), whose resolution
    order is Prefect's — and with Prefect gone that always lands on
    "local-run", joinable to nothing. The first pass shipped without this
    and every introspection report said local-run."""
    assert _introspect().report.run_id == "introspection-7.0.0-abc"


def test_a_complete_pass_reports_what_it_ran() -> None:
    """An empty tally renders "nothing to do" — over a pass that had just
    posted a finding, which is the opposite of true."""
    report = _introspect().report

    assert report.processed == 6
    assert "nothing to do" not in report.text()


def test_a_partial_pass_says_so() -> None:
    """A check that raises is logged and skipped and the run goes on, so
    "five of six ran" and "six ran and found nothing" are otherwise the
    same silence."""
    report = _introspect(completed=5).report

    assert report.severity == "WARN"
    assert "check_failed" in report.text()
