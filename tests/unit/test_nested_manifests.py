"""XSTACK-003 and SEC-007 read nested manifests, and API-008 accepts every
public-intent phrase DOC-011 does."""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.deterministic.auth import check_unauthenticated_routes
from evaluator_cog.engine.deterministic.delivery import check_pnpm_lockfile
from evaluator_cog.engine.deterministic.docs import (
    check_unauthenticated_routes_documented,
)
from evaluator_cog.engine.deterministic.security import check_sec_007
from evaluator_cog.engine.evaluator_config import EvaluatorConfig

_PKG = '{"name": "x", "dependencies": {"hono": "^4.0.0"}}'


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


# ── XSTACK-003 ──────────────────────────────────────────────────────────────


def test_xstack003_flags_a_nested_npm_lockfile_in_a_hidden_directory(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        {
            "package.json": _PKG,
            "pnpm-lock.yaml": "",
            ".railway/package.json": _PKG,
            ".railway/package-lock.json": "{}",
        },
    )
    findings = check_pnpm_lockfile(tmp_path)
    assert [(f["severity"], f["file"]) for f in findings] == [
        ("ERROR", ".railway/package-lock.json"),
        ("WARN", ".railway/package.json"),
    ]


def test_xstack003_flags_a_yarn_lockfile_at_any_depth(tmp_path: Path) -> None:
    _write(tmp_path, {"web/package.json": _PKG, "web/yarn.lock": ""})
    findings = check_pnpm_lockfile(tmp_path)
    assert {(f["severity"], f["file"]) for f in findings} == {
        ("ERROR", "web/yarn.lock"),
        ("WARN", "web/package.json"),
    }


def test_xstack003_passes_when_each_package_has_its_pnpm_lockfile(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        {
            "package.json": _PKG,
            "pnpm-lock.yaml": "",
            ".railway/package.json": _PKG,
            ".railway/pnpm-lock.yaml": "",
        },
    )
    assert check_pnpm_lockfile(tmp_path) == []


def test_xstack003_skips_node_modules_and_git(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "package.json": _PKG,
            "pnpm-lock.yaml": "",
            "node_modules/dep/package.json": _PKG,
            "node_modules/dep/package-lock.json": "{}",
            ".git/hooks/package.json": _PKG,
        },
    )
    assert check_pnpm_lockfile(tmp_path) == []


def test_xstack003_has_no_subject_without_a_package_json(tmp_path: Path) -> None:
    _write(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    assert check_pnpm_lockfile(tmp_path) == []


def test_xstack003_runs_for_a_python_api_service(tmp_path: Path) -> None:
    """applies_to names the repo type, not a language: a Python service's
    nested package is held to pnpm too."""
    _write(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname = 'x'\n",
            ".railway/package.json": _PKG,
            ".railway/package-lock.json": "{}",
        },
    )
    cfg = EvaluatorConfig(repo_type="api-service")
    cfg.rule_catalog = {
        "XSTACK-003": {
            "applies_to": ["api-service", "react-app"],
            "modifies": [],
            "status": "requirement",
        }
    }
    cfg.catalog_schema = {"traits": {}}
    result = run_all_checks(tmp_path, language="python", evaluator_config=cfg)
    assert "XSTACK-003" in result.checked_rule_ids
    assert {f["file"] for f in result.findings if f.get("rule_id") == "XSTACK-003"} == {
        ".railway/package-lock.json",
        ".railway/package.json",
    }


# ── SEC-007 ─────────────────────────────────────────────────────────────────


def test_sec007_counts_a_nested_package_as_javascript(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname = 'x'\n",
            ".railway/package.json": _PKG,
            ".github/dependabot.yml": (
                "version: 2\nupdates:\n  - package-ecosystem: uv\n    directory: /\n"
            ),
        },
    )
    findings = check_sec_007(tmp_path)
    assert len(findings) == 1
    assert "JavaScript" in findings[0]["finding"]


def test_sec007_ignores_packages_under_node_modules(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname = 'x'\n",
            "node_modules/dep/package.json": _PKG,
            ".github/dependabot.yml": (
                "version: 2\nupdates:\n  - package-ecosystem: uv\n    directory: /\n"
            ),
        },
    )
    assert check_sec_007(tmp_path) == []


# ── API-008 accepts DOC-011's phrasings ─────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "Intentionally public",
        "No auth required",
        "Read-only public",
        "Public endpoint",
    ],
)
@pytest.mark.parametrize("where", ["description", "docstring"])
def test_api008_accepts_every_doc011_phrase(
    tmp_path: Path, phrase: str, where: str
) -> None:
    if where == "description":
        route = (
            f'@app.get("/stats", description="{phrase}: counts only.")\n'
            "def stats():\n    return {}\n"
        )
    else:
        route = (
            '@app.get("/stats")\n'
            f'def stats():\n    """{phrase}: counts only."""\n    return {{}}\n'
        )
    _write(
        tmp_path,
        {"src/app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n" + route},
    )
    assert check_unauthenticated_routes_documented(tmp_path) == []
    assert check_unauthenticated_routes(tmp_path) == []


def test_api008_still_flags_an_undocumented_public_route(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "src/app/main.py": (
                "from fastapi import FastAPI\napp = FastAPI()\n"
                '@app.post("/things")\ndef things():\n    return {}\n'
            )
        },
    )
    assert [f["rule_id"] for f in check_unauthenticated_routes(tmp_path)] == ["API-008"]
