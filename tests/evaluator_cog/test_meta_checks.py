"""Tests for META-001 / META-002 / META-003 standards-repo checks."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from evaluator_cog.engine.deterministic import (
    check_meta_canonical_enums_are_dicts,
    check_meta_no_scattered_metadata,
    check_meta_release_pipeline_wired,
)


def _repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def _workflow_semantic_release() -> str:
    return """name: release
on:
  push:
    branches: [main]
jobs:
  ship:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npx semantic-release
"""


def test_meta001_passes_when_all_four_signals_present() -> None:
    root = _repo(
        {
            ".github/workflows/release.yml": _workflow_semantic_release(),
            ".releaserc.json": '{"branches":["main"]}',
            "package.json": '{"devDependencies":{"semantic-release":"22.0.0"},"scripts":{"release":"semantic-release"}}',
        }
    )
    assert check_meta_release_pipeline_wired(root) == []


def test_meta001_flags_missing_semantic_release_workflow() -> None:
    root = _repo(
        {
            ".github/workflows/ci.yml": "on: push\njobs: {x: {runs-on: ubuntu-latest, steps: [{run: echo hi}]}}",
            ".releaserc.json": "{}",
            "package.json": '{"devDependencies":{"semantic-release":"22.0.0"}}',
        }
    )
    f = check_meta_release_pipeline_wired(root)
    assert any("semantic-release" in x["finding"].lower() for x in f)


def test_meta001_flags_missing_releaserc() -> None:
    root = _repo(
        {
            ".github/workflows/release.yml": _workflow_semantic_release(),
            "package.json": '{"devDependencies":{"semantic-release":"22.0.0"}}',
        }
    )
    f = check_meta_release_pipeline_wired(root)
    assert any("releaserc" in x["finding"].lower() for x in f)


def test_meta001_flags_missing_package_json_wiring() -> None:
    root = _repo(
        {
            ".github/workflows/release.yml": _workflow_semantic_release(),
            ".releaserc.json": "{}",
            "package.json": '{"dependencies":{}}',
        }
    )
    f = check_meta_release_pipeline_wired(root)
    assert any("package.json" in x["finding"].lower() for x in f)


def test_meta001_flags_missing_push_to_main() -> None:
    root = _repo(
        {
            ".github/workflows/release.yml": """on: workflow_dispatch
jobs: {x: {runs-on: ubuntu-latest, steps: [{run: npx semantic-release}]}}""",
            ".releaserc.json": "{}",
            "package.json": '{"devDependencies":{"semantic-release":"22.0.0"}}',
        }
    )
    f = check_meta_release_pipeline_wired(root)
    assert any("main" in x["finding"].lower() for x in f)


def test_meta002_clean_index_and_no_stray_files() -> None:
    root = _repo({"index.yaml": "schema:\n  repo_types: {}\n"})
    assert check_meta_no_scattered_metadata(root) == []


def test_meta002_flags_version_in_index_yaml() -> None:
    root = _repo({"index.yaml": "version: 1.0.0\n"})
    f = check_meta_no_scattered_metadata(root)
    assert any(
        x["rule_id"] == "META-002" and "version" in x["finding"].lower() for x in f
    )


def test_meta002_flags_updated_in_index_yaml() -> None:
    root = _repo({"index.yaml": "updated: 2026-01-01\n"})
    f = check_meta_no_scattered_metadata(root)
    assert any("updated" in x["finding"].lower() for x in f)


def test_meta002_flags_version_txt_stray() -> None:
    root = _repo({"index.yaml": "schema: {}\n", "VERSION.txt": "9.9.9\n"})
    f = check_meta_no_scattered_metadata(root)
    assert any("stray" in x["finding"].lower() for x in f)


def test_meta002_flags_plain_version_file() -> None:
    root = _repo({"index.yaml": "schema: {}\n", "VERSION": "1\n"})
    f = check_meta_no_scattered_metadata(root)
    assert any("VERSION" in x["finding"] for x in f)


def test_meta003_passes_when_enums_are_dicts() -> None:
    root = _repo(
        {
            "index.yaml": """schema:
  repo_types: {a: {label: A}}
  traits: {t: {label: T}}
  dod_types: {d: {label: D}}
  service_statuses: {active: {}}
  rule_severities: {warn: {}}
"""
        }
    )
    assert check_meta_canonical_enums_are_dicts(root) == []


def test_meta003_flags_repo_types_list() -> None:
    root = _repo({"index.yaml": "schema:\n  repo_types:\n    - pipeline-cog\n"})
    f = check_meta_canonical_enums_are_dicts(root)
    assert any("repo_types" in x["finding"] for x in f)


def test_meta003_flags_traits_list() -> None:
    root = _repo({"index.yaml": "schema:\n  traits:\n    - logger-primitive\n"})
    f = check_meta_canonical_enums_are_dicts(root)
    assert any("traits" in x["finding"] for x in f)


def test_meta003_flags_dod_types_list() -> None:
    root = _repo({"index.yaml": "schema:\n  dod_types:\n    - new_cog\n"})
    f = check_meta_canonical_enums_are_dicts(root)
    assert any("dod_types" in x["finding"] for x in f)


def test_meta003_flags_service_statuses_list() -> None:
    root = _repo({"index.yaml": "schema:\n  service_statuses:\n    - active\n"})
    f = check_meta_canonical_enums_are_dicts(root)
    assert any("service_statuses" in x["finding"] for x in f)


def test_meta003_flags_rule_severities_list() -> None:
    root = _repo({"index.yaml": "schema:\n  rule_severities:\n    - WARN\n"})
    f = check_meta_canonical_enums_are_dicts(root)
    assert any("rule_severities" in x["finding"] for x in f)


def test_meta003_malformed_yaml_returns_single_finding() -> None:
    root = _repo({"index.yaml": "{{{not yaml\n"})
    f = check_meta_canonical_enums_are_dicts(root)
    assert len(f) == 1
    assert f[0]["rule_id"] == "META-003"


def test_meta003_missing_index_returns_empty() -> None:
    root = _repo({})
    assert check_meta_canonical_enums_are_dicts(root) == []


def test_meta003_missing_schema_keys_are_ignored() -> None:
    root = _repo({"index.yaml": "title: x\n"})
    assert check_meta_canonical_enums_are_dicts(root) == []


def test_meta001_workflows_dir_missing_still_flags() -> None:
    root = _repo(
        {
            ".releaserc.json": "{}",
            "package.json": '{"devDependencies":{"semantic-release":"1.0.0"}}',
        }
    )
    f = check_meta_release_pipeline_wired(root)
    assert len(f) >= 2


def test_meta002_no_index_skips_index_portion_but_allows_stray_file() -> None:
    root = _repo({"VERSION": "1\n"})
    f = check_meta_no_scattered_metadata(root)
    assert any("stray" in x["finding"].lower() for x in f)


def test_meta002_both_version_and_updated_lines() -> None:
    root = _repo({"index.yaml": "version: 1\nupdated: 2\n"})
    f = check_meta_no_scattered_metadata(root)
    assert sum(1 for x in f if x["rule_id"] == "META-002") >= 2


def test_meta003_multiple_list_enums_multiple_findings() -> None:
    root = _repo({"index.yaml": "schema:\n  repo_types: []\n  traits: []\n"})
    f = check_meta_canonical_enums_are_dicts(root)
    assert len(f) == 2


_REPO_ROOT = Path(__file__).resolve().parents[2]
_POSSIBLE_STANDARDS = _REPO_ROOT.parent / "ecosystem-standards"


@pytest.mark.skipif(
    not _POSSIBLE_STANDARDS.is_dir(),
    reason="Clone ecosystem-standards next to evaluator-cog to run integration check",
)
def test_meta_integration_runs_on_adjacent_standards_clone() -> None:
    std_root = _POSSIBLE_STANDARDS
    # Smoke: must not raise
    check_meta_release_pipeline_wired(std_root)
    check_meta_no_scattered_metadata(std_root)
    check_meta_canonical_enums_are_dicts(std_root)


def test_meta001_semantic_release_in_dependency_only() -> None:
    root = _repo(
        {
            ".github/workflows/release.yml": _workflow_semantic_release(),
            ".releaserc.json": "{}",
            "package.json": '{"dependencies":{"@semantic-release/exec":"0.0.0"}}',
        }
    )
    # @semantic-release/exec contains semantic-release substring in key
    assert check_meta_release_pipeline_wired(root) == []


def test_meta001_push_with_branches_list_variants() -> None:
    wf = """on:
  push:
    branches:
      - main
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: npx semantic-release
"""
    root = _repo(
        {
            ".github/workflows/r.yml": wf,
            ".releaserc.json": "{}",
            "package.json": '{"devDependencies":{"semantic-release":"22.0.0"}}',
        }
    )
    assert check_meta_release_pipeline_wired(root) == []
