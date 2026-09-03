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


# --- dimension integrity -----------------------------------------------------


def _emitted_dimensions() -> list[tuple[str, int, str, str]]:
    """(file, line, rule_id, dimension) for every _finding() in the package.

    Resolves ``CHECK_ID`` per enclosing function. Collecting it as a
    module-level constant instead attributes every call in a module to
    whichever function was walked last, which is how a first pass at
    this audit produced a confidently wrong answer.
    """
    import ast

    from evaluator_cog.engine.deterministic import _shared

    pkg = Path(_shared.__file__).parent
    out: list[tuple[str, int, str, str]] = []
    for py in sorted(pkg.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        module_consts = {
            n.targets[0].id: n.value.value
            for n in tree.body
            if isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        }
        functions = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for fn in functions:
            local = dict(module_consts)
            for n in ast.walk(fn):
                if (
                    isinstance(n, ast.Assign)
                    and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str)
                ):
                    local[n.targets[0].id] = n.value.value

            def resolve(node, scope=local):
                if isinstance(node, ast.Constant):
                    return node.value
                if isinstance(node, ast.Name):
                    return scope.get(node.id)
                return None

            for n in ast.walk(fn):
                if not (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "_finding"
                    and len(n.args) >= 3
                ):
                    continue
                rule_id, dimension = resolve(n.args[0]), resolve(n.args[2])
                if rule_id and dimension and rule_id != "CHECKER":
                    out.append((py.name, n.lineno, rule_id, dimension))
    return out


def test_every_emitted_dimension_is_one_the_catalog_declares() -> None:
    """A dimension the catalog does not define is a phantom bucket.

    `dimension` is a free-form string in the evaluator and in the API's
    request schema, so nothing rejects an invented one — it simply
    appears in Pipeline Health's GROUP BY as a category that does not
    exist. `test_coverage` sat beside the real `testing_coverage` for
    months, splitting the record between them.
    """
    import yaml

    index = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "evaluator.yaml").read_text()
    )
    assert index is not None  # sanity: the repo's own config parses

    declared = {
        "structural_conformance",
        "pipeline_consistency",
        "pipeline_reliability",
        "testing_coverage",
        "documentation_coverage",
        "cd_readiness",
        "cross_repo_coherence",
        "standards_currency",
        "monorepo_coherence",
        "security_posture",
        "operational_readiness",
    }
    bad = [
        (f, ln, rid, dim)
        for f, ln, rid, dim in _emitted_dimensions()
        if dim not in declared
    ]
    assert not bad, "dimensions the catalog does not declare:\n" + "\n".join(
        f"  {f}:{ln} {rid} -> {dim!r}" for f, ln, rid, dim in bad
    )


def test_no_rule_emits_two_different_dimensions() -> None:
    """One rule reports in one dimension.

    A rule split across two dimensions is counted twice in the
    dashboard's breakdown and fully in neither. PIPE-001 emitted
    `pipeline_consistency` from four call sites while the catalog filed
    it under `structural_conformance`.
    """
    from collections import defaultdict

    by_rule: dict[str, set[str]] = defaultdict(set)
    for _f, _ln, rule_id, dimension in _emitted_dimensions():
        by_rule[rule_id].add(dimension)
    split = {r: sorted(d) for r, d in by_rule.items() if len(d) > 1}
    assert not split, f"rules emitting more than one dimension: {split}"
