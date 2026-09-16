"""The consumer, and the one rule that makes a queue worth having.

A 202 from the old HTTP adapter put the work in one process's memory. A
deploy, an OOM or a restart mid-burst discarded every accepted-but-unstarted
job silently — no findings, no failure, no retry, and each repository's
record sitting at its previous state looking healthy. A message is
different only because it is not deleted until the work is done, so most of
what is pinned here is about when the delete happens.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from evaluator_cog.adapters import queue as q
from evaluator_cog.flows import conformance
from evaluator_cog.flows.conformance import (
    EvaluationEvent,
    EvaluationResult,
    RunContext,
)


def _evaluated(repo: str = "watcher-cog", *, services=("watcher-cog",)):
    """What a real handler hands back. A bare MagicMock will not do: the
    consumer now counts the services a run covered, and a mock counts as
    anything you ask it to."""
    result = EvaluationResult(repo=repo)
    result.evaluated.extend(services)
    return result


@pytest.fixture(autouse=True)
def _no_notifications(monkeypatch):
    """Catch run reports instead of posting them.

    Stubbed at ``_deliver`` rather than at ``RunReport.send``, so the
    severity-and-notable gate above it still runs. That gate is the thing
    under test in the reporting cases: a SUCCESS with ``notable=False`` is
    suppressed and never reaches here, which is exactly the behaviour that
    kept per-repository runs silent.
    """
    # Delivery resolves a base URL before it builds a message, and an
    # unset one short-circuits the whole path with a warning — which
    # reads as "the report was not sent" and would make these tests pass
    # for the wrong reason once the stub is in place.
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.test")
    sent = []

    def _capture(message, **_kwargs) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr("mini_app_polis.pipeline_status._deliver", _capture)
    return sent


def _body(kind: str = q.TYPE_REPOSITORY, version: int = q.MESSAGE_VERSION, **payload):
    if kind == q.TYPE_REPOSITORY and not payload:
        payload = {"repo": "watcher-cog", "ref": "v1.2.3", "mode": "deterministic"}
    return json.dumps({"type": kind, "version": version, "payload": payload})


# ── reading a message ────────────────────────────────────────────────────


def test_a_repository_message_becomes_one_evaluation() -> None:
    with (
        patch.object(q, "handler", return_value=_evaluated()) as handler,
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", return_value="7.0.0"),
    ):
        q.process_message(_body())

    event = handler.call_args.args[0]
    assert (event.repo, event.ref, event.mode) == (
        "watcher-cog",
        "v1.2.3",
        "deterministic",
    )
    assert event.org == "mini-app-polis"
    assert event.services == ({"id": "watcher-cog", "repo": "watcher-cog"},)


def test_an_absent_run_id_is_minted_from_the_catalog_version_now() -> None:
    """Not from the version current when the API accepted the request.

    The run id carries the catalog version the findings are graded against.
    A job can sit on the queue across a standards release, so the version
    is resolved where the grading happens.
    """
    with (
        patch.object(q, "handler", return_value=_evaluated()) as handler,
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", return_value="7.1.0"),
    ):
        q.process_message(_body())

    assert handler.call_args.args[0].run_id.startswith("deterministic-7.1.0-")


def test_a_supplied_run_id_is_used_unchanged() -> None:
    """A fleet pass keeps one run id across every repository in it."""
    with (
        patch.object(q, "handler", return_value=_evaluated()) as handler,
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", side_effect=AssertionError),
    ):
        q.process_message(
            _body(repo="deejay-cog", mode="llm", run_id="conformance-7.0.0-fleet")
        )

    assert handler.call_args.args[0].run_id == "conformance-7.0.0-fleet"


# ── messages this consumer cannot handle ─────────────────────────────────
#
# Left for the redrive policy rather than dropped. Something produced them
# and a person should get to see what.


@pytest.mark.parametrize(
    "body, why",
    [
        ("not json at all", "body is not JSON"),
        (json.dumps([1, 2, 3]), "not an object"),
        (_body(version=99), "message version"),
        (json.dumps({"type": q.TYPE_REPOSITORY, "version": 1}), "no payload"),
        (_body(kind="deejay.transcribe"), "unknown message type"),
        (_body(repo="", mode="deterministic"), "names no repo"),
        (_body(repo="x", mode="guess"), "unknown mode"),
    ],
)
def test_unhandleable_messages_are_refused_not_guessed_at(body: str, why: str) -> None:
    with pytest.raises(q.UnprocessableMessage, match=why):
        q.process_message(body)


def test_an_unrecognised_message_type_is_refused_not_guessed_at() -> None:
    """A producer bug, not another cog's traffic.

    This queue is evaluator-cog's alone — one prefix per cog in `infra/`.
    A shared fleet queue was considered and cannot work, because SQS has no
    selective receive: a consumer takes whatever it is handed, so an
    unrecognised type would send another cog's job to this cog's
    dead-letter queue.

    So a type this consumer does not handle means something enqueued the
    wrong shape. Dead-lettering it deliberately beats crashing in a way
    that reads as this cog's own bug.
    """
    with pytest.raises(q.UnprocessableMessage, match="unknown message type"):
        q.process_message(_body(kind="transcription.run", something="else"))


def test_a_run_that_delivered_nothing_raises_so_it_is_retried() -> None:
    """The property the HTTP adapter could not have.

    A run that computed findings and delivered none of them used to be
    logged and lost. Raising here means the message is never deleted, so
    the work comes back.
    """
    from evaluator_cog.flows.conformance import FindingDeliveryError

    with (
        patch.object(q, "handler", return_value=_evaluated()),
        patch.object(q, "_get_standards_version", return_value="7.0.0"),
        patch.object(
            q,
            "_assert_findings_were_delivered",
            side_effect=FindingDeliveryError("nothing landed"),
        ),
        pytest.raises(FindingDeliveryError),
    ):
        q.process_message(_body())


# ── the loop: when the delete happens ────────────────────────────────────


class _OneShot:
    """A shutdown flag that lets the loop run exactly one iteration."""

    def __init__(self) -> None:
        self._reads = 0

    def install(self) -> None:
        pass

    @property
    def requested(self) -> bool:
        self._reads += 1
        return self._reads > 1


def _run_one(monkeypatch, *, process_side_effect=None):
    """One poll cycle. Returns (the sqs client, the boto3.client factory)."""
    monkeypatch.setenv("EVALUATION_QUEUE_URL", "https://sqs.test/q")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    sqs = MagicMock()
    sqs.receive_message.return_value = {
        "Messages": [
            {
                "ReceiptHandle": "rh-1",
                "Body": _body(),
                "Attributes": {"ApproximateReceiveCount": "1"},
            }
        ]
    }
    with (
        patch.object(q.boto3, "client", return_value=sqs) as factory,
        patch.object(q, "_Shutdown", _OneShot),
        patch.object(q, "process_message", side_effect=process_side_effect),
        patch.object(q, "_report_failure"),
        patch("sentry_sdk.init"),
    ):
        q.main()
    return sqs, factory


def test_a_finished_job_is_deleted(monkeypatch) -> None:
    sqs, _ = _run_one(monkeypatch)
    sqs.delete_message.assert_called_once()
    assert sqs.delete_message.call_args.kwargs["ReceiptHandle"] == "rh-1"


def test_a_failed_job_is_left_on_the_queue(monkeypatch) -> None:
    """Not deleting is the retry. Everything else follows from it."""
    sqs, _ = _run_one(
        monkeypatch, process_side_effect=RuntimeError("the repo exploded")
    )
    sqs.delete_message.assert_not_called()


def test_an_unprocessable_message_is_left_for_the_dead_letter_queue(
    monkeypatch,
) -> None:
    """Dropping it here would hide whatever produced it."""
    sqs, _ = _run_one(
        monkeypatch, process_side_effect=q.UnprocessableMessage("unknown type")
    )
    sqs.delete_message.assert_not_called()


def test_the_consumer_long_polls(monkeypatch) -> None:
    """Short polling bills empty receives and adds latency to every job."""
    sqs, _ = _run_one(monkeypatch)
    assert sqs.receive_message.call_args.kwargs["WaitTimeSeconds"] == 20
    # One job per receive: the visibility timeout is sized for one, and a
    # batch would make the deadline depend on what happened to arrive.
    assert sqs.receive_message.call_args.kwargs["MaxNumberOfMessages"] == 1


def test_the_consumer_uses_its_own_named_credentials(monkeypatch) -> None:
    """Not boto3's AWS_ACCESS_KEY_ID.

    The API holds a send-only key and this holds a receive-only one, and
    the fleet keeps its secrets in one store. Under the conventional names
    the two collide, and this is the side that fails quietly.
    """
    monkeypatch.setenv("EVALUATION_QUEUE_CONSUMER_KEY_ID", "AKIACONSUMER")
    monkeypatch.setenv("EVALUATION_QUEUE_CONSUMER_SECRET", "consumer-secret")
    # Present and wrong for this service — it must be ignored.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAPRODUCER")

    _, factory = _run_one(monkeypatch)
    assert factory.call_args.kwargs["aws_access_key_id"] == "AKIACONSUMER"
    assert factory.call_args.kwargs["aws_secret_access_key"] == "consumer-secret"


def test_absent_credentials_fall_through_to_the_default_chain(monkeypatch) -> None:
    """Step 5: on Lambda the execution role supplies them and no key exists."""
    monkeypatch.delenv("EVALUATION_QUEUE_CONSUMER_KEY_ID", raising=False)
    monkeypatch.delenv("EVALUATION_QUEUE_CONSUMER_SECRET", raising=False)

    _, factory = _run_one(monkeypatch)
    assert "aws_access_key_id" not in factory.call_args.kwargs


def test_it_refuses_to_start_without_a_queue(monkeypatch) -> None:
    """A consumer polling nothing and reporting healthy is the old silence."""
    monkeypatch.delenv("EVALUATION_QUEUE_URL", raising=False)
    with patch("sentry_sdk.init"), pytest.raises(SystemExit):
        q.main()


# ── saying the run happened ──────────────────────────────────────────────


def test_a_repository_run_reports_its_outcome(_no_notifications) -> None:
    """The gap this closes.

    Only sweeps reported. A single repository's evaluation ran, posted its
    findings and said nothing, so a release that triggered one had no
    signal distinguishing "evaluated, clean" from "the job never ran" —
    and for five evaluations eaten by the stub Lambda, those two looked
    identical from the outside.
    """
    with (
        patch.object(q, "handler", return_value=_evaluated()),
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", return_value="7.0.0"),
    ):
        q.process_message(_body())

    assert len(_no_notifications) == 1


def test_a_clean_run_with_no_findings_still_reports(_no_notifications) -> None:
    """A SUCCESS is suppressed unless the caller marks it notable, so this
    is the case that silently did nothing before. Asserted separately from
    the WARN path because they travel different branches of that gate."""
    with (
        patch.object(q, "handler", return_value=_evaluated()),
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", return_value="7.0.0"),
    ):
        q.process_message(_body())

    assert len(_no_notifications) == 1


def test_a_run_that_delivered_nothing_reports_once_not_twice(
    _no_notifications,
) -> None:
    """The delivery assertion raises before the outcome report is built, so
    the failure path stays a single message. Two messages for one event is
    how a channel earns being ignored."""
    from evaluator_cog.flows.conformance import FindingDeliveryError

    with (
        patch.object(q, "handler", return_value=_evaluated()),
        patch.object(q, "_get_standards_version", return_value="7.0.0"),
        patch.object(
            q,
            "_assert_findings_were_delivered",
            side_effect=FindingDeliveryError("nothing landed"),
        ),
        pytest.raises(FindingDeliveryError),
    ):
        q.process_message(_body())

    assert _no_notifications == []


# ── what a fan-out message carries ───────────────────────────────────────


def test_a_fan_out_message_keeps_its_monorepo_grouped() -> None:
    """The property the whole fan-out depends on.

    A monorepo is one job carrying every app in it. Flatten it into one
    message per app and nothing raises — the apps are evaluated, findings
    are posted, the run reports success. What is missing is sibling
    deduplication, which is gated on a job carrying more than one service,
    so the same finding lands once per app and reads as a deduplication
    bug in the evaluator rather than a grouping bug in the dispatcher.
    """
    services = [
        {"id": "shop-web", "repo": "storefront-monorepo"},
        {"id": "shop-admin", "repo": "storefront-monorepo"},
    ]
    record = {"id": "storefront", "repo": "storefront-monorepo"}

    with (
        patch.object(q, "handler", return_value=_evaluated()) as handler,
        patch.object(q, "_assert_findings_were_delivered"),
    ):
        q.process_message(
            _body(
                repo="storefront-monorepo",
                ref="main",
                mode="deterministic",
                run_id="deterministic-7.0.0-abc",
                services=services,
                monorepo=record,
            )
        )

    event = handler.call_args.args[0]
    assert [s["id"] for s in event.services] == ["shop-web", "shop-admin"]
    assert event.monorepo == record


def test_a_release_message_still_synthesises_its_one_service() -> None:
    """CI names a repository and nothing else. The old shape must keep
    working unchanged — the sweep is still the proven path and every
    release-triggered evaluation goes through here."""
    with (
        patch.object(q, "handler", return_value=_evaluated()) as handler,
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", return_value="7.0.0"),
    ):
        q.process_message(_body())

    event = handler.call_args.args[0]
    assert event.services == ({"id": "watcher-cog", "repo": "watcher-cog"},)
    assert event.monorepo is None


def test_a_pinned_catalog_version_is_used_instead_of_resolving_one() -> None:
    """N jobs each resolving their own version would let a catalog release
    landing mid-pass grade some repositories against the old rules and some
    against the new, inside a run id claiming one version for all of them.
    _get_standards_version raises here to prove it is never consulted."""
    with (
        patch.object(q, "handler", return_value=_evaluated()) as handler,
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", side_effect=AssertionError),
    ):
        q.process_message(
            _body(
                repo="watcher-cog",
                run_id="deterministic-7.0.0-abc",
                standards_version="7.0.0",
            )
        )

    assert handler.call_args.args[0].standards_version == "7.0.0"


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"services": ["not-an-object"]}, "services must be a list of objects"),
        ({"monorepo": "not-an-object"}, "monorepo must be an object"),
    ],
)
def test_a_malformed_grouping_is_refused_not_guessed_at(payload, why) -> None:
    """Dead-letter it rather than silently falling back to the synthesised
    single service, which would evaluate a monorepo as one app and look
    like a clean run."""
    with pytest.raises(q.UnprocessableMessage, match=why):
        q.process_message(_body(repo="watcher-cog", run_id="r-1", **payload))


# ── what a suppressed finding means ──────────────────────────────────────


def _run_with_tally(*, attempted: int, duplicates: int, details=("CD-021 …",)):
    """Drive one repository message with a pre-loaded delivery tally."""

    def _handler(event, *, log, ctx):
        ctx.tally.attempted = attempted
        ctx.tally.posted = attempted - duplicates
        ctx.tally.duplicates = duplicates
        ctx.tally.duplicate_details.extend(details)
        return _evaluated()

    captured = {}

    def _send(self, *, notable=False, **kw):
        captured["severity"] = self.severity
        captured["text"] = self.text()
        return None

    with (
        patch.object(q, "handler", _handler),
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", return_value="7.0.0"),
        patch("mini_app_polis.pipeline_status.RunReport.send", _send),
    ):
        q.process_message(_body())

    return captured


@pytest.mark.parametrize("duplicates", [7, 2])
def test_suppressed_findings_never_raise_the_run(duplicates) -> None:
    """Both sources of these are ordinary.

    All seven means the job had already been done under this run id — a
    redelivered message, or the release workflow's retry landing twice.
    At-least-once delivery working as designed.

    Two of seven means one repository failed a rule the same way twice —
    CD-026 emits one finding per offending job, and identical text is one
    thing to fix rather than two. Collapsing them is the point of the
    fingerprint.

    An earlier version raised the second case to WARN on the theory that
    it meant a rule misbehaving. It does not, and the only thing the
    severity bought was a channel full of warnings about deduplication
    working.
    """
    report = _run_with_tally(attempted=7, duplicates=duplicates)

    assert report["severity"] == "SUCCESS"


def test_suppressed_findings_are_counted_and_not_named() -> None:
    """RunReport renders examples only for flagged reasons, deliberately —
    "naming every already-processed file is how the interesting line ends
    up below the fold". The rule ids live in the consumer's log instead."""
    report = _run_with_tally(
        attempted=7, duplicates=2, details=("CD-021 for api was already stored",)
    )

    assert "CD-021" not in report["text"]


# ── the report is titled like the rows it describes ──────────────────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("deterministic", "deterministic-conformance"), ("llm", "conformance-check")],
)
def test_the_run_report_is_filed_under_the_findings_flow_name(mode, expected) -> None:
    """It was hardcoded to the llm name, so a deterministic run announced
    itself as conformance-check while writing rows under
    deterministic-conformance."""
    seen = {}

    def _handler(event, *, log, ctx):
        seen["flow_name"] = ctx.report.flow_name
        return _evaluated()

    with (
        patch.object(q, "handler", _handler),
        patch.object(q, "_assert_findings_were_delivered"),
        patch.object(q, "_get_standards_version", return_value="7.0.0"),
    ):
        q.process_message(_body(repo="watcher-cog", ref="main", mode=mode))

    assert seen["flow_name"] == expected


def test_the_handler_does_not_report_on_its_own() -> None:
    """Where the per-job report lives, and why it is not in handler.

    RunReport.send is once-per-instance. A fleet pass is N calls to
    handler sharing nothing, so a report built there would be one per
    repository — which is what we want — but an introspection job also
    calls into the same module and must keep its own. Putting the report
    in the consumer's branch is what keeps each job's report its own.

    This guards the placement rather than the count: move the report into
    handler and the introspection job's summary is spent by whatever ran
    first.
    """
    ctx = RunContext.for_run("deterministic-conformance")
    event = EvaluationEvent(
        org="mini-app-polis",
        repo="watcher-cog",
        ref="main",
        services=({"id": "watcher-cog", "repo": "watcher-cog"},),
        run_id="deterministic-7.0.0-abc",
        mode="deterministic",
    )

    with (
        patch.object(conformance, "_get_standards_version", return_value="7.0.0"),
        patch.object(conformance, "_fetch_catalog_schema", return_value={}),
        patch.object(conformance, "_fetch_full_rule_catalog", return_value={}),
        patch.object(conformance, "_download_repo", return_value=None),
        patch.object(conformance, "_report_issue"),
        patch.object(conformance, "_post_not_evaluated"),
    ):
        conformance.handler(event, log=MagicMock(), ctx=ctx)

    assert ctx.report is not None
    assert not ctx.report._sent
