"""Deterministic identity-contract checks: AUTH-003, AUTH-004, CD-019.

Each rule gets a passing repo and one fixture per numbered clause, so a
regression tells you which clause broke rather than only that the rule
changed. Three tests carry more weight than the rest:

  - ``test_auth003_flags_factory_route_with_variable_path_and_no_guard``
    is the regression test for the defect the rule exists to catch. Its
    fixture is asserted to contain no string literal holding a path, so
    the day someone reintroduces a path-regex scan the test fails rather
    than quietly passing on a route that scan cannot see.
  - the two ``CLERK_SECRET_KEY`` tests pin the narrow reading of
    CD-019 (2) from both sides: a frontend handing the secret to Clerk's
    server SDK is legitimate and must stay unflagged, while the same
    secret used to obtain or verify a credential presented to an
    ecosystem API is the violation.
  - ``test_cd019_does_not_flag_test_that_mocks_the_auth_layer`` pins the
    exemption, and its companion pins that a stale docstring is not
    covered by it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from evaluator_cog.engine.deterministic.identity import (
    check_auth_003,
    check_auth_004,
    check_cd_019,
)


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _clause(findings: list[dict], rule: str, number: int) -> list[dict]:
    """Findings this rule attributed to one numbered sub-check."""
    marker = f"{rule} ({number}):"
    return [f for f in findings if marker in f["finding"]]


def _texts(findings: list[dict]) -> str:
    return "\n".join(f["finding"] for f in findings)


# A remediation may *name* the retired path to say it is gone ("retired with
# Clerk M2M"). It may never tell the reader to walk it. The retired CD-012
# check was deleted for inverting the architecture in exactly this way, so
# every suggestion is screened for an instruction to acquire an M2M token.
_INVERTS_ARCHITECTURE = re.compile(
    r"(acquire|obtain|mint|request|fetch|exchange|get|use|add|create)\b[^.]{0,40}"
    r"\bm2m\b",
    re.IGNORECASE,
)


def _assert_well_formed(findings: list[dict], rule: str) -> None:
    """Every finding names its rule, its severity and a real remediation."""
    for f in findings:
        assert f["rule_id"] == rule
        assert f["severity"] == "ERROR"
        assert f["dimension"] == "structural_conformance"
        assert len(f["suggestion"]) >= 40
        assert not _INVERTS_ARCHITECTURE.search(f["suggestion"]), (
            f"remediation points back at the removed Clerk M2M path: "
            f"{f['suggestion']!r}"
        )


# --- AUTH-003 ----------------------------------------------------------------


def _guarded_route_repo(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter, Depends\n"
        "from pkg.guard import require_scope\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/pages")\n'
        'async def list_pages(_guard=Depends(require_scope("wiki.page.read"))):\n'
        "    return []\n",
    )
    return tmp_path


def test_auth003_passes_a_scope_guarded_route(tmp_path: Path) -> None:
    assert check_auth_003(_guarded_route_repo(tmp_path)) == []


def test_auth003_returns_nothing_when_the_repo_registers_no_routes(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/pkg/util.py", "VALUE = 1\n")
    assert check_auth_003(tmp_path) == []


def test_auth003_flags_factory_route_with_variable_path_and_no_guard(
    tmp_path: Path,
) -> None:
    """The regression test for the original defect.

    Four routes in wcs_wiki.py were registered by a module-level factory
    onto a router passed in as a parameter, with the path supplied as a
    variable. No literal path exists anywhere in the module, so a scan
    that enumerates routes by matching path strings cannot see them —
    which is how they shipped unguarded. The fixture below is asserted
    to contain no path literal at all, so the check must find the route
    through its registration call or not at all.
    """
    source = (
        "from fastapi import APIRouter\n"
        "\n"
        "\n"
        "def register(router: APIRouter, path: str) -> None:\n"
        "    @router.get(path)\n"
        "    async def list_entries():\n"
        "        return []\n"
    )
    literals = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any("/" in literal for literal in literals), (
        "fixture must contain no literal path — that is the whole point"
    )

    _write(tmp_path, "src/pkg/wcs_wiki.py", source)
    findings = check_auth_003(tmp_path)
    _assert_well_formed(findings, "AUTH-003")
    clause1 = _clause(findings, "AUTH-003", 1)
    assert len(clause1) == 1
    assert "list_entries()" in clause1[0]["finding"]
    assert "factory" in clause1[0]["finding"]


def test_auth003_flags_bare_depends_as_unresolvable_rather_than_passing(
    tmp_path: Path,
) -> None:
    """An unresolvable guard is a finding, never a silent pass.

    ``Depends(current_user)`` is a bare reference: what it requires is
    decided in another module, so the registration site cannot say
    whether the route is scope-guarded or merely authenticated. Recording
    that as "authenticated-only" would downgrade a route that might be
    neither, so the enumerator marks it unresolvable and clause (1)
    reports it.
    """
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter, Depends\n"
        "from pkg.deps import current_user\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/items")\n'
        "async def list_items(user=Depends(current_user)):\n"
        "    return user\n",
    )
    findings = check_auth_003(tmp_path)
    _assert_well_formed(findings, "AUTH-003")
    clause1 = _clause(findings, "AUTH-003", 1)
    assert len(clause1) == 1
    assert "cannot be resolved statically" in clause1[0]["finding"]
    assert "list_items()" in clause1[0]["finding"]


def test_auth003_flags_undocumented_authenticated_only_route(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter, Depends\n"
        "from pkg.auth import verify_session\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/whoami")\n'
        "async def bootstrap_whoami(_s=Depends(verify_session())):\n"
        "    return {}\n",
    )
    _write(tmp_path, "src/pkg/auth.py", '"""Session verification helpers."""\n')
    findings = check_auth_003(tmp_path)
    _assert_well_formed(findings, "AUTH-003")
    clause2 = _clause(findings, "AUTH-003", 2)
    assert len(clause2) == 1
    assert "bootstrap_whoami" in clause2[0]["finding"]


def test_auth003_accepts_authenticated_only_route_named_in_auth_docstring(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter, Depends\n"
        "from pkg.auth import verify_session\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/whoami")\n'
        "async def bootstrap_whoami(_s=Depends(verify_session())):\n"
        "    return {}\n",
    )
    _write(
        tmp_path,
        "src/pkg/auth.py",
        '"""Auth wiring.\n'
        "\n"
        "The only authenticated-only endpoint is bootstrap_whoami, which a\n"
        "caller hits before it knows what scopes it holds.\n"
        '"""\n',
    )
    assert _clause(check_auth_003(tmp_path), "AUTH-003", 2) == []


def test_auth003_flags_principal_read_from_an_identity_header(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter, Depends, Header\n"
        "from pkg.guard import require_scope\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/things")\n'
        "async def list_things(\n"
        '    owner: str = Header(None, alias="X-Owner-Id"),\n'
        '    _guard=Depends(require_scope("wiki.thing.read")),\n'
        "):\n"
        "    return owner\n",
    )
    findings = check_auth_003(tmp_path)
    _assert_well_formed(findings, "AUTH-003")
    clause3 = _clause(findings, "AUTH-003", 3)
    assert len(clause3) == 1
    assert "X-Owner-Id" in clause3[0]["finding"]
    assert "list_things()" in clause3[0]["finding"]


def test_auth003_allows_an_owner_read_as_a_filter_value(tmp_path: Path) -> None:
    """Reading an owner to filter rows is not deriving the principal."""
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter, Depends, Query\n"
        "from pkg.guard import require_scope\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/things")\n'
        "async def list_things(\n"
        "    owner_id: str = Query(None),\n"
        '    _guard=Depends(require_scope("wiki.thing.read")),\n'
        "):\n"
        "    return {}\n",
    )
    assert check_auth_003(tmp_path) == []


def test_auth003_flags_dependency_deciding_access_from_is_admin(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter, Depends\n"
        "from pkg.deps import require_admin\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/admin")\n'
        "async def admin_index(_a=Depends(require_admin())):\n"
        "    return {}\n",
    )
    _write(
        tmp_path,
        "src/pkg/deps.py",
        "from fastapi import HTTPException\n"
        "\n"
        "\n"
        "def require_admin(row=None):\n"
        "    if not row.is_admin:\n"
        "        raise HTTPException(status_code=403)\n"
        "    return row\n",
    )
    findings = check_auth_003(tmp_path)
    _assert_well_formed(findings, "AUTH-003")
    clause4 = _clause(findings, "AUTH-003", 4)
    assert len(clause4) == 1
    assert "is_admin" in clause4[0]["finding"]


def test_auth003_allows_authority_column_mirrored_into_a_role(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter, Depends\n"
        "from pkg.deps import provision\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/admin")\n'
        "async def admin_index(_a=Depends(provision())):\n"
        "    return {}\n",
    )
    _write(
        tmp_path,
        "src/pkg/deps.py",
        "def provision(row=None):\n"
        "    if row.is_admin:\n"
        '        role = "admin"\n'
        "    else:\n"
        '        role = "member"\n'
        "    return role\n",
    )
    assert _clause(check_auth_003(tmp_path), "AUTH-003", 4) == []


def test_auth003_flags_two_and_four_segment_scopes_and_accepts_three(
    tmp_path: Path,
) -> None:
    """The grammar is exactly ``<domain>.<resource>.<action>``."""
    _write(
        tmp_path,
        "src/pkg/routes.py",
        "from fastapi import APIRouter, Depends\n"
        "from pkg.guard import require_scope\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/a")\n'
        'async def read_a(_g=Depends(require_scope("wiki.read"))):\n'
        "    return []\n"
        "\n"
        '@router.get("/b")\n'
        'async def read_b(_g=Depends(require_scope("wiki.page.read.all"))):\n'
        "    return []\n"
        "\n"
        '@router.get("/c")\n'
        'async def read_c(_g=Depends(require_scope("wiki.page.read"))):\n'
        "    return []\n",
    )
    findings = check_auth_003(tmp_path)
    _assert_well_formed(findings, "AUTH-003")
    clause5 = _clause(findings, "AUTH-003", 5)
    assert len(clause5) == 2
    text = _texts(clause5)
    assert "'wiki.read'" in text
    assert "2 dot-separated segments" in text
    assert "'wiki.page.read.all'" in text
    assert "4 dot-separated segments" in text
    assert "read_c()" not in text


# --- AUTH-004 ----------------------------------------------------------------


_GUARD_SOURCE = (
    "from identity.policy import authorize\n"
    "from identity.store import emit_audit, load_grants\n"
    "\n"
    "\n"
    "def require_scope(scope):\n"
    "    def dependency(principal):\n"
    "        decision = authorize(\n"
    "            principal=principal, scope=scope, grants=load_grants(principal)\n"
    "        )\n"
    "        emit_audit(\n"
    '            enforcement_point="pkg.guard",\n'
    "            principal=principal,\n"
    "            scope=scope,\n"
    "            outcome=decision.outcome,\n"
    "            reason=decision.reason,\n"
    "        )\n"
    "        return decision\n"
    "\n"
    "    return dependency\n"
)

_ROUTES_SOURCE = (
    "from fastapi import APIRouter, Depends\n"
    "from pkg.guard import require_scope\n"
    "\n"
    "router = APIRouter()\n"
    "\n"
    '@router.get("/pages")\n'
    'async def list_pages(_g=Depends(require_scope("wiki.page.read"))):\n'
    "    return []\n"
)

_SUITE_SOURCE = (
    "from fastapi.testclient import TestClient\n"
    "from pkg.app import app\n"
    "\n"
    "\n"
    "def test_allowed():\n"
    "    client = TestClient(app)\n"
    '    assert client.get("/pages").status_code == 200\n'
    "\n"
    "\n"
    "def test_denied():\n"
    "    client = TestClient(app)\n"
    '    assert client.get("/pages").status_code == 403\n'
)


def _auth004_repo(tmp_path: Path, *, guard: str = _GUARD_SOURCE) -> Path:
    _write(tmp_path, "src/pkg/routes.py", _ROUTES_SOURCE)
    _write(tmp_path, "src/pkg/guard.py", guard)
    _write(tmp_path, "tests/test_guard.py", _SUITE_SOURCE)
    return tmp_path


def test_auth004_passes_a_service_on_the_shared_contract(tmp_path: Path) -> None:
    assert check_auth_004(_auth004_repo(tmp_path)) == []


def test_auth004_returns_nothing_when_the_repo_registers_no_routes(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/pkg/util.py", "VALUE = 1\n")
    assert check_auth_004(tmp_path) == []


def test_auth004_flags_a_locally_reimplemented_decision(tmp_path: Path) -> None:
    repo = _auth004_repo(
        tmp_path,
        guard=(
            "def can(principal, scope):\n"
            "    for grant in principal.grants:\n"
            "        if grant.scope == scope:\n"
            "            return True\n"
            "    return False\n"
        ),
    )
    findings = check_auth_004(repo)
    _assert_well_formed(findings, "AUTH-004")
    clause1 = _clause(findings, "AUTH-004", 1)
    text = _texts(clause1)
    assert "identity.policy.authorize" in text
    assert "identity.store" in text
    assert "reimplements the authorization decision locally" in text


def test_auth004_flags_audit_emitted_on_only_one_branch(tmp_path: Path) -> None:
    repo = _auth004_repo(
        tmp_path,
        guard=(
            "from fastapi import HTTPException\n"
            "from identity.policy import authorize\n"
            "from identity.store import emit_audit\n"
            "\n"
            "\n"
            "def require_scope(principal, scope):\n"
            "    if authorize(principal=principal, scope=scope):\n"
            "        emit_audit(\n"
            '            enforcement_point="pkg.guard",\n'
            "            principal=principal,\n"
            "            scope=scope,\n"
            '            outcome="allow",\n'
            '            reason="granted",\n'
            "        )\n"
            "        return True\n"
            "    raise HTTPException(status_code=403)\n"
        ),
    )
    findings = check_auth_004(repo)
    _assert_well_formed(findings, "AUTH-004")
    clause2 = _clause(findings, "AUTH-004", 2)
    assert clause2
    assert "only one branch" in _texts(clause2)


def test_auth004_flags_a_guard_that_emits_no_audit_record(tmp_path: Path) -> None:
    repo = _auth004_repo(
        tmp_path,
        guard=(
            "from identity.policy import authorize\n"
            "from identity.store import load_grants\n"
            "\n"
            "\n"
            "def require_scope(principal, scope):\n"
            "    return authorize(\n"
            "        principal=principal, scope=scope, grants=load_grants(principal)\n"
            "    )\n"
        ),
    )
    clause2 = _clause(check_auth_004(repo), "AUTH-004", 2)
    assert clause2
    assert "emits no audit record" in _texts(clause2)


def test_auth004_flags_an_audit_record_missing_required_fields(
    tmp_path: Path,
) -> None:
    repo = _auth004_repo(
        tmp_path,
        guard=(
            "from identity.policy import authorize\n"
            "from identity.store import emit_audit\n"
            "\n"
            "\n"
            "def require_scope(principal, scope):\n"
            "    decision = authorize(principal=principal, scope=scope)\n"
            "    emit_audit(\n"
            '        enforcement_point="pkg.guard",\n'
            "        principal=principal,\n"
            "        scope=scope,\n"
            "    )\n"
            "    return decision\n"
        ),
    )
    findings = check_auth_004(repo)
    _assert_well_formed(findings, "AUTH-004")
    clause3 = _clause(findings, "AUTH-004", 3)
    assert clause3
    text = _texts(clause3)
    assert "outcome" in text
    assert "reason" in text


def test_auth004_flags_a_route_with_two_scope_dependencies(tmp_path: Path) -> None:
    repo = _auth004_repo(tmp_path)
    _write(
        repo,
        "src/pkg/routes.py",
        "from fastapi import APIRouter, Depends\n"
        "from pkg.guard import require_scope\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/pages", dependencies=[Depends(require_scope("wiki.page.read"))])\n'
        'async def list_pages(_g=Depends(require_scope("wiki.page.write"))):\n'
        "    return []\n",
    )
    findings = check_auth_004(repo)
    _assert_well_formed(findings, "AUTH-004")
    clause4 = _clause(findings, "AUTH-004", 4)
    assert len(clause4) == 1
    assert "auditing the same request twice" in clause4[0]["finding"]


def test_auth004_flags_a_suite_that_never_exercises_the_guard(tmp_path: Path) -> None:
    _write(tmp_path, "src/pkg/routes.py", _ROUTES_SOURCE)
    _write(tmp_path, "src/pkg/guard.py", _GUARD_SOURCE)
    _write(
        tmp_path,
        "tests/test_decision.py",
        "from identity.policy import authorize\n"
        "\n"
        "\n"
        "def test_decision():\n"
        '    assert authorize(principal="p", scope="wiki.page.read")\n',
    )
    findings = check_auth_004(tmp_path)
    _assert_well_formed(findings, "AUTH-004")
    clause5 = _clause(findings, "AUTH-004", 5)
    assert len(clause5) == 1
    assert "never exercises the guard end to end" in clause5[0]["finding"]


def test_auth004_flags_a_service_with_no_tests_directory(tmp_path: Path) -> None:
    _write(tmp_path, "src/pkg/routes.py", _ROUTES_SOURCE)
    _write(tmp_path, "src/pkg/guard.py", _GUARD_SOURCE)
    clause5 = _clause(check_auth_004(tmp_path), "AUTH-004", 5)
    assert len(clause5) == 1
    assert "no tests/ directory" in clause5[0]["finding"]


# --- CD-019: caller steps (1)-(4) --------------------------------------------


def test_cd019_passes_a_caller_that_names_its_machine(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/client.py",
        "from mini_app_polis.api import KaianoApiClient\n"
        "\n"
        "\n"
        "def build():\n"
        '    return KaianoApiClient.from_env("evaluator-cog")\n',
    )
    _write(tmp_path, ".env.example", "EVALUATOR_COG_API_KEY=\n")
    assert check_cd_019(tmp_path, repo_type="pipeline-cog") == []


def test_cd019_flags_an_unnamed_ecosystem_client(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/client.py",
        "from mini_app_polis.api import KaianoApiClient\n"
        "\n"
        "\n"
        "def build():\n"
        "    return KaianoApiClient.from_env()\n",
    )
    _write(tmp_path, ".env.example", "EVALUATOR_COG_API_KEY=\n")
    findings = check_cd_019(tmp_path, repo_type="pipeline-cog")
    _assert_well_formed(findings, "CD-019")
    clause1 = _clause(findings, "CD-019", 1)
    assert len(clause1) == 1
    assert "unnamed fallback" in clause1[0]["finding"]


@pytest.mark.parametrize(
    ("symbol", "source"),
    [
        (
            "get_m2m_token",
            "from pkg.legacy import get_m2m_token\n\ntoken = get_m2m_token()\n",
        ),
        (
            "m2m_tokens/verify",
            "import httpx\n"
            "\n"
            "\n"
            "def check(token):\n"
            '    return httpx.post("https://api.example.com/v1/m2m_tokens/verify")\n',
        ),
        (
            "machine_secret",
            "import os\n\nsecret = os.environ.get(machine_secret)\n",
        ),
        (
            "X-Internal-API-Key",
            'def headers(key):\n    return {"X-Internal-API-Key": key}\n',
        ),
        (
            "X-Owner-Id",
            'def owner(request):\n    return request.headers.get("X-Owner-Id")\n',
        ),
        (
            "require_wcs_admin",
            "from pkg.deps import require_wcs_admin\n\nGUARD = require_wcs_admin\n",
        ),
        (
            "require_wcs_service",
            "from fastapi import Depends\n"
            "from pkg.deps import require_wcs_service\n"
            "\n"
            "\n"
            "def handler(caller=Depends(require_wcs_service)):\n"
            "    return caller\n",
        ),
        (
            "get_current_caller",
            "from pkg.deps import get_current_caller\n"
            "\n"
            "\n"
            "def who():\n"
            "    return get_current_caller()\n",
        ),
        (
            "resolve_or_provision",
            "from pkg.store import resolve_or_provision\n"
            "\n"
            "\n"
            "def bind(sub):\n"
            "    return resolve_or_provision(sub)\n",
        ),
    ],
)
def test_cd019_flags_each_retired_symbol(
    tmp_path: Path, symbol: str, source: str
) -> None:
    """Every symbol retired with Clerk M2M is caught by clause (2)."""
    _write(tmp_path, "src/pkg/legacy_use.py", source)
    findings = check_cd_019(tmp_path, repo_type="pipeline-cog")
    _assert_well_formed(findings, "CD-019")
    clause2 = _clause(findings, "CD-019", 2)
    assert clause2, f"{symbol} was not flagged"
    assert repr(symbol) in _texts(clause2)


def test_cd019_flags_retired_symbols_in_env_example_and_readme(
    tmp_path: Path,
) -> None:
    _write(tmp_path, ".env.example", "MACHINE_SECRET=\n")
    _write(tmp_path, "README.md", "Send the X-Internal-API-Key header.\n")
    findings = check_cd_019(tmp_path, repo_type="pipeline-cog")
    _assert_well_formed(findings, "CD-019")
    text = _texts(_clause(findings, "CD-019", 2))
    assert ".env.example" in text
    assert "README.md" in text


def test_cd019_does_not_flag_clerk_secret_key_used_with_the_server_sdk(
    tmp_path: Path,
) -> None:
    """The legitimate use: session handling and profile reads.

    A frontend holds CLERK_SECRET_KEY so Clerk's server SDK can verify
    sessions and read user profiles from Clerk's backend API. That is
    not obtaining or verifying an ecosystem credential, and a blanket
    flag on the variable name would fire on a correct repo.
    """
    _write(
        tmp_path,
        "src/pkg/profiles.py",
        "import os\n"
        "\n"
        "from clerk_backend_api import Clerk\n"
        "\n"
        'clerk = Clerk(bearer_auth=os.environ["CLERK_SECRET_KEY"])\n'
        "\n"
        "\n"
        "def load_profile(user_id):\n"
        "    return clerk.users.get(user_id=user_id)\n"
        "\n"
        "\n"
        "def current_session(token):\n"
        "    return clerk.sessions.verify(token=token)\n",
    )
    assert check_cd_019(tmp_path, repo_type="frontend") == []


def test_cd019_flags_clerk_secret_key_used_to_obtain_a_credential(
    tmp_path: Path,
) -> None:
    """The violating use: the secret buys a credential the repo presents."""
    _write(
        tmp_path,
        "src/pkg/mint.py",
        "import os\n"
        "\n"
        "import httpx\n"
        "\n"
        "\n"
        "def acquire():\n"
        "    response = httpx.post(\n"
        '        "https://api.clerk.com/v1/m2m_tokens",\n'
        "        headers={\n"
        '            "Authorization": f"Bearer {os.environ[\'CLERK_SECRET_KEY\']}"\n'
        "        },\n"
        "    )\n"
        '    return response.json()["token"]\n',
    )
    findings = check_cd_019(tmp_path, repo_type="pipeline-cog")
    _assert_well_formed(findings, "CD-019")
    clause2 = _clause(findings, "CD-019", 2)
    text = _texts(clause2)
    assert "CLERK_SECRET_KEY" in text
    assert "m2m_tokens" in text


def test_cd019_flags_clerk_secret_key_sent_to_an_ecosystem_api(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "src/pkg/push.py",
        "import os\n"
        "\n"
        "import httpx\n"
        "\n"
        "\n"
        "def push(payload):\n"
        '    token = os.environ["CLERK_SECRET_KEY"]\n'
        "    return httpx.post(\n"
        '        "https://api.kaianolevine.com/v1/evaluations",\n'
        '        headers={"Authorization": f"Bearer {token}"},\n'
        "        json=payload,\n"
        "    )\n",
    )
    _write(tmp_path, ".env.example", "EVALUATOR_COG_API_KEY=\n")
    findings = check_cd_019(tmp_path, repo_type="pipeline-cog")
    _assert_well_formed(findings, "CD-019")
    clause2 = _clause(findings, "CD-019", 2)
    assert clause2
    assert "Authorization header on an outbound HTTP call" in _texts(clause2)


_QUOTED_VIOLATION_FIXTURE = '''"""A checker-shaped module: it quotes violating source, it does not run it."""

VIOLATING_SOURCE = """
import os
import httpx

def acquire():
    token = os.environ["CLERK_SECRET_KEY"]
    return httpx.post(
        "https://api.example.com/v1/m2m_tokens/verify",
        headers={"Authorization": f"Bearer {token}"},
    )
"""
'''


def test_cd019_does_not_flag_quoted_violating_source(tmp_path: Path) -> None:
    """A checker's own fixtures are not violations.

    These checks are run against repos that contain checker source, and
    a checker's fixtures quote the very patterns it looks for. The
    retired CD-012 check scanned text with substring containment and so
    flagged its own detection literals; nothing here may do that. The
    module below only *quotes* a violating snippet as a string constant
    and must come back clean, while the same code written out as code is
    flagged by the test above.
    """
    _write(tmp_path, "src/pkg/fixtures.py", _QUOTED_VIOLATION_FIXTURE)
    assert check_cd_019(tmp_path, repo_type="pipeline-cog") == []


def test_cd019_flags_an_authorization_header_from_a_self_minted_jwt(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "src/pkg/headers.py",
        "import jwt\n"
        "\n"
        "\n"
        "def build(secret):\n"
        '    token = jwt.encode({"sub": "evaluator-cog"}, secret, algorithm="HS256")\n'
        '    return {"Authorization": f"Bearer {token}"}\n',
    )
    findings = check_cd_019(tmp_path, repo_type="pipeline-cog")
    _assert_well_formed(findings, "CD-019")
    clause3 = _clause(findings, "CD-019", 3)
    assert len(clause3) == 1
    assert "locally signed JWT" in clause3[0]["finding"]


def test_cd019_flags_env_example_missing_the_machine_key(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/client.py",
        "from mini_app_polis.api import KaianoApiClient\n"
        "\n"
        "\n"
        "def build():\n"
        '    return KaianoApiClient.from_env("wiki-cog")\n',
    )
    _write(tmp_path, ".env.example", "OTHER_SETTING=1\n")
    findings = check_cd_019(tmp_path, repo_type="pipeline-cog")
    _assert_well_formed(findings, "CD-019")
    clause4 = _clause(findings, "CD-019", 4)
    assert len(clause4) == 1
    assert "WIKI_COG_API_KEY" in clause4[0]["finding"]


def test_cd019_flags_a_missing_env_example_for_a_caller(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/client.py",
        "from mini_app_polis.api import KaianoApiClient\n"
        "\n"
        "\n"
        "def build():\n"
        '    return KaianoApiClient.from_env("wiki-cog")\n',
    )
    clause4 = _clause(check_cd_019(tmp_path, repo_type="pipeline-cog"), "CD-019", 4)
    assert len(clause4) == 1
    assert "no .env.example" in clause4[0]["finding"]


def test_cd019_does_not_flag_test_that_mocks_the_auth_layer(tmp_path: Path) -> None:
    """CD-019 (2) exempts tests that mock the auth layer.

    A test that patches the retired surface to prove the new one
    replaced it is not a repo still using the retired surface. The
    exemption is keyed on the mocking machinery being present, not on
    the file merely being a test.
    """
    _write(
        tmp_path,
        "tests/test_auth.py",
        '"""Guard tests."""\n'
        "\n"
        "from unittest.mock import MagicMock\n"
        "\n"
        "from pkg.auth import get_current_caller\n"
        "\n"
        "\n"
        "def test_caller(monkeypatch):\n"
        '    monkeypatch.setattr("pkg.auth.get_current_caller", MagicMock())\n'
        "    assert get_current_caller is not None\n",
    )
    assert check_cd_019(tmp_path, repo_type="pipeline-cog") == []


def test_cd019_flags_a_test_that_really_uses_the_retired_surface(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "tests/test_plain.py",
        "from pkg.auth import get_current_caller\n"
        "\n"
        "\n"
        "def test_caller():\n"
        "    assert get_current_caller() is not None\n",
    )
    clause2 = _clause(check_cd_019(tmp_path, repo_type="pipeline-cog"), "CD-019", 2)
    assert clause2
    assert "'get_current_caller'" in _texts(clause2)


def test_cd019_flags_a_stale_docstring(tmp_path: Path) -> None:
    """Stale docstrings are explicitly not exempt."""
    _write(
        tmp_path,
        "src/pkg/notes.py",
        '"""Legacy note: get_m2m_token used to mint the machine credential."""\n'
        "\n"
        "VALUE = 1\n",
    )
    clause2 = _clause(check_cd_019(tmp_path, repo_type="pipeline-cog"), "CD-019", 2)
    assert len(clause2) == 1
    assert "docstring" in clause2[0]["finding"]


# --- CD-019: receiver steps (5)-(9) ------------------------------------------


_RECEIVER_AUTH_SOURCE = (
    '"""Credential verification, delegated to the shared identity library."""\n'
    "\n"
    "import os\n"
    "\n"
    "from identity import apikey, chain, clerk\n"
    "\n"
    'CLERK_ISSUERS = os.environ["CLERK_ISSUERS"].split(",")\n'
    'MACHINE_KEYS = {"deejay-cog": os.environ["DEEJAY_COG_API_KEY"]}\n'
    "\n"
    "\n"
    "def build_chain():\n"
    "    return chain.build(\n"
    "        clerk_issuers=CLERK_ISSUERS,\n"
    "        machine_keys=MACHINE_KEYS,\n"
    "        clerk_verifier=clerk,\n"
    "        apikey_verifier=apikey,\n"
    "    )\n"
)


def test_cd019_passes_a_receiver_on_the_shared_library(tmp_path: Path) -> None:
    _write(tmp_path, "src/pkg/auth.py", _RECEIVER_AUTH_SOURCE)
    assert check_cd_019(tmp_path, repo_type="api-service") == []


def test_cd019_flags_local_verification_primitives(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/auth.py",
        "import hmac\n"
        "\n"
        "from jose import jwt\n"
        "\n"
        "\n"
        "def verify_token(token, key):\n"
        "    if hmac.compare_digest(token, key):\n"
        "        return True\n"
        '    return jwt.decode(token, key, algorithms=["RS256"])\n',
    )
    findings = check_cd_019(tmp_path, repo_type="api-service")
    _assert_well_formed(findings, "CD-019")
    clause5 = _clause(findings, "CD-019", 5)
    text = _texts(clause5)
    assert "imports nothing from the shared identity library" in text
    assert "constant-time credential comparison" in text
    assert "RS256-family" in text


def test_cd019_flags_a_receiver_configuring_only_one_population(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "src/pkg/auth.py",
        "import os\n"
        "\n"
        "from identity import chain\n"
        "\n"
        'CLERK_ISSUERS = os.environ["CLERK_ISSUERS"].split(",")\n'
        "\n"
        "\n"
        "def build_chain():\n"
        "    return chain.build(clerk_issuers=CLERK_ISSUERS)\n",
    )
    findings = check_cd_019(tmp_path, repo_type="api-service")
    _assert_well_formed(findings, "CD-019")
    clause6 = _clause(findings, "CD-019", 6)
    assert len(clause6) == 1
    assert "machine-key set" in clause6[0]["finding"]


def test_cd019_flags_a_remote_call_on_the_verification_path(tmp_path: Path) -> None:
    _write(tmp_path, "src/pkg/config.py", _RECEIVER_AUTH_SOURCE)
    _write(
        tmp_path,
        "src/pkg/verify.py",
        "import httpx\n"
        "\n"
        "\n"
        "def verify_session(token):\n"
        '    return httpx.get("https://api.clerk.com/v1/sessions/" + token)\n',
    )
    findings = check_cd_019(tmp_path, repo_type="api-service")
    _assert_well_formed(findings, "CD-019")
    clause7 = _clause(findings, "CD-019", 7)
    assert len(clause7) == 1
    assert "Verification is local" in clause7[0]["finding"]


def test_cd019_flags_persisted_key_material_but_exempts_jwks_url(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/pkg/config.py", _RECEIVER_AUTH_SOURCE)
    _write(
        tmp_path,
        "src/pkg/models.py",
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n"
        "\n"
        "Base = declarative_base()\n"
        "\n"
        "\n"
        "class Machine(Base):\n"
        '    __tablename__ = "machines"\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
        "    api_key_hash = Column(String)\n"
        "\n"
        "\n"
        "class Issuer(Base):\n"
        '    __tablename__ = "issuers"\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    jwks_url = Column(String)\n",
    )
    findings = check_cd_019(tmp_path, repo_type="api-service")
    _assert_well_formed(findings, "CD-019")
    clause8 = _clause(findings, "CD-019", 8)
    assert len(clause8) == 1
    assert "api_key_hash" in clause8[0]["finding"]
    assert "jwks_url" not in clause8[0]["finding"]


def test_cd019_flags_a_machine_name_read_from_the_request(tmp_path: Path) -> None:
    _write(tmp_path, "src/pkg/config.py", _RECEIVER_AUTH_SOURCE)
    _write(
        tmp_path,
        "src/pkg/resolve.py",
        "from fastapi import Header\n"
        "\n"
        "\n"
        "def resolve_principal(\n"
        '    machine_name: str = Header(None, alias="X-Machine-Name"),\n'
        "):\n"
        "    return machine_name\n",
    )
    findings = check_cd_019(tmp_path, repo_type="api-service")
    _assert_well_formed(findings, "CD-019")
    clause9 = _clause(findings, "CD-019", 9)
    assert clause9
    assert "resolve_principal()" in _texts(clause9)


# --- CD-019: the repo_type split ---------------------------------------------


def test_cd019_receiver_does_not_demand_an_env_example_when_it_makes_no_calls(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/pkg/auth.py", _RECEIVER_AUTH_SOURCE)
    assert _clause(check_cd_019(tmp_path, repo_type="api-service"), "CD-019", 4) == []


def test_cd019_api_service_with_outbound_calls_runs_both_halves(
    tmp_path: Path,
) -> None:
    """An api-service that also calls out is a caller too."""
    _write(tmp_path, "src/pkg/auth.py", _RECEIVER_AUTH_SOURCE)
    _write(
        tmp_path,
        "src/pkg/outbound.py",
        "from mini_app_polis.api import KaianoApiClient\n"
        "\n"
        "\n"
        "def build():\n"
        "    return KaianoApiClient.from_env()\n",
    )
    findings = check_cd_019(tmp_path, repo_type="api-service")
    _assert_well_formed(findings, "CD-019")
    assert _clause(findings, "CD-019", 1)


def test_cd019_default_repo_type_runs_the_caller_steps(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pkg/legacy.py",
        "from pkg.deps import get_current_caller\n"
        "\n"
        "\n"
        "def who():\n"
        "    return get_current_caller()\n",
    )
    assert _clause(check_cd_019(tmp_path), "CD-019", 2)


# --- never raises ------------------------------------------------------------


def test_checks_never_raise_on_unparseable_or_absent_sources(tmp_path: Path) -> None:
    _write(tmp_path, "src/pkg/broken.py", "def (:\n")
    assert check_auth_003(tmp_path) == []
    assert check_auth_004(tmp_path) == []
    assert isinstance(check_cd_019(tmp_path, repo_type="api-service"), list)

    empty = tmp_path / "empty"
    empty.mkdir()
    assert check_auth_003(empty) == []
    assert check_auth_004(empty) == []
    assert check_cd_019(empty) == []
