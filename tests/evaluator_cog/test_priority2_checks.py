"""Deterministic checks: DOC-005, XSTACK-002, FE-009/010, PIPE-002/005, narrowed PIPE-008 / XSTACK-001."""

from __future__ import annotations

import json
from pathlib import Path

from evaluator_cog.engine.deterministic import (
    check_adrs_present,
    check_astro_build_time_data,
    check_astro_runtime_queries,
    check_db_writes_use_upserts,
    check_inputs_not_deleted,
    check_no_retired_trigger_patterns,
    check_response_shape_parity,
    check_respx_for_http_mocking,
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


def test_is_checker_self_source_helper_positive_and_negative_cases() -> None:
    from pathlib import Path as _Path

    from evaluator_cog.engine.deterministic._shared import _is_checker_self_source

    assert _is_checker_self_source(
        _Path("src/evaluator_cog/engine/deterministic/pipeline.py")
    )
    assert _is_checker_self_source(
        _Path("src/evaluator_cog/engine/deterministic/auth.py")
    )
    assert not _is_checker_self_source(_Path("src/evaluator_cog/engine/api_client.py"))
    assert not _is_checker_self_source(_Path("src/evaluator_cog/flows/conformance.py"))


# --- PIPE-002 / PIPE-005 ------------------------------------------------------


def test_pipe004_skips_checker_self_source_literals(tmp_path: Path) -> None:
    from evaluator_cog.engine.deterministic import check_shared_resource_concurrency

    _write(
        tmp_path,
        "src/pkg/engine/deterministic/checker.py",
        "from prefect import flow\n"
        "@flow\n"
        "def fake_checker_flow():\n"
        '    marker = "session.add("\n'
        "    return marker\n",
    )
    assert check_shared_resource_concurrency(tmp_path) == []


def test_pipe004_still_flags_real_flow_missing_concurrency_guard(
    tmp_path: Path,
) -> None:
    from evaluator_cog.engine.deterministic import check_shared_resource_concurrency

    _write(
        tmp_path,
        "src/pkg/flows/entry.py",
        "from prefect import flow\n"
        "@flow\n"
        "def write_flow(session):\n"
        "    session.add({'x': 1})\n"
        "    session.commit()\n",
    )
    findings = check_shared_resource_concurrency(tmp_path)
    assert any(f["rule_id"] == "PIPE-004" for f in findings)


def test_pipe002_flags_session_add_without_upsert_helpers(tmp_path: Path) -> None:
    _write(tmp_path, "src/db.py", "def save(session, row):\n    session.add(row)\n")
    f = check_db_writes_use_upserts(tmp_path)
    assert any(x["rule_id"] == "PIPE-002" for x in f)


def test_pipe002_skips_checker_self_source_literals(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/engine/deterministic/checker.py",
        'PATTERN = "session.add("\n',
    )
    assert check_db_writes_use_upserts(tmp_path) == []


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


def test_pipe005_skips_literal_only_files_delete_pattern(tmp_path: Path) -> None:
    """PIPE-005: both delete substrings only inside literals (fixture / self-scan)."""
    _write(
        tmp_path,
        "src/fixtures.py",
        'NOTE = ".files().delete( in text or files().delete( in text"\n',
    )
    assert check_inputs_not_deleted(tmp_path) == []


def test_pipe_005_ignores_trashed_inside_string_literal(tmp_path: Path) -> None:
    """PIPE-005: 'trashed' inside a string literal must not trigger the check.

    Regression: the `trashed` branch previously lacked the
    _is_inside_string_literal guard that the `delete(` branch already had,
    so the checker self-flagged when scanning its own source.
    """
    _write(
        tmp_path,
        "src/checker.py",
        "def check():\n"
        '    """This function looks for files().update({\\"trashed\\": true})."""\n'
        '    marker = "trashed"\n'
        "    return marker\n",
    )
    findings = check_inputs_not_deleted(tmp_path)
    assert all(f.get("rule_id") != "PIPE-005" for f in findings)


# --- TEST-007 -----------------------------------------------------------------


def test_test007_skips_http_tokens_only_in_string_literals(tmp_path: Path) -> None:
    """TEST-007: httpx/requests call text embedded only in literals is ignored."""
    _write(tmp_path, "pyproject.toml", "[project]\nname=x\ndependencies=[]\n# respx\n")
    _write(
        tmp_path,
        "tests/test_fixture.py",
        "EXAMPLE = \"httpx.get('https://example.com')\"\n",
    )
    findings = check_respx_for_http_mocking(tmp_path)
    assert not any(f.get("rule_id") == "TEST-007" for f in findings)


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


# --- TEST-011 -----------------------------------------------------------------


def test_test_011_accepts_assert_not_called(tmp_path: Path) -> None:
    """TEST-011: assert_not_called() is a valid assertion."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import MagicMock\n"
        "\n"
        "def test_a():\n"
        "    m = MagicMock()\n"
        "    m.do.assert_not_called()\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_accepts_call_count(tmp_path: Path) -> None:
    """TEST-011: .call_count comparisons count as assertions."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import MagicMock\n"
        "\n"
        "def test_a():\n"
        "    m = MagicMock()\n"
        "    m.do()\n"
        "    m.do()\n"
        "    assert m.do.call_count == 2\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_accepts_assert_awaited_once(tmp_path: Path) -> None:
    """TEST-011: AsyncMock's assert_awaited_once() is a valid assertion.

    Regression: only the sync verification tokens were recognized, so
    tests verifying AsyncMocks via the await-flavored APIs were falsely
    flagged.
    """
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import AsyncMock\n"
        "\n"
        "async def test_a():\n"
        "    m = AsyncMock()\n"
        "    await m.do()\n"
        "    m.do.assert_awaited_once()\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_accepts_assert_not_awaited(tmp_path: Path) -> None:
    """TEST-011: AsyncMock's assert_not_awaited() is a valid assertion."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import AsyncMock\n"
        "\n"
        "async def test_a():\n"
        "    m = AsyncMock()\n"
        "    m.do.assert_not_awaited()\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_accepts_await_count_and_await_args(tmp_path: Path) -> None:
    """TEST-011: reads of .await_count / .await_args / .await_args_list count."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import AsyncMock\n"
        "\n"
        "async def test_a():\n"
        "    m = AsyncMock()\n"
        "    await m.do(1)\n"
        "    assert m.do.await_count == 1\n"
        "    assert m.do.await_args.args == (1,)\n"
        "\n"
        "async def test_b():\n"
        "    m = AsyncMock()\n"
        "    await m.do(1)\n"
        "    await m.do(2)\n"
        "    assert len(m.do.await_args_list) == 2\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_still_flags_unverified_async_mock(tmp_path: Path) -> None:
    """TEST-011: an AsyncMock awaited but never verified is still flagged."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import AsyncMock\n"
        "\n"
        "async def test_a():\n"
        "    m = AsyncMock()\n"
        "    await m.do()\n"
    )
    findings = check_mock_assertions(tmp_path)
    assert len(findings) == 1


def test_test_011_still_flags_unverified_mock(tmp_path: Path) -> None:
    """TEST-011: mocks without any interrogation still get flagged."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import MagicMock\n"
        "\n"
        "def test_a():\n"
        "    m = MagicMock()\n"
        "    m.do()\n"
        "    assert True\n"
    )
    findings = check_mock_assertions(tmp_path)
    assert len(findings) == 1


def test_test_011_ignores_test_def_inside_string_literal(tmp_path: Path) -> None:
    """TEST-011: 'def test_X():' text inside a string literal must not be scanned.

    Regression: the checker previously regex-matched `def test_a():` text
    embedded in a string passed to write_text() and falsely flagged it as
    an unverified-mock test. AST-based test discovery fixes that. The body
    also references ``check_mock_assertions`` so mock names embedded only in
    fixture strings do not trip the capture regex for MagicMock/patch.
    """
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    # A real test that just writes fixture source text — the fixture text
    # contains `def test_a():` with MagicMock and no assertions, but
    # because it's inside a string, it's not a real test.
    (tests_dir / "test_x.py").write_text(
        "def test_real(tmp_path):\n"
        "    from evaluator_cog.engine.deterministic import check_mock_assertions\n"
        "    src = (\n"
        '        "from unittest.mock import MagicMock\\n"\n'
        '        "def test_a():\\n"\n'
        '        "    m = MagicMock()\\n"\n'
        '        "    m.do()\\n"\n'
        "    )\n"
        '    (tmp_path / "f.py").write_text(src)\n'
        '    assert (tmp_path / "f.py").exists()\n'
        "    assert callable(check_mock_assertions)\n"
    )
    findings = check_mock_assertions(tmp_path)
    assert findings == []


def test_test_011_handles_unparseable_test_file_gracefully(tmp_path: Path) -> None:
    """TEST-011: a syntactically broken test file should not crash the check."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_broken.py").write_text(
        "def test_a(:  # intentional syntax error\n"
    )
    # Must not raise.
    findings = check_mock_assertions(tmp_path)
    assert findings == []


def test_test_011_accepts_behavior_injection_with_downstream_assertion(
    tmp_path: Path,
) -> None:
    """TEST-011: patch(..., return_value=X) or side_effect=X is behavior injection.

    The mock is plumbing, not the thing under test. As long as the body has
    at least one assert statement, the test passes.
    """
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import patch\n"
        "\n"
        "def test_downstream():\n"
        "    with patch('module.fetch', return_value='stub-value'):\n"
        "        result = do_thing()\n"
        "    assert result == 'expected'\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_accepts_capture_list_with_membership_assertion(
    tmp_path: Path,
) -> None:
    """TEST-011: asserting that an element is in a captured list counts."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import patch\n"
        "\n"
        "def test_captures():\n"
        "    posted = []\n"
        "    def _fake(x): posted.append(x)\n"
        "    with patch('module.send', side_effect=_fake):\n"
        "        emit('hello')\n"
        "    assert 'hello' in posted\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_excludes_self_reference(tmp_path: Path) -> None:
    """TEST-011: tests that invoke check_mock_assertions by name are excluded.

    Those tests exercise the check by feeding it fixture source. Flagging
    them is circular — their mocks are fixture content, not real mocks.
    """
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import MagicMock\n"
        "\n"
        "def test_self_referential(tmp_path):\n"
        "    from evaluator_cog.engine.deterministic import check_mock_assertions\n"
        "    # Fixture content — not a real mock for this test\n"
        "    tests_dir = tmp_path / 'tests'\n"
        "    tests_dir.mkdir()\n"
        "    (tests_dir / 't.py').write_text('m = MagicMock()')\n"
        "    findings = check_mock_assertions(tmp_path)\n"
        "    assert len(findings) == 1\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_accepts_patch_with_replacement_class(tmp_path: Path) -> None:
    """TEST-011: patch(target, ReplacementClass) is behavior injection.

    The replacement class (or callable) supplants the target for the
    duration of the patch. The test then asserts on real code-under-test
    behavior. Equivalent to patch(..., return_value=...) semantically.
    """
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import patch\n"
        "\n"
        "class FakeApi:\n"
        "    def get(self, p):\n"
        "        return {'data': []}\n"
        "\n"
        "def test_replaces_client():\n"
        "    with patch('module.ApiClient', FakeApi):\n"
        "        result = do_something()\n"
        "    assert result is not None\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_accepts_pytest_raises_as_verification(tmp_path: Path) -> None:
    """TEST-011: pytest.raises(...) is an exception-shape assertion and
    counts as verification of the code under test."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "import pytest\n"
        "from unittest.mock import patch\n"
        "\n"
        "def test_raises():\n"
        "    with patch('x.y', side_effect=ValueError), pytest.raises(ValueError):\n"
        "        do_something()\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_accepts_pytest_raises_with_bare_mock(tmp_path: Path) -> None:
    """TEST-011: a bare MagicMock (no side_effect / no return_value) used
    alongside ``with pytest.raises(...):`` is still accepted — the raises
    context is the verification. The mock is plumbing to reach the failure
    path; interrogating it is not additionally required."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "import pytest\n"
        "from unittest.mock import MagicMock\n"
        "\n"
        "def test_bad_input_raises():\n"
        "    fake_client = MagicMock()\n"
        "    with pytest.raises(ValueError):\n"
        "        do_something(fake_client, bad_input=None)\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_accepts_unittest_assertraises_with_mock(tmp_path: Path) -> None:
    """TEST-011: unittest-style ``self.assertRaises(...)`` is also a valid
    exception-shape assertion."""
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "import unittest\n"
        "from unittest.mock import MagicMock\n"
        "\n"
        "class T(unittest.TestCase):\n"
        "    def test_raises(self):\n"
        "        fake = MagicMock()\n"
        "        with self.assertRaises(ValueError):\n"
        "            do_something(fake)\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_still_flags_bare_patch_without_verification(
    tmp_path: Path,
) -> None:
    """TEST-011: bare patch(target) with no replacement, no assertion,
    and no mock-API interrogation is still the genuine TEST-011 target.
    A bare patch returns a MagicMock but the test never asserts on it.
    """
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import patch\n"
        "\n"
        "def test_bare():\n"
        "    with patch('x.y'):\n"
        "        do_something()\n"
    )
    findings = check_mock_assertions(tmp_path)
    assert len(findings) == 1


# --- PIPE-006 -----------------------------------------------------------------


def test_pipe_006_accepts_repo_local_wrapper(tmp_path: Path) -> None:
    """PIPE-006: flows calling a repo-local logger wrapper are accepted."""
    from evaluator_cog.engine.deterministic import check_prefect_run_logger

    src = tmp_path / "src" / "mycog"
    src.mkdir(parents=True)
    (src / "helpers.py").write_text(
        "import logging\n"
        "from prefect import get_run_logger\n"
        "\n"
        "_log = logging.getLogger(__name__)\n"
        "\n"
        "def get_prefect_logger():\n"
        "    try:\n"
        "        return get_run_logger()\n"
        "    except Exception:\n"
        "        return _log\n"
    )
    (src / "flow_mod.py").write_text(
        "from prefect import flow\n"
        "from .helpers import get_prefect_logger\n"
        "\n"
        "@flow\n"
        "def my_flow():\n"
        "    logger = get_prefect_logger()\n"
        "    logger.info('hello')\n"
    )
    assert check_prefect_run_logger(tmp_path) == []


def test_pipe_006_flags_flow_with_no_logger(tmp_path: Path) -> None:
    """PIPE-006: flows with no logger acquisition still flagged."""
    from evaluator_cog.engine.deterministic import check_prefect_run_logger

    src = tmp_path / "src" / "mycog"
    src.mkdir(parents=True)
    (src / "flow_mod.py").write_text(
        "from prefect import flow\n\n@flow\ndef my_flow():\n    print('hello')\n"
    )
    assert len(check_prefect_run_logger(tmp_path)) == 1


def test_pipe_006_does_not_accept_logger_function_without_run_logger(
    tmp_path: Path,
) -> None:
    """PIPE-006: get_logger without get_run_logger is not a wrapper."""
    from evaluator_cog.engine.deterministic import check_prefect_run_logger

    src = tmp_path / "src" / "mycog"
    src.mkdir(parents=True)
    (src / "helpers.py").write_text(
        "import logging\n\ndef get_logger():\n    return logging.getLogger(__name__)\n"
    )
    (src / "flow_mod.py").write_text(
        "from prefect import flow\n"
        "from .helpers import get_logger\n"
        "\n"
        "@flow\n"
        "def my_flow():\n"
        "    logger = get_logger()\n"
        "    logger.info('hello')\n"
    )
    assert len(check_prefect_run_logger(tmp_path)) == 1


# --- CD-015 -------------------------------------------------------------------


def test_cd_015_accepts_from_prefect_import_serve(tmp_path: Path) -> None:
    """CD-015: `from prefect import serve` then `serve(...)` is accepted."""
    from evaluator_cog.engine.deterministic import check_prefect_serve_pattern

    src = tmp_path / "src" / "mycog"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        "from prefect import serve\n"
        "from .flow_mod import my_flow\n"
        "\n"
        "def main():\n"
        "    serve(my_flow.to_deployment(name='x'))\n"
    )
    warn_findings = [
        f for f in check_prefect_serve_pattern(tmp_path) if f.get("severity") == "WARN"
    ]
    assert warn_findings == []


def test_cd_015_still_warns_when_no_serve(tmp_path: Path) -> None:
    """CD-015: repo with no serve() call at all is still flagged."""
    from evaluator_cog.engine.deterministic import check_prefect_serve_pattern

    src = tmp_path / "src" / "mycog"
    src.mkdir(parents=True)
    (src / "main.py").write_text("def main(): pass\n")
    warn_findings = [
        f for f in check_prefect_serve_pattern(tmp_path) if f.get("severity") == "WARN"
    ]
    assert len(warn_findings) == 1


def test_cd_015_still_catches_work_pool(tmp_path: Path) -> None:
    """CD-015: flow.deploy() and work_pool_name still flagged as incompatible."""
    from evaluator_cog.engine.deterministic import check_prefect_serve_pattern

    src = tmp_path / "src" / "mycog"
    src.mkdir(parents=True)
    (src / "main.py").write_text(
        "def main():\n    flow.deploy(work_pool_name='default')\n"
    )
    errors = [
        f for f in check_prefect_serve_pattern(tmp_path) if f.get("severity") == "ERROR"
    ]
    assert len(errors) >= 1


def test_cd_015_skips_checker_self_source_literals(tmp_path: Path) -> None:
    from evaluator_cog.engine.deterministic import check_prefect_serve_pattern

    _write(
        tmp_path,
        "src/pkg/engine/deterministic/foo.py",
        "MARKERS = ['flow.deploy(', 'work_pool_name']\n",
    )
    findings = check_prefect_serve_pattern(tmp_path)
    assert not any(
        f["rule_id"] == "CD-015" and f["severity"] == "ERROR" for f in findings
    )


# --- API-008 ------------------------------------------------------------------


def test_api_008_accepts_intentionally_public_in_description(tmp_path: Path) -> None:
    """API-008: routes with 'intentionally public' in description= are exempt."""
    from evaluator_cog.engine.deterministic import check_unauthenticated_routes

    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n"
        "@app.get('/version', description='Reports version. Intentionally public.')\n"
        "async def version() -> dict:\n"
        "    return {'version': '1'}\n"
    )
    assert check_unauthenticated_routes(tmp_path, language="python") == []


def test_api_008_accepts_intentionally_public_in_docstring(tmp_path: Path) -> None:
    """API-008: routes with 'intentionally public' in docstring are exempt."""
    from evaluator_cog.engine.deterministic import check_unauthenticated_routes

    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n"
        "@app.get('/')\n"
        "async def root() -> dict:\n"
        "    '''Redirect. Intentionally public.'''\n"
        "    return {}\n"
    )
    assert check_unauthenticated_routes(tmp_path, language="python") == []


def test_api_008_still_flags_unmarked_public_route(tmp_path: Path) -> None:
    """API-008: routes with no auth and no intent marker still flagged."""
    from evaluator_cog.engine.deterministic import check_unauthenticated_routes

    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n"
        "@app.get('/secret')\n"
        "async def secret() -> dict:\n"
        "    return {'data': 'leak'}\n"
    )
    findings = check_unauthenticated_routes(tmp_path, language="python")
    assert len(findings) == 1


def test_api_008_accepts_depends(tmp_path: Path) -> None:
    """API-008: routes with Depends() unchanged."""
    from evaluator_cog.engine.deterministic import check_unauthenticated_routes

    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text(
        "from fastapi import FastAPI, Depends\n"
        "app = FastAPI()\n"
        "\n"
        "def verify(): pass\n"
        "\n"
        "@app.get('/protected')\n"
        "async def protected(user=Depends(verify)) -> dict:\n"
        "    return {}\n"
    )
    assert check_unauthenticated_routes(tmp_path, language="python") == []


# --- CD-010 --------------------------------------------------------------------


def test_cd_010_typescript_accepts_sentry_node(tmp_path: Path) -> None:
    """CD-010: TS service with @sentry/node + common-typescript-utils passes."""
    from evaluator_cog.engine.deterministic import check_three_layer_observability

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.ts").write_text(
        'import * as Sentry from "@sentry/node";\n'
        'import { createLogger } from "common-typescript-utils";\n'
        "Sentry.init({ dsn: process.env.SENTRY_DSN });\n"
        'const logger = createLogger("app");\n'
    )
    (tmp_path / ".env.example").write_text("SENTRY_DSN=\n")
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"@sentry/node": "^10.0.0", "common-typescript-utils": "^1.0.0"}}'
    )
    findings = check_three_layer_observability(
        tmp_path, cog_subtype=None, language="typescript"
    )
    layer_errors = [f for f in findings if "Layer" in f["finding"]]
    assert layer_errors == []


def test_cd_010_typescript_flags_missing_sentry(tmp_path: Path) -> None:
    """CD-010: TS service without @sentry/* is flagged at Layer 3."""
    from evaluator_cog.engine.deterministic import check_three_layer_observability

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.ts").write_text(
        'import { createLogger } from "common-typescript-utils";\n'
    )
    (tmp_path / ".env.example").write_text("")
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"common-typescript-utils": "^1.0.0"}}'
    )
    findings = check_three_layer_observability(
        tmp_path, cog_subtype=None, language="typescript"
    )
    assert any("Layer 3" in f["finding"] for f in findings)


def test_cd_010_python_unchanged(tmp_path: Path) -> None:
    """CD-010: Python path still works as before."""
    from evaluator_cog.engine.deterministic import check_three_layer_observability

    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text(
        "import sentry_sdk\n"
        "from mini_app_polis.logger import get_logger\n"
        "sentry_sdk.init()\n"
    )
    (tmp_path / ".env.example").write_text("SENTRY_DSN=\n")
    findings = check_three_layer_observability(
        tmp_path, cog_subtype=None, language="python"
    )
    assert findings == []


def test_cd010_layer1_passes_per_service_env_without_hostname(tmp_path: Path) -> None:
    """CD-010 Layer 1: per-service HEALTHCHECKS_URL_* + code ref without hostname."""
    from evaluator_cog.engine.deterministic import check_three_layer_observability

    (tmp_path / ".env.example").write_text(
        "HEALTHCHECKS_URL_WATCHER=\nSENTRY_DSN=\n",
        encoding="utf-8",
    )
    _write(
        tmp_path,
        "src/worker.py",
        "import os\nimport sentry_sdk\nfrom mini_app_polis.logger import get_logger\n"
        '_url = os.getenv("HEALTHCHECKS_URL_WATCHER")\n'
        "sentry_sdk.init()\n",
    )
    findings = check_three_layer_observability(
        tmp_path, cog_subtype="pipeline", language="python"
    )
    assert not any("Layer 1" in f["finding"] for f in findings)


def test_cd010_layer1_fails_pipeline_without_healthchecks_signals(
    tmp_path: Path,
) -> None:
    """CD-010 Layer 1: pipeline without env key or source ref still errors."""
    from evaluator_cog.engine.deterministic import check_three_layer_observability

    (tmp_path / ".env.example").write_text("SENTRY_DSN=\n", encoding="utf-8")
    _write(
        tmp_path,
        "src/main.py",
        "import sentry_sdk\nfrom mini_app_polis.logger import get_logger\nsentry_sdk.init()\n",
    )
    findings = check_three_layer_observability(
        tmp_path, cog_subtype="pipeline", language="python"
    )
    assert any("Layer 1" in f["finding"] for f in findings)


# --- XSTACK-002 (TS exclusions) ------------------------------------------------


def test_xstack_002_skips_src_test_directory(tmp_path: Path) -> None:
    """XSTACK-002: files under src/test/ are not flagged."""
    src = tmp_path / "src" / "test"
    src.mkdir(parents=True)
    (src / "mocks.ts").write_text(
        "export function mockHandler(c) {\n  return c.json({ ok: true });\n}\n"
    )
    assert check_response_shape_parity(tmp_path, language="typescript") == []


def test_xstack_002_skips_dot_test_files(tmp_path: Path) -> None:
    """XSTACK-002: *.test.ts files are not flagged."""
    src = tmp_path / "src" / "routes"
    src.mkdir(parents=True)
    (src / "songs.test.ts").write_text(
        'test("x", () => {\n  c.json({ data: [] });\n});\n'
    )
    assert check_response_shape_parity(tmp_path, language="typescript") == []


def test_xstack_002_still_flags_production_handler(tmp_path: Path) -> None:
    """XSTACK-002: real production handlers using raw c.json still flagged."""
    src = tmp_path / "src" / "routes"
    src.mkdir(parents=True)
    (src / "songs.ts").write_text(
        "export function handler(c) {\n  return c.json({ ok: true });\n}\n"
    )
    findings = check_response_shape_parity(tmp_path, language="typescript")
    assert len(findings) == 1


# --- TEST-013 -----------------------------------------------------------------


def test_test_013_skips_react_tsx(tmp_path: Path) -> None:
    """TEST-013: setTimeout in .tsx files is not flagged (UI delays)."""
    from evaluator_cog.engine.deterministic import check_hardcoded_time_values

    src = tmp_path / "src" / "pages"
    src.mkdir(parents=True)
    (src / "SongsPage.tsx").write_text(
        "export default function SongsPage() {\n"
        "  await new Promise((r) => setTimeout(r, 400));\n"
        "}\n"
    )
    assert check_hardcoded_time_values(tmp_path, language="typescript") == []


def test_test_013_skips_pages_directory(tmp_path: Path) -> None:
    """TEST-013: setTimeout in src/pages/ is skipped even for .ts files."""
    from evaluator_cog.engine.deterministic import check_hardcoded_time_values

    src = tmp_path / "src" / "pages"
    src.mkdir(parents=True)
    (src / "helpers.ts").write_text(
        "export const delay = () => setTimeout(() => {}, 300);\n"
    )
    assert check_hardcoded_time_values(tmp_path, language="typescript") == []


def test_test_013_still_flags_backend_ts(tmp_path: Path) -> None:
    """TEST-013: setTimeout in non-UI .ts files still flagged."""
    from evaluator_cog.engine.deterministic import check_hardcoded_time_values

    src = tmp_path / "src" / "services"
    src.mkdir(parents=True)
    (src / "retry.ts").write_text("export function retry() { setTimeout(fn, 5000); }\n")
    findings = check_hardcoded_time_values(tmp_path, language="typescript")
    assert len(findings) == 1


# --- TEST-010 regression tests for parametrised-route matching ----------------


def _write_route(src: Path, filename: str, body: str) -> None:
    src.mkdir(parents=True, exist_ok=True)
    (src / filename).write_text(body)


def _write_test(tests_dir: Path, filename: str, body: str) -> None:
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / filename).write_text(body)


def test_test_010_accepts_concrete_url_for_param_route(tmp_path: Path) -> None:
    """TEST-010: a route with {id} is covered by a test that uses a concrete value.

    Regression: the prior substring check would never match `{id}` against test
    code that writes `/v1/catalog/abc123`, so every parametrised route was
    reported as untested.
    """
    from evaluator_cog.engine.deterministic import check_route_contract_tests

    _write_route(
        tmp_path / "src",
        "routes.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/catalog/{id}')\n"
        "async def get_item(id: str): return {'id': id}\n",
    )
    _write_test(
        tmp_path / "tests",
        "test_routes.py",
        "async def test_get(client):\n"
        "    resp = await client.get('/v1/catalog/abc123')\n"
        "    assert resp.status_code == 200\n",
    )
    assert check_route_contract_tests(tmp_path) == []


def test_test_010_accepts_fstring_url_for_param_route(tmp_path: Path) -> None:
    """TEST-010: a route with {id} is covered by a test that uses an f-string URL."""
    from evaluator_cog.engine.deterministic import check_route_contract_tests

    _write_route(
        tmp_path / "src",
        "routes.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.patch('/wcs/notes/{note_id}')\n"
        "async def patch_note(note_id: str): return {}\n",
    )
    _write_test(
        tmp_path / "tests",
        "test_notes.py",
        "import uuid\n"
        "async def test_patch(client):\n"
        "    resp = await client.patch(\n"
        "        f'/v1/wcs/notes/{uuid.uuid4()}',\n"
        "        json={'visibility': 'public'},\n"
        "    )\n"
        "    assert resp.status_code == 404\n",
    )
    assert check_route_contract_tests(tmp_path) == []


def test_test_010_accepts_multi_segment_param_route(tmp_path: Path) -> None:
    """TEST-010: a route with {id} in the middle and a static suffix matches."""
    from evaluator_cog.engine.deterministic import check_route_contract_tests

    _write_route(
        tmp_path / "src",
        "routes.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/sets/{id}/tracks')\n"
        "async def list_tracks(id: str): return []\n",
    )
    _write_test(
        tmp_path / "tests",
        "test_sets.py",
        "async def test_tracks(client):\n"
        "    resp = await client.get('/v1/sets/set-42/tracks')\n"
        "    assert resp.status_code == 200\n",
    )
    assert check_route_contract_tests(tmp_path) == []


def test_test_010_accepts_fstring_with_dict_subscript_param(tmp_path: Path) -> None:
    """TEST-010: f-string expressions with nested quotes must still match.

    Regression: the earlier version of this fix used a wildcard that
    excluded quote characters, which broke on the common Python idiom
    `f"/v1/wcs/admin/notes/{note['id']}/visibility"` — the single quotes
    around the dict key appear inside the path parameter and caused the
    match to fail. The wildcard now excludes only slash and whitespace.
    """
    from evaluator_cog.engine.deterministic import check_route_contract_tests

    _write_route(
        tmp_path / "src",
        "routes.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.patch('/wcs/admin/notes/{note_id}/visibility')\n"
        "async def set_default(note_id: str): return {}\n",
    )
    _write_test(
        tmp_path / "tests",
        "test_admin.py",
        "async def test_patch_default(client):\n"
        "    note = {'id': 'abc'}\n"
        "    resp = await client.patch(\n"
        '        f"/v1/wcs/admin/notes/{note[' + "'id'" + ']}/visibility",\n'
        "        json={'is_default_visible': True},\n"
        "    )\n"
        "    assert resp.status_code == 200\n",
    )
    assert check_route_contract_tests(tmp_path) == []


def test_test_010_still_flags_truly_untested_param_route(tmp_path: Path) -> None:
    """TEST-010: the fix must not regress the flagging of genuinely untested routes."""
    from evaluator_cog.engine.deterministic import check_route_contract_tests

    _write_route(
        tmp_path / "src",
        "routes.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/flags/{name}')\n"
        "async def get_flag(name: str): return {'name': name}\n",
    )
    _write_test(
        tmp_path / "tests",
        "test_other.py",
        "async def test_unrelated(client):\n"
        "    resp = await client.get('/v1/health')\n"
        "    assert resp.status_code == 200\n",
    )
    findings = check_route_contract_tests(tmp_path)
    assert len(findings) == 1
    assert "/flags/{name}" in findings[0]["finding"]


def test_test_010_regex_does_not_cross_path_segments(tmp_path: Path) -> None:
    """TEST-010: the {id} wildcard must not match across `/` boundaries.

    Otherwise a route like `/sets/{id}/tracks` could be falsely satisfied by
    a test URL of `/v1/sets/abc/extra/tracks` which is a different route.
    """
    from evaluator_cog.engine.deterministic import check_route_contract_tests

    _write_route(
        tmp_path / "src",
        "routes.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/sets/{id}/tracks')\n"
        "async def list_tracks(id: str): return []\n",
    )
    _write_test(
        tmp_path / "tests",
        "test_other.py",
        "async def test_unrelated(client):\n"
        "    resp = await client.get('/v1/sets/abc/extra/tracks/stuff')\n"
        "    assert resp.status_code == 200\n",
    )
    # The `/` inside the test URL between `abc` and `extra` means the real
    # route /sets/{id}/tracks is not exercised. Should flag.
    findings = check_route_contract_tests(tmp_path)
    assert len(findings) == 1


# --- TEST-011 regression test: client.patch(...) must not trigger ------------


def test_test_011_does_not_flag_fastapi_client_patch_method(tmp_path: Path) -> None:
    """TEST-011: the checker must not confuse `client.patch(...)` with `unittest.mock.patch`.

    Regression: the prior mock-creation regex word-matched `patch` globally,
    which fired on the FastAPI test client's HTTP method `client.patch(...)`.
    Tests that use the HTTP PATCH verb of a test client have no mocks in
    them and must not be reported.
    """
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_notes.py").write_text(
        "import uuid\n"
        "\n"
        "async def test_patch_note_not_found(client) -> None:\n"
        "    resp = await client.patch(\n"
        "        f'/v1/wcs/notes/{uuid.uuid4()}',\n"
        "        json={'visibility': 'public'},\n"
        "    )\n"
        "    assert resp.status_code == 404\n"
        "    assert resp.json()['error']['code'] == 'note_not_found'\n"
    )
    assert check_mock_assertions(tmp_path) == []


def test_test_011_still_flags_bare_unittest_patch_without_assertion(
    tmp_path: Path,
) -> None:
    """TEST-011: bare `patch(...)` usage (no return_value, no verification) still flagged.

    Confirms the `.patch` lookbehind didn't accidentally disable the whole
    `patch` code path — only the method-call form.
    """
    from evaluator_cog.engine.deterministic import check_mock_assertions

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text(
        "from unittest.mock import patch\n"
        "\n"
        "def test_a():\n"
        "    with patch('module.thing'):\n"
        "        do_something()\n"
    )
    findings = check_mock_assertions(tmp_path)
    assert len(findings) == 1
