"""Each repository's conformance message leads with what the check found."""

from collections import Counter
from unittest.mock import patch

import mini_app_polis.pipeline_status as ps
import pytest

import evaluator_cog.adapters.queue as q
import evaluator_cog.flows.conformance as conf
from evaluator_cog import __version__


@pytest.fixture
def sent(monkeypatch):
    """Messages as they would be delivered."""
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.test")
    messages: list[dict] = []

    def _capture(message, **_kwargs) -> bool:
        messages.append(message)
        return True

    with patch.object(ps, "_deliver", _capture):
        yield messages


def _description(message: dict) -> str:
    return message["embeds"][0]["description"]


def _event(mode: str) -> conf.EvaluationEvent:
    return conf.EvaluationEvent(
        org="mini-app-polis",
        repo="infra",
        ref="main",
        services=({"id": "infra", "repo": "infra"},),
        run_id=f"{mode}-7.27.4-x",
        mode=mode,
        standards_version="7.27.4",
    )


def _report(mode: str, found: dict[str, int], sent) -> str:
    ctx = conf.RunContext.for_run(conf.flow_name_for_mode(mode))
    conf._record_standards_version("7.27.4", ctx=ctx)
    ctx.tally.attempted = sum(found.values()) or 1
    ctx.tally.posted = ctx.tally.attempted
    ctx.tally.by_severity = Counter(found)
    q._report_run(
        _event(mode), conf.EvaluationResult(repo="infra", evaluated=["infra"]), ctx=ctx
    )
    (message,) = sent
    return _description(message)


def test_a_repo_with_findings_leads_with_them(sent) -> None:
    text = _report("llm", {"WARN": 2, "INFO": 1}, sent)
    first = text.splitlines()[0]
    assert first.startswith(
        "infra@main — LLM check: 2 WARN, 1 INFO (standards 7.27.4, "
    )
    assert first.endswith(f"s) (version={__version__})")
    for word in ("processed", "offered", "posted", "flow=", "target"):
        assert word not in text


def test_a_clean_repo_says_so(sent) -> None:
    text = _report("deterministic", {}, sent)
    assert text.splitlines()[0].startswith(
        "infra@main — deterministic check: clean (standards 7.27.4, "
    )
    assert "(standards=" not in text
    for word in ("processed", "offered", "posted", "flow="):
        assert word not in text


def test_severities_are_listed_most_severe_first() -> None:
    assert (
        conf.repo_check_line(
            "infra@main — LLM check", {"INFO": 3, "ERROR": 1, "WARN": 2}
        )
        == "infra@main — LLM check: 1 ERROR, 2 WARN, 3 INFO"
    )
    assert (
        conf.repo_check_line("x@y — deterministic check", {}, took="2s")
        == "x@y — deterministic check: clean (2s)"
    )


def test_a_repo_not_evaluated_is_still_named_below_the_line(sent) -> None:
    ctx = conf.RunContext.for_run("conformance-check")
    q._report_run(
        _event("llm"),
        conf.EvaluationResult(repo="infra", not_evaluated=["infra"]),
        ctx=ctx,
    )
    text = _description(sent[0])
    assert text.splitlines()[0].startswith("infra@main — LLM check: clean")
    assert "not_evaluated" in text and "infra" in text.splitlines()[-1]


def test_the_tally_counts_what_was_offered_by_severity() -> None:
    from evaluator_cog.engine.api_client import PostResult

    a, b = (
        PostResult(by_severity=Counter({"WARN": 1})),
        PostResult(by_severity=Counter({"WARN": 2, "INFO": 1})),
    )
    a.merge(b)
    assert a.by_severity == {"WARN": 3, "INFO": 1}
