"""Test-suite rule checks (TestClient, fixtures, mock assertions, respx)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
    _is_inside_string_literal,
)


def check_testclient_for_v1_routes(repo_path: Path) -> list[Finding]:
    """TEST-008: /v1/ route tests use TestClient or AsyncClient."""
    CHECK_ID = "TEST-008"
    import re

    findings: list[Finding] = []
    tests_dir = repo_path / "tests"
    if not tests_dir.is_dir():
        return findings

    for test_file in tests_dir.rglob("test_*.py"):
        try:
            text = test_file.read_text()
        except Exception:
            continue
        # Skip if file uses TestClient/AsyncClient
        if "TestClient" in text or "AsyncClient" in text:
            continue
        # Look for test functions referencing /v1/
        for m in re.finditer(r"def (test_\w+)\([^)]*\):([\s\S]*?)(?=\ndef |\Z)", text):
            fn_name, body = m.group(1), m.group(2)
            if "/v1/" in body:
                rel = test_file.relative_to(repo_path)
                findings.append(
                    _finding(
                        "TEST-008",
                        "WARN",
                        "test_coverage",
                        f"{rel}::{fn_name}: references /v1/ without TestClient/AsyncClient.",
                        "Use fastapi.testclient.TestClient or httpx.AsyncClient for route tests.",
                    )
                )
    return findings


def check_db_test_fixtures(repo_path: Path) -> list[Finding]:
    """TEST-009: conftest has DB test fixtures."""
    CHECK_ID = "TEST-009"
    findings: list[Finding] = []
    src = repo_path / "src"
    tests = repo_path / "tests"
    if not src.is_dir() or not tests.is_dir():
        return findings

    # Is this a SQLAlchemy repo?
    has_sqlalchemy = False
    for py_file in src.rglob("*.py"):
        try:
            if "sqlalchemy" in py_file.read_text().lower():
                has_sqlalchemy = True
                break
        except Exception:
            continue
    if not has_sqlalchemy:
        return findings

    conftest_files = list(tests.rglob("conftest.py"))
    if not conftest_files:
        findings.append(
            _finding(
                "TEST-009",
                "ERROR",
                "test_coverage",
                "SQLAlchemy repo has no conftest.py with DB test fixtures.",
                "Add a conftest.py with DATABASE_URL override, in-memory engine, or "
                "transaction rollback fixture.",
            )
        )
        return findings

    combined = "\n".join(f.read_text() for f in conftest_files if f.exists())
    has_fixture_pattern = (
        "DATABASE_URL" in combined
        or "sqlite:///:memory:" in combined
        or ("rollback" in combined.lower() and "fixture" in combined.lower())
    )
    if not has_fixture_pattern:
        findings.append(
            _finding(
                "TEST-009",
                "ERROR",
                "test_coverage",
                "conftest.py has no DB test fixture pattern (DATABASE_URL override, in-memory SQLite, or rollback fixture).",
                "Add one of: DATABASE_URL override, in-memory SQLite engine, or rollback fixture.",
            )
        )
    return findings


def check_route_contract_tests(repo_path: Path) -> list[Finding]:
    """TEST-010: Each FastAPI route has a contract test."""
    CHECK_ID = "TEST-010"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    tests = repo_path / "tests"
    if not src.is_dir() or not tests.is_dir():
        return findings

    route_paths: set[str] = set()
    route_attrs = {"get", "post", "put", "delete", "patch"}
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in route_attrs:
                    continue
                if (
                    dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)
                ):
                    route_paths.add(dec.args[0].value)

    if not route_paths:
        return findings

    test_text = ""
    for test_file in tests.rglob("test_*.py"):
        try:
            test_text += "\n" + test_file.read_text()
        except Exception:
            continue

    # Build a regex for each route. `/catalog/{id}` becomes
    # `/catalog/[^\s/]+`, so concrete URLs in tests like
    # `client.get(f"/v1/catalog/{item_id}")` or
    # `client.get("/v1/catalog/abc123")` match. Without this, path
    # parameters like `{id}` / `{name}` never appear literally in test
    # code and every parametrised route was reported as untested.
    #
    # The wildcard excludes slash (so a multi-segment path like
    # `/sets/{id}/tracks` can't be falsely satisfied by a test URL
    # `/v1/sets/abc/extra/tracks` — different route) and whitespace
    # (so a match can't bleed across lines in the concatenated test
    # text). Quotes are NOT excluded: Python f-strings often nest
    # quote characters inside the path-parameter expression itself,
    # e.g. `f"/v1/wcs/admin/notes/{note['id']}/visibility"`, and
    # excluding quotes would cause the regex to fail on that
    # extremely common pattern.
    #
    # Routes without any `{...}` placeholder fall through to a plain
    # substring match via `re.escape`.
    _param_re = re.compile(r"\{[^}]+\}")
    _param_sub = r"[^\s/]+"

    def _route_to_regex(route: str) -> re.Pattern[str]:
        parts = _param_re.split(route)
        pattern = _param_sub.join(re.escape(p) for p in parts)
        return re.compile(pattern)

    untested = [r for r in route_paths if not _route_to_regex(r).search(test_text)]
    if untested:
        sample = ", ".join(sorted(untested)[:5])
        suffix = " (and others)" if len(untested) > 5 else ""
        findings.append(
            _finding(
                "TEST-010",
                "ERROR",
                "test_coverage",
                f"{len(untested)} route(s) have no contract test referencing them: {sample}{suffix}.",
                "Add tests that exercise each /v1/ route and assert the response shape.",
            )
        )
    return findings


def check_mock_assertions(repo_path: Path) -> list[Finding]:
    """TEST-011: Mocks have corresponding assertions.

    Accepts multiple valid verification patterns:

    1. Direct mock-API verification — ``assert_called`` / ``assert_any_call`` /
       ``assert_not_called`` / ``assert_called_with`` / ``assert_called_once`` /
       ``assert_called_once_with``, plus reads of ``.call_count`` / ``.call_args`` /
       ``.call_args_list`` / ``.called``. The AsyncMock await-flavored
       equivalents count too — ``assert_awaited`` / ``assert_any_await`` /
       ``assert_not_awaited`` / ``assert_awaited_with`` / ``assert_awaited_once``
       / ``assert_awaited_once_with``, plus reads of ``.await_count`` /
       ``.await_args`` / ``.await_args_list``.

    2. Capture-list verification — a local ``name: list = []`` (or ``name = []``)
       bound inside the test body, then referenced in any ``assert`` statement.
       This covers the common pytest idiom where the mock hands off to a closure
       that appends to the list, and the test asserts on the list afterward.

    3. Behavior-injection verification — ``patch(..., return_value=X)`` or
       ``patch(..., side_effect=X)`` configures the mock as plumbing for the
       real thing under test. The test verifies the real thing's output with
       any ``assert`` statement, not the mock itself.

    4. Exception-shape verification — ``with pytest.raises(...):`` /
       ``pytest.warns(...)`` / unittest's ``assertRaises`` / ``assertWarns``
       contexts. The raise itself is the verification. Mocks alongside are
       typically plumbing to reach the failure path, and do not additionally
       need mock-API verification.

    A test that creates a mock but has zero assertions in its body is still
    flagged.

    Excludes tests that call ``check_mock_assertions`` in their body — those
    tests exercise this check by feeding it fixture source, so flagging them
    is circular.

    Uses AST parsing so that ``def test_X():`` text appearing inside string
    literals is not mistaken for a real test function. Function bodies are
    extracted by line slicing rather than ast.get_source_segment — the latter
    is ~5x slower because it re-computes source positions per call.
    """
    findings: list[Finding] = []
    tests = repo_path / "tests"
    if not tests.is_dir():
        return findings

    _assert_prefix = chr(97) + "ssert_"
    _mock_verify_patterns = (
        rf"\.{_assert_prefix}called\b",
        rf"\.{_assert_prefix}any_call\b",
        rf"\.{_assert_prefix}not_called\b",
        rf"\.{_assert_prefix}called_with\b",
        rf"\.{_assert_prefix}called_once\b",
        rf"\.{_assert_prefix}called_once_with\b",
        # AsyncMock await-flavored verification APIs.
        rf"\.{_assert_prefix}awaited\b",
        rf"\.{_assert_prefix}any_await\b",
        rf"\.{_assert_prefix}not_awaited\b",
        rf"\.{_assert_prefix}awaited_with\b",
        rf"\.{_assert_prefix}awaited_once\b",
        rf"\.{_assert_prefix}awaited_once_with\b",
        r"\.call_count\b",
        r"\.call_args\b",
        r"\.call_args_list\b",
        r"\.called\b",
        r"\.await_count\b",
        r"\.await_args\b",
        r"\.await_args_list\b",
    )
    _mock_verify_re = re.compile("|".join(_mock_verify_patterns))
    # Matches mock-creation tokens. The `patch` alternative uses a negative
    # lookbehind to exclude method-call forms like `client.patch(...)` — the
    # FastAPI test client exposes HTTP verbs as methods, and prior to this
    # guard the bare-word match was firing on `client.patch("/v1/...")` as
    # if it were `unittest.mock.patch(...)`. Legitimate `patch` usage is
    # either `patch(...)` on its own or `with patch(...)`, neither of
    # which is preceded by a `.`.
    _mock_create_re = re.compile(
        r"\b(?:MagicMock|AsyncMock|mock_\w+)\b|(?<!\.)\bpatch\b"
    )

    # A local list binding: `name = []`, `name: Type = []`, or `name = list()`.
    _empty_list_bind_re = re.compile(
        r"^\s*(\w+)\s*(?::\s*[^=]+)?=\s*(?:\[\s*\]|list\(\s*\))\s*$",
        re.MULTILINE,
    )

    # `patch(..., return_value=X)` or `patch(..., side_effect=X)` — behavior
    # injection. These mocks are plumbing; we don't require verifying them.
    _patch_behavior_injection_re = re.compile(
        r"\bpatch[.\w]*\([^)]*\b(return_value|side_effect)\s*=",
        re.DOTALL,
    )

    # `patch(target, replacement)` form — 2+ positional args. The second arg
    # is a fake class, instance, or callable that replaces the target. The
    # test then asserts on real behavior after injecting the fake.
    # Matches `patch("foo.bar", FakeClass)` and `patch.object(obj, "method",
    # fake_fn)` but not bare `patch("foo.bar")` which returns a MagicMock.
    # We require the second argument to not begin with a `kw=` pattern at the
    # top level — `ARG, ARG` vs `ARG, kw=ARG`. Simple heuristic: any `,` at
    # top-level depth followed by something that isn't `\w+\s*=`.
    _patch_replacement_re = re.compile(
        r"\bpatch[.\w]*\(\s*[^,)]+,\s*(?!\w+\s*=)[^,)]+[,)]",
        re.DOTALL,
    )

    # Catch MagicMock(..., side_effect=...) / MagicMock(..., return_value=...)
    # which is the same behavior-injection idiom outside of `patch()`.
    _mock_ctor_behavior_injection_re = re.compile(
        r"\b(?:MagicMock|AsyncMock)\([^)]*\b(?:return_value|side_effect)\s*=",
        re.DOTALL,
    )

    # Any explicit `assert ...` statement (not assertRaises / not assert_xxx),
    # or a `pytest.raises(...)` / `pytest.warns(...)` call — both of which
    # are exception-shape assertions on the code under test.
    _has_assert_re = re.compile(
        r"^\s*assert\b|\bpytest\.raises\s*\(|\bpytest\.warns\s*\(",
        re.MULTILINE,
    )

    # Exception-shape verification patterns. A test that wraps the call
    # under test in ``with pytest.raises(...):`` (or the unittest
    # ``assertRaises`` / ``assertWarns`` equivalents) is verifying the
    # behavior of the code under test — the raise itself IS the assertion.
    # Mocks used alongside such a context are typically plumbing to reach
    # the failure path, and do not additionally need mock-API verification.
    _exception_context_re = re.compile(
        r"\bpytest\.raises\s*\(|\bpytest\.warns\s*\("
        r"|\bassertRaises\s*\(|\bassertRaisesRegex\s*\("
        r"|\bassertWarns\s*\(|\bassertWarnsRegex\s*\("
    )

    # Self-reference: test body invokes the function under test.
    _self_reference_re = re.compile(r"\bcheck_mock_assertions\b")

    def _has_capture_list_assertion(body_src: str) -> bool:
        names = {m.group(1) for m in _empty_list_bind_re.finditer(body_src)}
        if not names:
            return False
        for name in names:
            # Any `assert ...` statement that references the captured
            # list name counts as verification, whether via len(), indexing,
            # membership, comparison, or iteration in a comprehension.
            pat = rf"\bassert\b[^\n]*\b{re.escape(name)}\b"
            if re.search(pat, body_src):
                return True
        return False

    def _is_behavior_injection_with_assertion(body_src: str) -> bool:
        """Patches with return_value/side_effect/replacement are plumbing;
        any assert counts as verification of the real code under test.

        Forms recognised as behavior injection:
          - ``patch(target, return_value=X)`` / ``patch(target, side_effect=X)``
          - ``patch(target, FakeClass)`` / ``patch.object(obj, "m", fake_fn)``
          - ``MagicMock(return_value=X)`` / ``AsyncMock(side_effect=X)``
        """
        injects = (
            _patch_behavior_injection_re.search(body_src)
            or _patch_replacement_re.search(body_src)
            or _mock_ctor_behavior_injection_re.search(body_src)
        )
        if not injects:
            return False
        return bool(_has_assert_re.search(body_src))

    for test_file in tests.rglob("test_*.py"):
        try:
            text = test_file.read_text()
        except Exception:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()
        rel = test_file.relative_to(repo_path)

        test_fns: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    test_fns.append(node)
            elif isinstance(node, ast.ClassDef):
                for inner in node.body:
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                        inner.name.startswith("test_")
                    ):
                        test_fns.append(inner)

        for fn in test_fns:
            start = (fn.lineno or 1) - 1
            for dec in fn.decorator_list:
                ln = getattr(dec, "lineno", None)
                if isinstance(ln, int) and ln > 0:
                    start = min(start, ln - 1)
            end = fn.end_lineno or len(lines)
            body_src = "\n".join(lines[start:end])
            if not body_src:
                continue
            if not _mock_create_re.search(body_src):
                continue
            if _self_reference_re.search(body_src):
                continue
            if _mock_verify_re.search(body_src):
                continue
            if _has_capture_list_assertion(body_src):
                continue
            if _is_behavior_injection_with_assertion(body_src):
                continue
            if _exception_context_re.search(body_src):
                continue
            findings.append(
                _finding(
                    "TEST-011",
                    "ERROR",
                    "test_coverage",
                    f"{rel}::{fn.name}: creates mocks but has no verification "
                    f"(mock-API helpers like called / call_args, capture-list "
                    f"assertion, or behavior-injection with at least one "
                    f"assert statement).",
                    "Verify the mock was exercised using unittest.mock's standard "
                    "verification APIs, assert against a capture list populated "
                    "by the mocked callable, or include at least one assert on "
                    "the code under test when the mock is used only for "
                    "return_value / side_effect behavior injection.",
                )
            )
    return findings


def check_test_gap_critical_paths(repo_path: Path) -> list[Finding]:
    """TEST-GAP-001: Track presence of TEST-001..004 critical-path tests."""
    CHECK_ID = "TEST-GAP-001"
    findings: list[Finding] = []
    tests = repo_path / "tests"
    if not tests.is_dir():
        return findings

    test_text = ""
    for test_file in tests.rglob("test_*.py"):
        try:
            test_text += "\n" + test_file.read_text()
        except Exception:
            continue

    # Heuristic markers for each critical-path category
    critical_markers = {
        "TEST-001 (normalization)": ("normalize", "normalise", "normalization"),
        "TEST-002 (deduplication)": ("dedup", "deduplication"),
        "TEST-003 (persistence)": ("persist", "upsert", "session.commit"),
        "TEST-004 (archival)": ("archive", "archival", "move"),
    }
    missing = []
    for label, markers in critical_markers.items():
        if not any(m in test_text.lower() for m in markers):
            missing.append(label)

    if missing:
        findings.append(
            _finding(
                "TEST-GAP-001",
                "INFO",
                "test_coverage",
                f"Missing critical-path tests: {', '.join(missing)}.",
                "Add tests for the missing categories so each pipeline stage has coverage.",
            )
        )
    return findings


def check_respx_for_http_mocking(repo_path: Path) -> list[Finding]:
    """TEST-007: respx/httpx for HTTP mocking — no real external calls."""
    CHECK_ID = "TEST-007"
    findings = []
    pyproject = repo_path / "pyproject.toml"
    py_text = pyproject.read_text().lower() if pyproject.exists() else ""
    if "respx" not in py_text:
        findings.append(
            _finding(
                "TEST-007",
                "ERROR",
                "testing_coverage",
                "respx is absent from development dependencies.",
                "Add respx to dev dependencies for HTTP mocking in tests.",
            )
        )

    tests_dir = repo_path / "tests"
    if not tests_dir.is_dir():
        return findings
    for test_file in tests_dir.rglob("test_*.py"):
        try:
            text = test_file.read_text()
        except OSError:
            continue

        http_tokens = (
            "httpx.get(",
            "httpx.post(",
            "requests.get(",
            "requests.post(",
        )
        real_http_tokens = [
            tok
            for tok in http_tokens
            if tok in text and not _is_inside_string_literal(text, tok)
        ]
        if real_http_tokens and "respx.mock" not in text:
            findings.append(
                _finding(
                    "TEST-007",
                    "ERROR",
                    "testing_coverage",
                    f"Raw HTTP calls found without respx.mock in {test_file.relative_to(repo_path)}.",
                    "Wrap HTTP interactions in respx.mock() and avoid real external network calls.",
                )
            )
            break
    return findings


def check_pytest_config(repo_path: Path) -> list[Finding]:
    """TEST-005: pytest configuration present in pyproject.toml.

    The wider test-structure checks that previously lived here (TEST-003
    failure-path detection) were retired in favor of LLM routing per
    the ecosystem-standards v3.8.0 classification. TEST-005 remains a
    deterministic structural check and is preserved here with a proper
    CHECK_ID.
    """
    CHECK_ID = "TEST-005"
    findings: list[Finding] = []
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        return findings
    if "[tool.pytest.ini_options]" not in pyproject.read_text():
        findings.append(
            _finding(
                "TEST-005",
                "WARN",
                "testing_coverage",
                "[tool.pytest.ini_options] absent from pyproject.toml.",
                "Add pytest configuration to pyproject.toml.",
            )
        )
    return findings
