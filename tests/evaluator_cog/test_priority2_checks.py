"""Deterministic checks: DOC-005, XSTACK-002, FE-009/010, CD-012, PIPE-002/005, narrowed PIPE-008 / XSTACK-001."""

from __future__ import annotations

import json
from pathlib import Path

from evaluator_cog.engine.deterministic import (
    check_adrs_present,
    check_astro_build_time_data,
    check_astro_runtime_queries,
    check_clerk_m2m_auth,
    check_db_writes_use_upserts,
    check_inputs_not_deleted,
    check_no_retired_trigger_patterns,
    check_response_shape_parity,
    check_shared_library_used,
)


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _nontrivial_src_py() -> str:
    return "".join(f"x_{i} = {i}\n" for i in range(55))


# --- DOC-005 -----------------------------------------------------------------


def test_doc005_skips_when_loc_under_threshold(tmp_path: Path) -> None:
    _write(tmp_path, "src/tiny.py", "a = 1\n")
    assert check_adrs_present(tmp_path) == []


def test_doc005_flags_missing_decisions_dir(tmp_path: Path) -> None:
    _write(tmp_path, "src/big.py", _nontrivial_src_py())
    f = check_adrs_present(tmp_path)
    assert any(x["rule_id"] == "DOC-005" for x in f)


def test_doc005_flags_empty_decisions_dir(tmp_path: Path) -> None:
    _write(tmp_path, "src/big.py", _nontrivial_src_py())
    (tmp_path / "docs" / "decisions").mkdir(parents=True, exist_ok=True)
    f = check_adrs_present(tmp_path)
    assert any(x["rule_id"] == "DOC-005" for x in f)


def test_doc005_passes_when_adr_present(tmp_path: Path) -> None:
    _write(tmp_path, "src/big.py", _nontrivial_src_py())
    _write(tmp_path, "docs/decisions/ADR-001-init.md", "# ADR\n")
    assert check_adrs_present(tmp_path) == []


# --- XSTACK-002 --------------------------------------------------------------


def test_xstack002_python_flags_missing_response_model(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        '@router.get("/items")\n'
        "def list_items():\n"
        "    return {}\n",
    )
    f = check_response_shape_parity(tmp_path, language="python")
    assert any(x["rule_id"] == "XSTACK-002" for x in f)


def test_xstack002_python_passes_with_response_model(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        '@router.get("/items", response_model=dict)\n'
        "def list_items():\n"
        "    return {}\n",
    )
    assert check_response_shape_parity(tmp_path, language="python") == []


def test_xstack002_typescript_flags_raw_c_json(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/api.ts",
        "import { Hono } from 'hono'\n"
        "const app = new Hono()\n"
        "app.get('/x', (c) => c.json({ ok: true }))\n",
    )
    f = check_response_shape_parity(tmp_path, language="typescript")
    assert any(x["rule_id"] == "XSTACK-002" for x in f)


def test_xstack002_typescript_passes_with_success_helper(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/api.ts",
        "import { Hono } from 'hono'\n"
        "import { success } from './http'\n"
        "const app = new Hono()\n"
        "app.get('/x', (c) => success(c, { ok: true }))\n",
    )
    assert check_response_shape_parity(tmp_path, language="typescript") == []


# --- FE-009 / FE-010 ---------------------------------------------------------


def test_fe009_no_astro_returns_empty(tmp_path: Path) -> None:
    _write(tmp_path, "src/x.py", "x = 1\n")
    assert check_astro_build_time_data(tmp_path) == []


def test_fe009_flags_runtime_fetch_matching_frontmatter_elsewhere(
    tmp_path: Path,
) -> None:
    url = "https://api.example.com/data"
    _write(
        tmp_path,
        "src/pages/a.astro",
        f"---\nconst _ = await fetch('{url}')\n---\n<div />\n",
    )
    _write(
        tmp_path,
        "src/pages/b.astro",
        f"<script>\nconst r = await fetch('{url}')\n</script>\n",
    )
    f = check_astro_build_time_data(tmp_path)
    assert any(x["rule_id"] == "FE-009" for x in f)


def test_fe009_skips_when_client_directive_present(tmp_path: Path) -> None:
    url = "https://api.example.com/data"
    _write(
        tmp_path,
        "src/pages/a.astro",
        f"---\nconst _ = await fetch('{url}')\n---\n<div />\n",
    )
    _write(
        tmp_path,
        "src/pages/b.astro",
        "<script>\n"
        f"const r = await fetch('{url}')\n"
        "</script>\n"
        "<Counter client:load />\n",
    )
    assert check_astro_build_time_data(tmp_path) == []


def test_fe010_flags_undocumented_runtime_fetch(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/x.astro",
        "<script>\nconst r = await fetch('https://api.secret.example/v1')\n</script>\n",
    )
    f = check_astro_runtime_queries(tmp_path)
    assert any(x["rule_id"] == "FE-010" for x in f)


def test_fe010_passes_when_url_in_readme(tmp_path: Path) -> None:
    u = "https://api.secret.example/v1"
    _write(tmp_path, "README.md", f"Calls `{u}` from the browser.\n")
    _write(
        tmp_path, "src/x.astro", f"<script>\nconst r = await fetch('{u}')\n</script>\n"
    )
    assert check_astro_runtime_queries(tmp_path) == []


def test_fe010_skips_client_island_even_if_undocumented(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/x.astro",
        "<script>\n"
        "const r = await fetch('https://api.secret.example/v1')\n"
        "</script>\n"
        "<Island client:visible />\n",
    )
    assert check_astro_runtime_queries(tmp_path) == []


# --- CD-012 ------------------------------------------------------------------


def test_cd012_flags_x_internal_api_key(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/c.py",
        'HEADERS = {"X-Internal-API-Key": "x"}\n'
        "import httpx\n"
        "httpx.get('https://example.com', headers=HEADERS)\n",
    )
    f = check_clerk_m2m_auth(tmp_path, language="python")
    assert any(x["rule_id"] == "CD-012" for x in f)


def test_cd012_skips_tests_tree_under_src(tmp_path: Path) -> None:
    _write(tmp_path, "src/tests/bad.py", 'HEADERS = {"X-Internal-API-Key": "x"}\n')
    assert check_clerk_m2m_auth(tmp_path, language="python") == []


def test_cd012_passes_when_jwt_pattern_present(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/c.py",
        "import httpx\n"
        "def call():\n"
        "    token = get_token()  # clerk jwt\n"
        "    return httpx.get('https://api.kaianolevine.com/x', headers={'Authorization': token})\n",
    )
    assert check_clerk_m2m_auth(tmp_path, language="python") == []


def test_cd012_flags_internal_httpx_without_jwt_signals(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/c.py",
        "import httpx\n"
        "def call():\n"
        "    return httpx.get('https://api.kaianolevine.com/v1/foo')\n",
    )
    f = check_clerk_m2m_auth(tmp_path, language="python")
    assert any(x["rule_id"] == "CD-012" for x in f)


# --- PIPE-002 / PIPE-005 ------------------------------------------------------


def test_pipe002_flags_session_add_without_upsert_helpers(tmp_path: Path) -> None:
    _write(tmp_path, "src/db.py", "def save(session, row):\n    session.add(row)\n")
    f = check_db_writes_use_upserts(tmp_path)
    assert any(x["rule_id"] == "PIPE-002" for x in f)


def test_pipe002_passes_when_on_conflict_present(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/db.py",
        "def save(session, row):\n"
        "    session.add(row)\n"
        "    stmt = insert(Table).values(x=1).on_conflict_do_nothing()\n",
    )
    assert check_db_writes_use_upserts(tmp_path) == []


def test_pipe002_flags_raw_insert_without_on_conflict(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/db.py",
        'def q():\n    return "INSERT INTO t (a) VALUES (1)"\n',
    )
    f = check_db_writes_use_upserts(tmp_path)
    assert any(x["rule_id"] == "PIPE-002" for x in f)


def test_pipe005_flags_drive_files_delete(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/drive.py",
        "def rm(drive, fid):\n    drive.files().delete(fileId=fid).execute()\n",
    )
    f = check_inputs_not_deleted(tmp_path)
    assert any(x["rule_id"] == "PIPE-005" for x in f)


def test_pipe005_flags_trashed_update(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/drive.py",
        "def trash(service, fid):\n"
        "    return service.files().update(fileId=fid, body={'trashed': True})\n",
    )
    f = check_inputs_not_deleted(tmp_path)
    assert any(x["rule_id"] == "PIPE-005" for x in f)


def test_pipe005_flags_os_remove_on_input_path(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/io.py",
        "import os\ndef clean(input_path):\n    os.remove(input_path)\n",
    )
    f = check_inputs_not_deleted(tmp_path)
    assert any(x["rule_id"] == "PIPE-005" for x in f)


def test_pipe005_ignores_remove_on_static_paths(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/io.py",
        "import os\ndef clean():\n    os.remove('/tmp/scratch.dat')\n",
    )
    assert check_inputs_not_deleted(tmp_path) == []


def test_pipe005_skips_under_src_tests(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/tests/t.py",
        "import os\ndef x(input_path):\n    os.remove(input_path)\n",
    )
    assert check_inputs_not_deleted(tmp_path) == []


# --- PIPE-008 (narrowed) -----------------------------------------------------


def test_pipe008_bare_dispatches_url_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path, "src/t.py", 'url = "https://api.github.com/repos/o/r/dispatches"\n'
    )
    assert check_no_retired_trigger_patterns(tmp_path) == []


def test_pipe008_flags_active_httpx_post_to_dispatches(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/t.py",
        "import httpx\n"
        "httpx.post('https://api.github.com/repos/o/r/dispatches', json={})\n",
    )
    f = check_no_retired_trigger_patterns(tmp_path)
    assert any(x["rule_id"] == "PIPE-008" for x in f)


def test_pipe008_flags_google_app_script_trigger_string(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/legacy.ts",
        "export const hook = 'google-app-script-trigger'\n",
    )
    f = check_no_retired_trigger_patterns(tmp_path)
    assert any(x["rule_id"] == "PIPE-008" for x in f)


def test_pipe008_flags_gh_workflow_run_argv_list(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/cli.py",
        "import subprocess\nsubprocess.run(['gh', 'workflow', 'run', 'ci.yml'])\n",
    )
    f = check_no_retired_trigger_patterns(tmp_path)
    assert any(x["rule_id"] == "PIPE-008" for x in f)


# --- XSTACK-001 (narrowed) ----------------------------------------------------


def test_xstack001_python_flags_missing_dep(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname=x\n")
    f = check_shared_library_used(tmp_path, language="python")
    assert any(x["rule_id"] == "XSTACK-001" for x in f)


def test_xstack001_python_passes_when_dep_declared(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        "[project]\nname=x\ndependencies=['common-python-utils']\n",
    )
    assert check_shared_library_used(tmp_path, language="python") == []


def test_xstack001_ts_hand_rolled_ok_when_dep_declared(tmp_path: Path) -> None:
    pkg = {"name": "x", "dependencies": {"common-typescript-utils": "1.0.0"}}
    _write(tmp_path, "package.json", json.dumps(pkg))
    _write(tmp_path, "src/index.ts", "function createLogger() { return console }\n")
    assert check_shared_library_used(tmp_path, language="typescript") == []


def test_xstack001_ts_workspace_root_dep_satisfies(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"name":"x","dependencies":{}}\n')
    ws = '{"dependencies":{"common-typescript-utils":"1.0.0"}}'
    assert (
        check_shared_library_used(
            tmp_path, language="typescript", workspace_package_json_text=ws
        )
        == []
    )
