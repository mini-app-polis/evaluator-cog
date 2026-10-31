"""DOC-014 (no retired component described as current) and DOC-009's name
normalisation."""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluator_cog.engine.deterministic import (
    check_doc_014,
    check_split_package_identity,
)

_RETIRED = {
    "Oldorch": {"what": "an orchestrator", "replaced_by": "Lambda", "when": "2026-09"},
    "acme-api": {"what": "an API", "replaced_by": "acme-v2", "when": "2026-10"},
}


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def test_readme_describing_a_retired_component_is_a_finding(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"README.md": "# x\n\nRun it under Oldorch.\n"})
    (finding,) = check_doc_014(repo, retired=_RETIRED)
    assert finding["rule_id"] == "DOC-014"
    assert finding["file"] == "README.md"
    assert "line 3: Oldorch (replaced by: Lambda)" in finding["finding"]


@pytest.mark.parametrize(
    "text",
    [
        "Oldorch was retired in September.",
        "This replaced acme-api.",
        "We moved off Oldorch. It is gone.",
        "It runs on Lambda instead of Oldorch.",
    ],
)
def test_a_sentence_about_the_retirement_is_provenance(
    tmp_path: Path, text: str
) -> None:
    repo = _repo(tmp_path, {"docs/ARCH.md": text + "\n"})
    assert check_doc_014(repo, retired=_RETIRED) == []


def test_terms_match_case_insensitively_within_identifier_bounds(
    tmp_path: Path,
) -> None:
    repo = _repo(
        tmp_path,
        {
            ".env.example": "OLDORCH_API_KEY=\n# Oldorchestra is a different thing\n",
        },
    )
    (finding,) = check_doc_014(repo, retired=_RETIRED)
    assert "line 1: Oldorch" in finding["finding"]
    assert "line 2" not in finding["finding"]


def test_an_env_key_is_never_provenance(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {".env.example": "LEGACY_OLDORCH_URL=\n"})
    assert check_doc_014(repo, retired=_RETIRED)


def test_out_of_scope_files_are_not_read(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "CHANGELOG.md": "Uses Oldorch.\n",
            "docs/decisions/ADR-0001-x.md": "Uses Oldorch.\n",
            "docs/journal.md": "<!-- historical: closed -->\nUses Oldorch.\n",
            "tests/test_x.py": "# Uses Oldorch.\n",
            "src/pkg/mod.py": 'X = "Oldorch"\n',
        },
    )
    assert check_doc_014(repo, retired=_RETIRED) == []


def test_source_comments_and_docstrings_are_read(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "src/pkg/mod.py": (
                '"""Call this when running under Oldorch."""\n'
                "# Oldorch picks this up.\n"
            ),
            "src/app/x.ts": "/* Served by acme-api. */\nconst a = 1;\n",
        },
    )
    findings = {f["file"]: f["finding"] for f in check_doc_014(repo, retired=_RETIRED)}
    assert "line 1: Oldorch" in findings["src/pkg/mod.py"]
    assert "line 2: Oldorch" in findings["src/pkg/mod.py"]
    assert "line 1: acme-api" in findings["src/app/x.ts"]


def test_lines_doc016_or_doc017_report_are_left_to_them(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "src/app/x.ts": (
                "// Matching acme-api here.\n"
                "// See acme-api lib/cache.ts.\n"
                "// Served by acme-api.\n"
            ),
        },
    )
    (finding,) = check_doc_014(repo, retired=_RETIRED)
    assert "line 3: acme-api" in finding["finding"]
    assert "line 1" not in finding["finding"]
    assert "line 2" not in finding["finding"]


def test_no_registry_means_nothing_to_check(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"README.md": "Run under Oldorch.\n"})
    assert check_doc_014(repo) == []
    assert check_doc_014(repo, retired={}) == []


# ── DOC-009 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["my-service", "My.Service", "my__service", "MY_SERVICE"]
)
def test_doc009_pep503_normalised_names_are_not_a_split(
    tmp_path: Path, name: str
) -> None:
    repo = _repo(
        tmp_path,
        {
            "pyproject.toml": f'[project]\nname = "{name}"\n',
            "src/my_service/__init__.py": "",
        },
    )
    assert check_split_package_identity(repo) == []


def test_doc009_a_real_split_still_needs_documenting(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "acme-utils"\n',
            "src/acme/__init__.py": "",
        },
    )
    assert check_split_package_identity(repo)
