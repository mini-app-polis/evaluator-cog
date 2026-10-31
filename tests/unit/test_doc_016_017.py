"""DOC-016 (self-containment) and DOC-017 (no history in comments)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluator_cog.engine.deterministic import check_doc_016, check_doc_017


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


# ── DOC-016 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "# See MINI-123 for why.",
        "URL = 'https://app.asana.com/0/1/2'",
        "# the bucket is declared in main.tf",
        "# added by migration 042",
        "# see 042_add_x.sql",
    ],
)
def test_each_internal_reference_is_a_finding(tmp_path: Path, line: str) -> None:
    repo = _repo(tmp_path, {"src/pkg/mod.py": f"{line}\n"})
    (finding,) = check_doc_016(repo, repo_type="pipeline-cog")
    assert finding["rule_id"] == "DOC-016"
    assert finding["severity"] == "WARN"
    assert "src/pkg/mod.py" in finding["finding"]
    assert "line 1:" in finding["finding"]


def test_every_hit_in_a_file_is_listed_with_its_line(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {"tests/unit/test_x.ts": "// MINI-1\nconst a = 1;\n// see main.tf\n"},
    )
    (finding,) = check_doc_016(repo, repo_type="static-site")
    assert "line 1: MINI-1" in finding["finding"]
    assert "line 3: main.tf" in finding["finding"]


def test_a_clean_repo_and_a_public_contract_are_not_findings(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "src/pkg/mod.py": (
                "# Posts to api-kaianolevine-com's /v1/tasks route,\n"
                "# documented at https://api.kaianolevine.com/docs.\n"
                "# mini-app-polis/infra provisions the queue.\n"
                "x = self_tf = 1\n"
            ),
        },
    )
    assert check_doc_016(repo, repo_type="pipeline-cog") == []


def test_infra_may_name_terraform_and_the_apis_their_migrations(
    tmp_path: Path,
) -> None:
    infra = _repo(tmp_path / "infra", {"tests/test_x.py": "# reads cogs.tf\n"})
    assert check_doc_016(infra, repo_type="infrastructure") == []
    api = _repo(
        tmp_path / "api",
        {"tests/test_m.py": "# migration 042, in 042_add_x.sql\n"},
    )
    assert check_doc_016(api, repo_type="api-service") == []
    # A tracker id is nobody's to cite, whatever the repo.
    api_with_id = _repo(tmp_path / "api2", {"src/a.py": "# MINI-9\n"})
    assert len(check_doc_016(api_with_id, repo_type="api-service")) == 1


def test_documents_are_not_read(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "README.md": "MINI-1 main.tf migration 042\n",
            "CHANGELOG.md": "MINI-2\n",
            "docs/notes.md": "MINI-3\n",
            "docs/decisions/ADR-0001-x.md": "MINI-4\n",
        },
    )
    assert check_doc_016(repo, repo_type="pipeline-cog") == []


# ── DOC-017 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "previously",
        "used to",
        "no longer",
        "was changed",
        "was renamed",
        "moved from",
        "as of 2026-10-29",
        "as of October 2026",
        "originally",
    ],
)
def test_each_phrase_in_a_comment_is_a_finding(tmp_path: Path, phrase: str) -> None:
    repo = _repo(tmp_path, {"src/pkg/mod.py": f"x = 1  # this {phrase} here\n"})
    (finding,) = check_doc_017(repo)
    assert finding["rule_id"] == "DOC-017"
    assert finding["severity"] == "WARN"
    assert (
        f'line 1: "{phrase}"' in finding["finding"].lower()
        or phrase in finding["finding"]
    )


def test_a_docstring_and_a_ts_comment_count(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "src/pkg/mod.py": 'def f():\n    """Originally a loop."""\n    return 1\n',
            "src/web/a.ts": "/* This was renamed\n   last year */\nconst a = 1;\n",
        },
    )
    findings = {f["finding"].split(" ", 1)[0]: f for f in check_doc_017(repo)}
    assert 'line 2: "Originally"' in findings["src/pkg/mod.py"]["finding"]
    assert 'line 1: "was renamed"' in findings["src/web/a.ts"]["finding"]


def test_a_string_literal_or_a_document_is_not_a_finding(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "src/pkg/mod.py": 'MESSAGE = "this is no longer supported"\n',
            "README.md": "This was previously done by hand.\n",
            "CHANGELOG.md": "Originally released.\n",
            "docs/x.md": "no longer\n",
            "docs/decisions/ADR-0002-y.md": "moved from Prefect\n",
        },
    )
    assert check_doc_017(repo) == []


@pytest.mark.parametrize(
    "comment",
    [
        "the key is used to sign the body",
        "the token it's used to verify",
        "a task that no longer passes",
        "a page that no longer exists",
        "so it is not moved from under it",
        "the previously recorded value",
        "a subject no longer than 72 characters",
    ],
)
def test_a_present_tense_use_of_a_phrase_is_not_a_finding(
    tmp_path: Path, comment: str
) -> None:
    repo = _repo(tmp_path, {"src/pkg/mod.py": f"x = 1  # {comment.upper()}\n"})
    assert check_doc_017(repo) == []


def test_a_present_tense_use_wrapped_in_a_docstring_is_not_a_finding(
    tmp_path: Path,
) -> None:
    repo = _repo(
        tmp_path,
        {"src/pkg/mod.py": 'def f():\n    """The key is\n    used to sign."""\n'},
    )
    assert check_doc_017(repo) == []


@pytest.mark.parametrize(
    ("comment", "phrase"),
    [
        ("this module used to poll the queue", "used to"),
        ("the fleet no longer runs on Prefect", "no longer"),
        ("the check moved from the fleet lane", "moved from"),
        ("the branch previously lacked a guard", "previously"),
        ("it was previously reported", "previously"),
        ("a task no longer ready", "no longer"),
    ],
)
def test_the_history_form_of_each_phrase_is_still_a_finding(
    tmp_path: Path, comment: str, phrase: str
) -> None:
    repo = _repo(tmp_path, {"src/pkg/mod.py": f"x = 1  # {comment}\n"})
    (finding,) = check_doc_017(repo)
    assert f'line 1: "{phrase}"' in finding["finding"]


# ── The retired registry ──────────────────────────────────────────────────

_RETIRED = {
    "Oldorch": {"what": "an orchestrator", "replaced_by": "Lambda", "when": "2026-09"},
    "acme-api": {"what": "an API", "replaced_by": "acme-v2", "when": "2026-10"},
}


@pytest.mark.parametrize(
    "line",
    [
        "// (acme-api src/lib/db-errors.ts)",
        "// acme-api lib/cache.ts",
        "# ported to here from Oldorch's runner.py",
        "// see acme-api/src/index.ts",
    ],
)
def test_doc016_flags_a_retired_component_s_file(tmp_path: Path, line: str) -> None:
    repo = _repo(tmp_path, {"src/app/mod.ts": f"{line}\n"})
    (finding,) = check_doc_016(repo, repo_type="api-service", retired=_RETIRED)
    assert "retired" in finding["finding"]
    assert finding["file"] == "src/app/mod.ts"


@pytest.mark.parametrize(
    "line",
    [
        "// acme-api is gone",
        "# Oldorch.serve is not called here",
        "# Oldorchestra src/x.py",
    ],
)
def test_doc016_a_retired_name_without_a_path_is_not_a_file(
    tmp_path: Path, line: str
) -> None:
    repo = _repo(tmp_path, {"src/app/mod.py": f"{line}\nx = 1\n"})
    assert check_doc_016(repo, repo_type="api-service", retired=_RETIRED) == []


def test_doc016_without_a_registry_is_unchanged(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/app/mod.ts": "// acme-api lib/cache.ts\n"})
    assert check_doc_016(repo, repo_type="api-service") == []


@pytest.mark.parametrize(
    "comment",
    [
        "# ported from the old runner",
        "# carried over from the first version",
        "# same as Oldorch did",
        "# as in acme-api",
        "# like Oldorch",
        "# unlike Oldorch",
        "# matching acme-api",
        "# from Oldorch",
        "# replaces Oldorch",
        "# keeps acme-api's wire contract",
    ],
)
def test_doc017_flags_history_and_comparisons(tmp_path: Path, comment: str) -> None:
    repo = _repo(tmp_path, {"src/pkg/mod.py": f"{comment}\nx = 1\n"})
    (finding,) = check_doc_017(repo, retired=_RETIRED)
    assert finding["rule_id"] == "DOC-017"
    assert finding["file"] == "src/pkg/mod.py"
    assert "line 1:" in finding["finding"]


def test_doc017_a_bare_mention_is_not_a_comparison(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/pkg/mod.py": "# Talks to acme-v2.\nx = 1\n"})
    assert check_doc_017(repo, retired=_RETIRED) == []
    repo = _repo(tmp_path, {"src/pkg/mod.py": "# Oldorchestra's queue\nx = 1\n"})
    assert check_doc_017(repo, retired=_RETIRED) == []
