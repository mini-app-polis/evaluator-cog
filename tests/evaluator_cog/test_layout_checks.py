"""LAYOUT-001 (required entries) and LAYOUT-002 (drift from the written layout).

The layout here is a trimmed copy of the shape ecosystem-standards
publishes in ``schema.layouts``: base entries with no `types`, Python
entries scoped by type, one owned by another rule, one glob.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.deterministic.layout import (
    check_layout_drift,
    check_layout_required,
)
from evaluator_cog.engine.evaluator_config import load_evaluator_config

PY = ["pipeline-cog", "api-service", "shared-library"]
LAYOUTS = {
    "entries": [
        {"path": ".github/", "required": True, "rule": "CD-026"},
        {"path": ".gitignore", "required": True},
        {"path": "README.md", "required": True, "rule": "DOC-001"},
        {"path": "evaluator.yaml", "required": True, "rule": "EVAL-008"},
        {"path": "LICENSE"},
        {"path": "pyproject.toml", "required": True, "rule": "PY-007", "types": PY},
        {"path": "src/", "required": True, "rule": "PY-005", "types": PY},
        {"path": "tests/", "required": True, "rule": "TEST-021", "types": PY},
        {"path": ".gitattributes", "required": True, "types": PY},
        {"path": "scripts/", "types": PY},
        {"path": "migrations/", "types": ["api-service"]},
        {"path": "*.tf", "required": True, "types": ["infrastructure"]},
        {"path": "modules/", "types": ["infrastructure"]},
    ]
}


def _touch(repo: Path, rel: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")


def _cog(repo: Path) -> Path:
    for rel in (
        ".github/workflows/ci.yml",
        ".gitignore",
        "README.md",
        "evaluator.yaml",
        "pyproject.toml",
        "src/cog/__init__.py",
        "tests/unit/test_x.py",
        ".gitattributes",
    ):
        _touch(repo, rel)
    return repo


def _text(findings: list[dict]) -> str:
    return " || ".join(f["finding"] for f in findings)


# --- LAYOUT-001 ---------------------------------------------------------------


def test_001_complete_repo_passes(tmp_path: Path) -> None:
    assert (
        check_layout_required(_cog(tmp_path), repo_type="pipeline-cog", layouts=LAYOUTS)
        == []
    )


def test_001_reports_required_entries_no_rule_owns(tmp_path: Path) -> None:
    repo = _cog(tmp_path)
    (repo / ".gitignore").unlink()
    (repo / ".gitattributes").unlink()
    findings = check_layout_required(repo, repo_type="pipeline-cog", layouts=LAYOUTS)
    assert len(findings) == 1
    assert ".gitignore" in _text(findings) and ".gitattributes" in _text(findings)


def test_001_leaves_rule_owned_entries_to_their_rule(tmp_path: Path) -> None:
    """A missing README.md is DOC-001's finding, not a second one here."""
    repo = _cog(tmp_path)
    (repo / "README.md").unlink()
    assert check_layout_required(repo, repo_type="pipeline-cog", layouts=LAYOUTS) == []


def test_001_type_scoped_requirements_apply_only_to_their_type(tmp_path: Path) -> None:
    _touch(tmp_path, ".gitignore")
    _touch(tmp_path, "main.tf")
    assert (
        check_layout_required(tmp_path, repo_type="infrastructure", layouts=LAYOUTS)
        == []
    )


def test_001_glob_requirement_unmet_is_reported(tmp_path: Path) -> None:
    _touch(tmp_path, ".gitignore")
    findings = check_layout_required(
        tmp_path, repo_type="infrastructure", layouts=LAYOUTS
    )
    assert "*.tf" in _text(findings)


def test_001_a_file_does_not_satisfy_a_directory_entry(tmp_path: Path) -> None:
    layouts = {"entries": [{"path": "docs/", "required": True}]}
    _touch(tmp_path, "docs")
    assert (
        len(check_layout_required(tmp_path, repo_type="pipeline-cog", layouts=layouts))
        == 1
    )


def test_001_no_layout_in_the_catalog_checks_nothing(tmp_path: Path) -> None:
    assert check_layout_required(tmp_path, repo_type="pipeline-cog", layouts=None) == []


# --- LAYOUT-002 ---------------------------------------------------------------


def test_002_repo_inside_the_layout_passes(tmp_path: Path) -> None:
    repo = _cog(tmp_path)
    _touch(repo, "scripts/backfill.py")
    _touch(repo, "LICENSE")
    assert check_layout_drift(repo, repo_type="pipeline-cog", layouts=LAYOUTS) == []


def test_002_reports_every_unlisted_entry_once(tmp_path: Path) -> None:
    repo = _cog(tmp_path)
    _touch(repo, "Claude outputs/handoff.md")
    _touch(repo, ".coveragerc")
    _touch(repo, ".vscode/settings.json")
    findings = check_layout_drift(repo, repo_type="pipeline-cog", layouts=LAYOUTS)
    assert len(findings) == 1 and findings[0]["severity"] == "INFO"
    for name in ("Claude outputs/", ".coveragerc", ".vscode/"):
        assert name in _text(findings)


def test_002_entries_scoped_to_another_type_are_unlisted(tmp_path: Path) -> None:
    """migrations/ belongs to api-service; in a pipeline cog it is drift."""
    repo = _cog(tmp_path)
    _touch(repo, "migrations/001.sql")
    assert "migrations/" in _text(
        check_layout_drift(repo, repo_type="pipeline-cog", layouts=LAYOUTS)
    )
    assert check_layout_drift(repo, repo_type="api-service", layouts=LAYOUTS) == []


def test_002_layout_exceptions_are_not_reported(tmp_path: Path) -> None:
    repo = _cog(tmp_path)
    _touch(repo, "conformance/package.json")
    exceptions = {"conformance/": "until the rewrite is declared done"}
    assert (
        check_layout_drift(
            repo, repo_type="api-service", layouts=LAYOUTS, exceptions=exceptions
        )
        == []
    )


def test_002_untracked_local_clutter_never_counts(tmp_path: Path) -> None:
    repo = _cog(tmp_path)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    _touch(repo, ".venv/bin/python")
    _touch(repo, ".ruff_cache/x")
    assert check_layout_drift(repo, repo_type="pipeline-cog", layouts=LAYOUTS) == []


def test_002_globs_match_names(tmp_path: Path) -> None:
    for rel in (
        ".gitignore",
        "README.md",
        "evaluator.yaml",
        "main.tf",
        "cogs.tf",
        "modules/x/main.tf",
    ):
        _touch(tmp_path, rel)
    _touch(tmp_path, ".github/workflows/ci.yml")
    assert (
        check_layout_drift(tmp_path, repo_type="infrastructure", layouts=LAYOUTS) == []
    )


# --- evaluator.yaml and the runner ----------------------------------------------


def test_layout_exceptions_are_read_from_evaluator_yaml(tmp_path: Path) -> None:
    (tmp_path / "evaluator.yaml").write_text(
        "type: api-service\nlayout_exceptions:\n"
        "  - path: conformance/\n    reason: until the rewrite is declared done\n",
        encoding="utf-8",
    )
    cfg = load_evaluator_config(tmp_path)
    assert cfg.layout_exceptions == {
        "conformance/": "until the rewrite is declared done"
    }


def test_runner_checks_layout_only_with_a_catalog_layout(tmp_path: Path) -> None:
    repo = _cog(tmp_path)
    _touch(repo, ".coveragerc")
    (repo / "evaluator.yaml").write_text("type: pipeline-cog\n", encoding="utf-8")
    scope = [
        "pipeline-cog",
        "trigger-cog",
        "api-service",
        "shared-library",
        "infrastructure",
    ]
    catalog = {
        rid: {
            "applies_to": scope,
            "severity": sev,
            "status": status,
            "dimension": "structural_conformance",
            "check_mode": "deterministic",
        }
        for rid, sev, status in (
            ("LAYOUT-001", "WARN", "requirement"),
            ("LAYOUT-002", "INFO", "convention"),
        )
    }
    cfg = load_evaluator_config(
        repo, rule_catalog=catalog, catalog_schema={"layouts": LAYOUTS}
    )
    result = run_all_checks(repo, evaluator_config=cfg)
    assert {"LAYOUT-001", "LAYOUT-002"} <= result.checked_rule_ids
    assert any(f["rule_id"] == "LAYOUT-002" for f in result.findings)

    legacy = run_all_checks(repo, dod_type="new_cog", cog_subtype="pipeline")
    assert not {"LAYOUT-001", "LAYOUT-002"} & legacy.checked_rule_ids
