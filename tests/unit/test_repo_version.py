"""Which version of the application an evaluation read."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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


# -- the release tag on the commit read ---------------------------------------


def _refs(*pairs: tuple[str, str, str]) -> list[dict]:
    return [
        {"ref": f"refs/tags/{name}", "object": {"type": kind, "sha": sha}}
        for name, kind, sha in pairs
    ]


def _tag(refs, commit: str = "eb761c4"):
    response = MagicMock()
    response.json.return_value = refs
    with patch.object(conf.httpx, "get", return_value=response) as get:
        tag = conf.release_tag("mini-app-polis", "website", commit, {}, 5.0)
    assert get.call_args.args[0] == (
        "https://api.github.com/repos/mini-app-polis/website/git/matching-refs/tags"
    )
    return tag


def test_the_tag_on_the_commit_is_its_version() -> None:
    refs = _refs(
        ("v2.1.2", "commit", "0000000aaaa"),
        ("v2.1.3", "commit", "eb761c462614ed1502d6d237eb85795d1665f49f"),
    )
    assert _tag(refs) == "v2.1.3"


def test_of_two_tags_on_one_commit_the_highest_is_its_version() -> None:
    refs = _refs(
        ("v2", "commit", "eb761c4abc"),
        ("v2.10.0", "commit", "eb761c4abc"),
        ("v2.9.0", "commit", "eb761c4abc"),
    )
    assert _tag(refs) == "v2.10.0"


def test_a_commit_no_release_tags_has_no_tag() -> None:
    assert _tag(_refs(("v2.1.2", "commit", "0000000aaaa"))) is None


def test_an_annotated_tag_is_not_matched() -> None:
    assert _tag(_refs(("v2.1.3", "tag", "eb761c4abc"))) is None


def test_github_failing_leaves_the_version_alone() -> None:
    with patch.object(conf.httpx, "get", side_effect=RuntimeError("down")):
        assert conf.release_tag("o", "r", "eb761c4", {}, 5.0) is None


# -- an evaluation pinned to a commit ------------------------------------------

_SHA = "0123456789abcdef0123456789abcdef01234567"


def _run_pinned(*, phase: str, tag: str | None):
    """Drive the handler for a sha-pinned event; return what it read and posted."""
    event = conf.EvaluationEvent(
        org="mini-app-polis",
        repo="infra",
        ref="main",
        services=({"id": "infra"},),
        run_id="r1",
        standards_version="6.20.0",
        sha=_SHA,
        phase=phase,
    )
    ctx = conf.RunContext()
    seen: dict[str, object] = {}

    def download(_repo, _tmp, ref, *_a, ctx, **_k):
        seen["ref"] = ref
        seen["version"] = ctx.repo_version
        seen["phase"] = ctx.phase
        # What _download_repo does: the abbreviated commit from the zipball
        # folder, and the release tag on it when there is one.
        ctx.repo_commit = _SHA[:7]
        if tag:
            ctx.repo_version = tag
        return None

    with (
        patch.object(conf, "_download_repo", side_effect=download),
        patch.object(conf, "_fetch_catalog_schema", return_value={}),
        patch.object(conf, "_fetch_full_rule_catalog", return_value={}),
        patch.object(conf, "post_findings", return_value=conf.PostResult()) as post,
    ):
        conf.handler(event, log=conf.log, ctx=ctx)
    return seen, post.call_args.kwargs, ctx


def test_a_pre_release_evaluation_reads_the_sha_with_no_tag_required() -> None:
    seen, posted, ctx = _run_pinned(phase="pre-release", tag=None)

    assert seen["ref"] == _SHA
    # A sha is not a version: with no tag on it, there is none.
    assert seen["version"] is None
    assert posted["repo_version"] is None
    assert posted["repo_commit"] == _SHA
    assert posted["phase"] == "pre-release"
    assert (ctx.repo_version, ctx.repo_commit, ctx.phase) == (None, None, None)


def test_a_post_release_evaluation_records_the_tag_on_the_sha() -> None:
    seen, posted, _ctx = _run_pinned(phase="post-release", tag="v1.44.0")

    assert seen["phase"] == "post-release"
    assert posted["repo_version"] == "v1.44.0"
    assert posted["repo_commit"] == _SHA
    assert posted["phase"] == "post-release"


def test_an_evaluation_with_no_phase_posts_none() -> None:
    ctx = conf.RunContext()
    with patch.object(conf, "post_findings", return_value=conf.PostResult()) as post:
        conf._post_tracked("label", ctx=ctx, findings=[], repo="infra")
    assert post.call_args.kwargs["phase"] is None
