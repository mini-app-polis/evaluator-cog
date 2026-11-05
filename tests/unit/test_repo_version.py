"""Which version of the application an evaluation read."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from evaluator_cog.flows import conformance as conf


@pytest.mark.parametrize(
    ("folder", "commit"),
    [
        ("mini-app-polis-api-kaianolevine-com-e2a8dca", "e2a8dca"),
        (
            "mini-app-polis-infra-0123456789abcdef0123456789abcdef01234567",
            "0123456789abcdef0123456789abcdef01234567",
        ),
        ("mini-app-polis-infra-0123456789abcdef", "0123456789abcdef"),
        ("mini-app-polis-infra", None),
        ("mini-app-polis-infra-XYZ1234", None),
    ],
)
def test_the_commit_is_read_from_the_zipball_folder(folder: str, commit) -> None:
    assert conf.zipball_commit(folder) == commit


def test_every_post_carries_the_version_being_evaluated() -> None:
    ctx = conf.RunContext()
    ctx.repo_version = "v1.43.0"
    ctx.repo_commit = "abc1234"
    with patch.object(conf, "post_findings", return_value=conf.PostResult()) as post:
        conf._post_tracked("label", ctx=ctx, findings=[], repo="infra")
    kwargs = post.call_args.kwargs
    assert kwargs["repo_version"] == "v1.43.0"
    assert kwargs["repo_commit"] == "abc1234"


def test_a_caller_that_names_the_version_keeps_its_own() -> None:
    ctx = conf.RunContext()
    ctx.repo_version = "v1.43.0"
    with patch.object(conf, "post_findings", return_value=conf.PostResult()) as post:
        conf._post_tracked("label", ctx=ctx, findings=[], repo_version="main")
    assert post.call_args.kwargs["repo_version"] == "main"


def test_the_version_is_cleared_once_the_repository_is_done() -> None:
    event = conf.EvaluationEvent(
        org="mini-app-polis",
        repo="infra",
        ref="v1.2.0",
        services=({"id": "infra"},),
        run_id="r1",
        standards_version="6.20.0",
    )
    ctx = conf.RunContext()
    seen: dict[str, object] = {}

    def download(*_a, ctx, **_k):
        seen["version"] = ctx.repo_version
        ctx.repo_commit = "abc1234"
        return None

    with (
        patch.object(conf, "_download_repo", side_effect=download),
        patch.object(conf, "_fetch_catalog_schema", return_value={}),
        patch.object(conf, "_fetch_full_rule_catalog", return_value={}),
        patch.object(conf, "post_findings", return_value=conf.PostResult()) as post,
    ):
        conf.handler(event, log=conf.log, ctx=ctx)

    assert seen["version"] == "v1.2.0"
    assert post.call_args.kwargs["repo_version"] == "v1.2.0"
    assert post.call_args.kwargs["repo_commit"] == "abc1234"
    assert ctx.repo_version is None
    assert ctx.repo_commit is None
