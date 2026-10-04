"""Test-suite rule checks (TestClient, fixtures, mock assertions, respx)."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from urllib.parse import urlparse

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
    _is_checker_self_source,
    _is_inside_string_literal,
)


def _client_fixture_names(tests_dir: Path) -> set[str]:
    """Fixture names that hand a test a real HTTP client.

    A route test that takes a ``client`` fixture is going through
    ``AsyncClient`` just as surely as one that constructs it inline — the
    construction has simply been moved to ``conftest.py``, which is where
    a shared fixture belongs. Reading only the test file makes the better
    arrangement look like the violation.
    """
    import ast

    names: set[str] = set()
    for conftest in tests_dir.rglob("conftest.py"):
        try:
            tree = ast.parse(conftest.read_text())
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not any("fixture" in ast.unparse(d) for d in node.decorator_list):
                continue
            source = ast.unparse(node)
            if "TestClient" in source or "AsyncClient" in source:
                names.add(node.name)
    return names


def check_testclient_for_v1_routes(repo_path: Path) -> list[Finding]:
    """TEST-008: /v1/ route tests use TestClient or AsyncClient."""
    CHECK_ID = "TEST-008"
    import re

    findings: list[Finding] = []
    tests_dir = repo_path / "tests"
    if not tests_dir.is_dir():
        return findings

    client_fixtures = _client_fixture_names(tests_dir)

    for test_file in tests_dir.rglob("test_*.py"):
        try:
            text = test_file.read_text()
        except Exception:
            continue
        # Skip if file uses TestClient/AsyncClient
        if "TestClient" in text or "AsyncClient" in text:
            continue
        # Look for test functions referencing /v1/
        for m in re.finditer(
            r"def (test_\w+)\(([^)]*)\):([\s\S]*?)(?=\ndef |\Z)", text
        ):
            fn_name, params, body = m.group(1), m.group(2), m.group(3)
            if "/v1/" not in body:
                continue
            # A parameter naming a client fixture is the client.
            taken = {
                p.split(":")[0].split("=")[0].strip()
                for p in params.split(",")
                if p.strip()
            }
            if taken & client_fixtures:
                continue
            rel = test_file.relative_to(repo_path)
            findings.append(
                _finding(
                    "TEST-008",
                    "WARN",
                    "testing_coverage",
                    f"{rel}::{fn_name}: references /v1/ without TestClient/AsyncClient.",
                    "Use fastapi.testclient.TestClient or httpx.AsyncClient for route tests.",
                )
            )
    return findings


_LOCAL_DB_HOSTS = frozenset({"localhost", "127.0.0.1"})
_DB_URL_RE = re.compile(
    r"(?:postgres(?:ql)?|mysql|mariadb)(?:\+\w+)?://[^\s'\"]+", re.IGNORECASE
)


def _imports_sqlalchemy(src: Path) -> bool:
    """True when production code imports SQLAlchemy — the repo has a database.

    An import, not the word: the evaluator's own checker source names
    sqlalchemy in its pattern lists and is excluded for the same reason.
    """
    for py in src.rglob("*.py"):
        if _is_checker_self_source(py):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                a.name.split(".")[0] == "sqlalchemy" for a in node.names
            ):
                return True
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] == "sqlalchemy"
            ):
                return True
    return False


def _is_remote_host(host: str) -> bool:
    """A qualified name or an address that is not this machine.

    Single-label names are not reported: ``h`` in a URL-parsing fixture or
    ``postgres`` as a compose service name cannot reach a production host.
    """
    return bool(host) and "." in host and not host.startswith("127.")


def _is_local_test_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in _LOCAL_DB_HOSTS and parsed.path.rstrip("/").endswith(
        "_test"
    )


def _ci_test_job_db_urls(repo_path: Path) -> list[str]:
    """Values of every *DATABASE_URL variable on the CI `test` job and its steps."""
    import yaml

    ci = repo_path / ".github" / "workflows" / "ci.yml"
    try:
        workflow = yaml.safe_load(ci.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    job = (
        (workflow.get("jobs") or {}).get("test") if isinstance(workflow, dict) else None
    )
    if not isinstance(job, dict):
        return []
    envs = [job.get("env") or {}]
    envs += [
        (s or {}).get("env") or {}
        for s in job.get("steps") or []
        if isinstance(s, dict)
    ]
    return [
        str(value)
        for env in envs
        if isinstance(env, dict)
        for key, value in env.items()
        if str(key).upper().endswith("DATABASE_URL")
    ]


def _guards_test_database(node: ast.AST) -> bool:
    """True when `node` raises, names a local host and tests for `_test`."""
    strings = {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    raises = any(isinstance(n, ast.Raise) for n in ast.walk(node))
    return raises and bool(strings & _LOCAL_DB_HOSTS) and "_test" in strings


def _app_packages(repo_path: Path) -> set[str]:
    src = repo_path / "src"
    return {
        p.name for p in src.iterdir() if p.is_dir() and (p / "__init__.py").is_file()
    }


def _imports_package(stmt: ast.stmt, packages: set[str]) -> bool:
    if isinstance(stmt, ast.Import):
        return any(a.name.split(".")[0] in packages for a in stmt.names)
    if isinstance(stmt, ast.ImportFrom) and stmt.module and not stmt.level:
        return stmt.module.split(".")[0] in packages
    return False


def _conftest_guards_first(tree: ast.Module, packages: set[str]) -> bool:
    """True when a module-level guard runs before the app package is imported.

    The guard is a function that raises on a non-local or non-`_test` URL,
    called at module level or from `pytest_configure`, or the same check
    written inline as a module-level statement. It has to come before the
    first module-level import of the package under src/: importing the app
    builds its settings and engine, and that is the connection the guard
    exists to stop.
    """
    guards = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _guards_test_database(n)
    }
    first_import = next(
        (i for i, s in enumerate(tree.body) if _imports_package(s, packages)),
        len(tree.body),
    )

    def calls_guard(node: ast.AST) -> bool:
        return any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in guards
            for n in ast.walk(node)
        )

    for i, stmt in enumerate(tree.body):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name == "pytest_configure" and (
                calls_guard(stmt) or _guards_test_database(stmt)
            ):
                return True
            continue
        if i < first_import and (calls_guard(stmt) or _guards_test_database(stmt)):
            return True
    return False


def check_test_database_guard(repo_path: Path) -> list[Finding]:
    """TEST-009: tests can only reach a local test database.

    Three conditions, each its own finding, in the order the rule gives
    them. (1) CI hands the test job a local ``*_test`` database. (2) A
    conftest refuses any other URL before the app is imported — the
    fixtures empty the database between tests, so pointed elsewhere a
    correct suite deletes real data. (3) No database URL under tests/
    names a remote host.

    In-memory SQLite no longer satisfies (1): the engine is TEST-015's, and
    this rule is only the guard. A repo whose src/ never mentions
    SQLAlchemy has no database and passes.
    """
    CHECK_ID = "TEST-009"
    findings: list[Finding] = []
    src = repo_path / "src"
    tests = repo_path / "tests"
    if not src.is_dir():
        return findings
    if not _imports_sqlalchemy(src):
        return findings

    urls = _ci_test_job_db_urls(repo_path)
    if not any(_is_local_test_url(u) for u in urls):
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                "testing_coverage",
                "The CI test job sets no *DATABASE_URL pointing at a local database "
                "named *_test"
                + (f" (found: {', '.join(sorted(set(urls)))})." if urls else "."),
                "Give the `test` job a Postgres service and set e.g. "
                "TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/<name>_test.",
            )
        )

    packages = _app_packages(repo_path)
    guarded = False
    for conftest in sorted(tests.rglob("conftest.py")) if tests.is_dir() else []:
        try:
            tree = ast.parse(conftest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        if _conftest_guards_first(tree, packages):
            guarded = True
            break
    if not guarded:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                "testing_coverage",
                "No conftest.py refuses a database URL that is not local and named "
                "*_test before the application is imported.",
                "In tests/conftest.py, above the app import: parse the test database "
                "URL and raise unless its host is localhost/127.0.0.1 and its "
                "database name ends in _test.",
            )
        )

    remote: set[str] = set()
    for py in sorted(tests.rglob("*.py")) if tests.is_dir() else []:
        text = py.read_text(encoding="utf-8", errors="ignore")
        for url in _DB_URL_RE.findall(text):
            host = urlparse(url).hostname or ""
            if _is_remote_host(host) and not re.search(r"[{$]", url):
                remote.add(f"{py.relative_to(repo_path)} ({host})")
    if remote:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                "testing_coverage",
                f"Tests name a database on a non-local host: {', '.join(sorted(remote))}.",
                "Point tests only at the local *_test database the CI job provides.",
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
       ``.await_args`` / ``.await_args_list`` / ``.method_calls`` /
       ``.mock_calls``.

    2. Capture-list verification — a local ``name: list = []`` (or ``name = []``)
       bound inside the test body, then referenced in any ``assert`` statement.
       This covers the common pytest idiom where the mock hands off to a closure
       that appends to the list, and the test asserts on the list afterward.

    3. Behavior-injection verification — ``patch(..., return_value=X)``,
       ``patch(..., side_effect=X)``, the same keywords on a ``MagicMock``
       constructor, or an assignment to ``.return_value`` / ``.side_effect``
       on a mock built bare. Any of these configures the mock as plumbing
       for the real thing under test. The test verifies the real thing's output with
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
        # The whole call sequence, in order — the strictest verification a
        # mock offers, and the one this check used to miss.
        r"\.method_calls\b",
        r"\.mock_calls\b",
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
    # `patch` counts only where it is actually *used* — `patch(`,
    # `patch.object(`, `patch .` — never as a bare word. Matching the bare
    # word read the English noun as mock creation: the docstring "an
    # un-taken patch release is not staleness" made TEST-011 fire on a
    # test that verifies its result with a plain assert. Same class of
    # defect as the retired CD-012 check scanning its own detection
    # literals — a text match where a structural one was needed.
    _mock_create_re = re.compile(
        r"\b(?:MagicMock|AsyncMock|mock_\w+)\b|(?<!\.)\bpatch\s*[.(]"
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

    # The same idiom again, written as an assignment rather than a
    # constructor argument: ``sp.find_playlist_by_name.return_value = {...}``,
    # ``g.drive.service.files().get().execute.side_effect = RuntimeError(...)``.
    # This is how a mock built bare and configured afterwards injects
    # behavior, and it is the commoner of the two forms. Reaching it
    # requires a mock to have been created in the body already — that is
    # the condition under which this check runs at all — so an assignment
    # to ``.return_value`` here is mock configuration and nothing else.
    _mock_attr_behavior_injection_re = re.compile(
        r"^\s*[\w.()\[\]\"\'-]+\.(?:return_value|side_effect)\s*=(?!=)",
        re.MULTILINE,
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
          - ``mock.method.return_value = X`` / ``mock.method.side_effect = X``
        """
        injects = (
            _patch_behavior_injection_re.search(body_src)
            or _patch_replacement_re.search(body_src)
            or _mock_ctor_behavior_injection_re.search(body_src)
            or _mock_attr_behavior_injection_re.search(body_src)
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
            body_lines = list(lines[start:end])
            # Blank the docstring before matching. It is documentation,
            # not code, and every pattern below is a text search — prose
            # describing what a test mocks is not the test mocking
            # anything. Blanking rather than deleting keeps line offsets
            # intact for anything that reports positions.
            doc = fn.body[0] if fn.body else None
            if (
                isinstance(doc, ast.Expr)
                and isinstance(doc.value, ast.Constant)
                and isinstance(doc.value.value, str)
            ):
                doc_start = (doc.lineno or 1) - 1 - start
                doc_end = (doc.end_lineno or doc.lineno or 1) - start
                for i in range(max(doc_start, 0), min(doc_end, len(body_lines))):
                    body_lines[i] = ""
            body_src = "\n".join(body_lines)
            if not body_src.strip():
                continue
            match = _mock_create_re.search(body_src)
            if not match:
                continue
            # A checker's own tests quote mock machinery as fixture text —
            # `_write(tmp_path, "tests/t.py", "from unittest.mock import
            # patch\n...")`. That is a string being handed to the code
            # under test, not this test creating a mock. When every hit
            # sits inside a string literal, there is no mock here.
            if _is_inside_string_literal(body_src, match.group(0)):
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
                    "testing_coverage",
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
