"""CI / delivery / build / observability rule checks (GitHub Actions, logging, secrets)."""

from __future__ import annotations

import ast
import re
from contextlib import suppress
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)


def check_pytest_coverage_in_ci(repo_path: Path) -> list[Finding]:
    """TEST-006: pytest coverage measured in CI."""
    CHECK_ID = "TEST-006"
    findings = []
    ci = repo_path / ".github" / "workflows" / "ci.yml"
    if not ci.exists():
        return findings
    content = ci.read_text()
    if "pytest --cov" not in content and "pytest-cov" not in content:
        findings.append(
            _finding(
                "TEST-006",
                "WARN",
                "testing_coverage",
                "Coverage not measured in CI — pytest --cov not found in ci.yml.",
                "Add --cov flag to pytest invocation in CI.",
            )
        )
    return findings


def check_ci(
    repo_path: Path,
    exceptions: frozenset[str] | None = None,
    monorepo_root: Path | None = None,
) -> list[Finding]:
    """
    Runs all CI checks in one pass.
    Covers: VER-003, VER-005, VER-006.
    """
    CHECK_ID = "VER-003"
    findings = []
    _exc = exceptions or frozenset()
    ci_root = monorepo_root or repo_path
    ci = ci_root / ".github" / "workflows" / "ci.yml"
    if not ci.exists():
        findings.append(
            _finding(
                "VER-003",
                "ERROR",
                "cd_readiness",
                "ci.yml not found at .github/workflows/ci.yml.",
                "Add a CI workflow with test and release jobs.",
            )
        )
        return findings

    content = ci.read_text()

    if "VER-003" not in _exc and "semantic-release" not in content:
        findings.append(
            _finding(
                "VER-003",
                "ERROR",
                "cd_readiness",
                "semantic-release not found in ci.yml.",
                "Add a release job running npx semantic-release.",
            )
        )

    if "VER-005" not in _exc and "fetch-depth: 0" not in content:
        findings.append(
            _finding(
                "VER-005",
                "ERROR",
                "cd_readiness",
                "fetch-depth: 0 absent from ci.yml checkout step.",
                "Add fetch-depth: 0 to the actions/checkout step in the release job.",
            )
        )

    if "VER-006" not in _exc and (
        "npm install --no-save" not in content
        and "pnpm exec semantic-release" not in content
        and "pnpm run semantic-release" not in content
        and "pnpm add" not in content
    ):
        findings.append(
            _finding(
                "VER-006",
                "ERROR",
                "cd_readiness",
                "npm install --no-save step absent from release job.",
                "Add explicit npm install --no-save before npx semantic-release, "
                "or use pnpm exec semantic-release with plugins in devDependencies.",
            )
        )

    return findings


def check_no_print_statements(repo_path: Path) -> list[Finding]:
    """CD-003: No print() statements in production code paths."""
    CHECK_ID = "CD-003"

    findings = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                findings.append(
                    _finding(
                        "CD-003",
                        "WARN",
                        "cd_readiness",
                        f"print() statement found in {py_file.relative_to(repo_path)}.",
                        "Replace with structured logger from common-python-utils.",
                    )
                )
                break  # one finding per file is enough
    return findings


def check_no_hardcoded_urls(repo_path: Path) -> list[Finding]:
    """FE-007: No hardcoded API URLs in source."""
    CHECK_ID = "FE-007"
    import re

    findings = []
    pattern = re.compile(r"https?://(localhost|.*railway\.app|.*up\.railway\.app)")
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        content = py_file.read_text()
        if pattern.search(content):
            findings.append(
                _finding(
                    "FE-007",
                    "ERROR",
                    "structural_conformance",
                    f"Hardcoded API URL found in {py_file.relative_to(repo_path)}.",
                    "Move URL to environment variable.",
                )
            )
    return findings


def _stdlib_logging_is_primary(text: str) -> bool:
    """True when this module reaches for stdlib logging as its own logger.

    Parsed rather than searched. The strings ``import logging`` and
    ``logging.getLogger`` appear in this very file as the patterns a
    text scan would look for, and in any module that documents the rule
    — matching those is matching prose, not code.

    A stdlib logger built inside an ``except`` handler is a fallback for
    a shared logger that was tried first (the Prefect run logger outside
    a run, say), not the module's primary logger, so it does not count.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    fallback: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            for inner in ast.walk(node):
                fallback.add(id(inner))

    for node in ast.walk(tree):
        if id(node) in fallback:
            continue
        if isinstance(node, ast.Import):
            if any(
                a.name == "logging" or a.name.startswith("logging.") for a in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "logging":
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"getLogger", "basicConfig"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "logging"
            ):
                return True
    return False


def check_structured_logging(repo_path: Path) -> list[Finding]:
    """CD-009: Structured logging via shared library."""
    CHECK_ID = "CD-009"
    findings = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        text = py_file.read_text()
        if _stdlib_logging_is_primary(text):
            findings.append(
                _finding(
                    "CD-009",
                    "WARN",
                    "cd_readiness",
                    f"Hand-rolled logging detected in {py_file.relative_to(repo_path)}.",
                    "Use shared structured logger from the shared utility library.",
                )
            )
            break
    for ts_file in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
        text = ts_file.read_text()
        if "console.log(" in text:
            findings.append(
                _finding(
                    "CD-009",
                    "WARN",
                    "cd_readiness",
                    f"console.log used as primary logger in {ts_file.relative_to(repo_path)}.",
                    "Use shared structured logger helpers instead of console.log.",
                )
            )
            break
    return findings


#: Seconds to wait on the `git ls-files` used to decide whether a file is
#: committed. Generous for a metadata read, and bounded so a wedged git
#: cannot stall the run — the check degrades to "git cannot say" instead.
_GIT_QUERY_TIMEOUT_SECONDS = 30


def _tracked_paths(repo_path: Path) -> set[str] | None:
    """Paths git tracks under ``repo_path``, relative to it, or None.

    None means "no answer available" — no working tree above this path
    (the zipball download path), or git failed — and callers must not
    read that as "nothing is tracked".

    The .git directory is looked for upward, not only at ``repo_path``:
    a monorepo service is checked at ``apps/api`` while the working tree
    lives at the repo root, and asking only at the service directory
    would answer "cannot say" for every monorepo.
    """
    root: Path | None = None
    for candidate in (repo_path, *repo_path.parents):
        if (candidate / ".git").exists():
            root = candidate
            break
    if root is None:
        return None
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            timeout=_GIT_QUERY_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None

    try:
        prefix = repo_path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    prefix_str = "" if prefix == Path(".") else prefix.as_posix() + "/"

    tracked: set[str] = set()
    for line in result.stdout.split("\0"):
        if not line:
            continue
        if prefix_str and not line.startswith(prefix_str):
            continue
        tracked.add(line[len(prefix_str) :])
    return tracked


def check_no_hardcoded_secrets(repo_path: Path) -> list[Finding]:
    """CD-011: Doppler as canonical secret store."""
    CHECK_ID = "CD-011"
    import re

    findings = []

    tracked = _tracked_paths(repo_path)
    for env_file in sorted(repo_path.rglob(".env*")):
        name = env_file.name
        if name == ".env.example" or any(
            marker in name for marker in ("example", "sample", "template")
        ):
            continue
        rel = env_file.relative_to(repo_path)
        # The finding says "committed", so check that it is. In the
        # production path the repo arrives as a zipball of tracked files
        # and every match is committed by construction; run the same
        # check against a working tree and an ignored local .env — the
        # very file the rule wants developers to keep — reads as a
        # violation. Only skip when git can answer; a zipball has no
        # .git, and there the earlier behaviour is the correct one.
        if tracked is not None and rel.as_posix() not in tracked:
            continue
        findings.append(
            _finding(
                "CD-011",
                "ERROR",
                "cd_readiness",
                f"Committed env file detected: {env_file.relative_to(repo_path)}.",
                "Remove committed env files and use Doppler-managed runtime secrets.",
            )
        )
        break

    secret_patterns = [
        re.compile(r"sk-[A-Za-z0-9]{16,}"),
        re.compile(r"Bearer\s+[A-Za-z0-9]{20,}"),
        re.compile(r"password\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE),
        re.compile(r"api[_-]?key\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE),
    ]
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    for path in (
        list(src.rglob("*.py")) + list(src.rglob("*.ts")) + list(src.rglob("*.tsx"))
    ):
        text = path.read_text()
        lowered = text.lower()
        if "os.getenv(" in lowered or "process.env" in lowered:
            pass
        for pat in secret_patterns:
            if pat.search(text):
                findings.append(
                    _finding(
                        "CD-011",
                        "ERROR",
                        "cd_readiness",
                        f"Potential hardcoded secret in {path.relative_to(repo_path)}.",
                        "Move secrets to Doppler/runtime env vars and remove literal values.",
                    )
                )
                break
        if any(
            f["rule_id"] == "CD-011" and "hardcoded secret" in f["finding"]
            for f in findings
        ):
            break
    return findings


def check_gha_not_trigger_relay(repo_path: Path) -> list[Finding]:
    """CD-006: GitHub Actions must not relay repository triggers into app code.

    Scans ``.github/workflows`` for ``repository_dispatch`` paired with
    Prefect/deployment invocations, scheduled jobs calling Prefect Cloud, and
    internal trigger HTTP paths. Handles malformed YAML and the YAML 1.1
    ``on:`` → ``true`` quirk via ``suppress`` around ``yaml.safe_load``.
    """
    CHECK_ID = "CD-006"
    findings: list[Finding] = []
    import yaml as _yaml

    wf_dir = repo_path / ".github" / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.rglob("*.yml")) + sorted(wf_dir.rglob("*.yaml")):
            try:
                text = wf.read_text()
            except OSError:
                continue
            low = text.lower()
            rel = str(wf.relative_to(repo_path))
            with suppress(Exception):
                _yaml.safe_load(text)

            if "repository_dispatch" in low:
                relay = any(
                    k in low
                    for k in (
                        "prefect deployment run",
                        "prefect deploy",
                        "run_deployment(",
                        "npx prefect",
                    )
                ) or (
                    "/dispatches" in text
                    and any(k in low for k in ("curl ", "httpx.", "requests."))
                )
                pure_ci = ("pytest" in low or "ruff" in low) and not relay
                if relay and not pure_ci:
                    findings.append(
                        _finding(
                            "CD-006",
                            "WARN",
                            "structural_conformance",
                            f"repository_dispatch workflow appears to relay into automation ({rel}).",
                            "Prefer watcher-cog + Prefect; do not chain GitHub Actions into app invocations.",
                        )
                    )

            if ("schedule" in low or "cron:" in low) and "api.prefect.cloud" in low:
                findings.append(
                    _finding(
                        "CD-006",
                        "WARN",
                        "structural_conformance",
                        f"Scheduled workflow references Prefect Cloud API ({rel}).",
                        "Avoid cron-driven Prefect Cloud calls from GitHub Actions; use Prefect-native scheduling.",
                    )
                )

            if re.search(r"['\"]/v1/(trigger|runs)", text):
                findings.append(
                    _finding(
                        "CD-006",
                        "WARN",
                        "structural_conformance",
                        f"Workflow references internal trigger HTTP path ({rel}).",
                        "Do not POST to internal trigger endpoints from GitHub Actions.",
                    )
                )

    src = repo_path / "src"
    if src.is_dir():
        for py in src.rglob("*.py"):
            if "tests/" in str(py).replace("\\", "/"):
                continue
            try:
                t = py.read_text()
            except OSError:
                continue
            if re.search(
                r"(httpx|requests)\.(post|put)\([^\)]*api\.github\.com/[^\"'\)]+/dispatches",
                t,
                re.I,
            ):
                findings.append(
                    _finding(
                        "CD-006",
                        "WARN",
                        "structural_conformance",
                        f"Python source posts to GitHub dispatches API ({py.relative_to(repo_path)}).",
                        "Use watcher-cog + Prefect instead of repository_dispatch relays.",
                    )
                )
    return findings


def check_migration_in_ci(
    repo_path: Path,
    language: str = "python",
    monorepo_root: Path | None = None,
) -> list[Finding]:
    """API-011: CI runs database migrations on deploy.

    Python (Alembic): ci.yml contains 'alembic upgrade head' in a deploy job.
    TypeScript (Drizzle): ci.yml contains 'drizzle-kit push' or 'drizzle-kit migrate'.
    For monorepo services, also checks the workspace root ci.yml.
    """
    findings: list[Finding] = []

    ci_texts = []
    ci = repo_path / ".github" / "workflows" / "ci.yml"
    if ci.exists():
        with suppress(Exception):
            ci_texts.append(ci.read_text())
    if monorepo_root is not None:
        root_ci = monorepo_root / ".github" / "workflows" / "ci.yml"
        if root_ci.exists():
            with suppress(Exception):
                ci_texts.append(root_ci.read_text())

    if not ci_texts:
        findings.append(
            _finding(
                "API-011",
                "ERROR",
                "structural_conformance",
                "api-service has no .github/workflows/ci.yml — migration steps cannot be verified.",
                "Add a ci.yml with deploy job including migration step.",
            )
        )
        return findings

    combined = "\n".join(ci_texts)

    if language == "python":
        if "alembic upgrade head" not in combined and "alembic upgrade" not in combined:
            findings.append(
                _finding(
                    "API-011",
                    "ERROR",
                    "structural_conformance",
                    "ci.yml has no 'alembic upgrade head' step.",
                    "Add 'alembic upgrade head' to the deploy job so migrations run "
                    "automatically on release.",
                )
            )
    else:
        if "drizzle-kit push" not in combined and "drizzle-kit migrate" not in combined:
            findings.append(
                _finding(
                    "API-011",
                    "ERROR",
                    "structural_conformance",
                    "ci.yml has no 'drizzle-kit push' or 'drizzle-kit migrate' step.",
                    "Add a Drizzle migration step to the deploy job.",
                )
            )
    return findings


def check_three_layer_observability(
    repo_path: Path,
    cog_subtype: str | None = None,
    language: str = "python",
) -> list[Finding]:
    """CD-010: Three-layer observability — Healthchecks + logger + Sentry.

    Language-aware: Python services must use sentry_sdk + common-python-utils;
    TypeScript services must use @sentry/node (or @sentry/react/@sentry/astro)
    + common-typescript-utils. The stack is equivalent; only the package
    names differ.
    """
    findings: list[Finding] = []
    src = repo_path / "src"
    env_example = repo_path / ".env.example"

    env_text = env_example.read_text() if env_example.exists() else ""
    src_text = ""
    if src.is_dir():
        exts = ("*.py",) if language == "python" else ("*.ts", "*.tsx", "*.astro")
        for ext in exts:
            for f in src.rglob(ext):
                try:
                    src_text += "\n" + f.read_text(errors="replace")
                except Exception:
                    continue

    package_json_text = ""
    package_json = repo_path / "package.json"
    if package_json.exists():
        with suppress(Exception):
            package_json_text = package_json.read_text()

    # Layer 1: Healthchecks — only required for worker-style services.
    if cog_subtype in ("pipeline", "trigger"):
        env_has_healthchecks = (
            "HEALTHCHECKS_URL" in env_text or "HEALTHCHECKS_URL_" in env_text
        )
        src_has_ping_signal = "healthchecks.io" in src_text.lower() or (
            "HEALTHCHECKS_URL" in src_text
        )
        if not (env_has_healthchecks and src_has_ping_signal):
            findings.append(
                _finding(
                    "CD-010",
                    "ERROR",
                    "cd_readiness",
                    "Layer 1 missing: no HEALTHCHECKS_URL env var or healthchecks.io ping in source.",
                    "Add HEALTHCHECKS_URL (or a per-service HEALTHCHECKS_URL_<NAME>) "
                    "to .env.example and reference it from the main loop to ping "
                    "healthchecks.io.",
                )
            )

    # Layer 2: structured logging via shared library.
    if language == "python":
        layer2_present = (
            "common_python_utils" in src_text or "mini_app_polis" in src_text
        )
        layer2_hint = (
            "Import the shared logger from common_python_utils "
            "(import package name: mini_app_polis) and use it throughout."
        )
    else:
        layer2_present = "common-typescript-utils" in src_text or (
            "common-typescript-utils" in package_json_text
        )
        layer2_hint = (
            "Import the shared logger from common-typescript-utils "
            "(createLogger) and use it throughout."
        )
    if not layer2_present:
        findings.append(
            _finding(
                "CD-010",
                "ERROR",
                "cd_readiness",
                "Layer 2 missing: no shared-library logger usage.",
                layer2_hint,
            )
        )

    # Layer 3: Sentry.
    if language == "python":
        layer3_present = "sentry_sdk" in src_text and (
            "SENTRY_DSN" in env_text or "SENTRY_DSN" in src_text
        )
        layer3_hint = (
            "Initialise sentry_sdk at entry point and add SENTRY_DSN "
            "(or a service-specific variant) to .env.example."
        )
    else:
        layer3_present = (
            "@sentry/node" in package_json_text
            or "@sentry/react" in package_json_text
            or "@sentry/astro" in package_json_text
        ) and ("SENTRY_DSN" in env_text or "SENTRY_DSN" in src_text)
        layer3_hint = (
            "Install @sentry/node (api) or @sentry/react/@sentry/astro (web), "
            "initialise it at entry point, and add SENTRY_DSN to .env.example."
        )
    if not layer3_present:
        findings.append(
            _finding(
                "CD-010",
                "ERROR",
                "cd_readiness",
                "Layer 3 missing: Sentry integration not detected.",
                layer3_hint,
            )
        )
    return findings


def check_pnpm_lockfile(
    repo_path: Path,
    monorepo_root: Path | None = None,
) -> list[Finding]:
    """XSTACK-003: pnpm for all TypeScript projects."""
    CHECK_ID = "XSTACK-003"
    findings = []
    check_root = monorepo_root or repo_path
    if (check_root / "package-lock.json").exists():
        findings.append(
            _finding(
                "XSTACK-003",
                "ERROR",
                "structural_conformance",
                "package-lock.json found — npm is not the approved package manager for TypeScript projects.",
                "Migrate to pnpm: remove package-lock.json, run pnpm install, commit pnpm-lock.yaml.",
            )
        )
    if (check_root / "yarn.lock").exists():
        findings.append(
            _finding(
                "XSTACK-003",
                "ERROR",
                "structural_conformance",
                "yarn.lock found — yarn is not the approved package manager for TypeScript projects.",
                "Migrate to pnpm: remove yarn.lock, run pnpm install, commit pnpm-lock.yaml.",
            )
        )
    if not (check_root / "pnpm-lock.yaml").exists():
        findings.append(
            _finding(
                "XSTACK-003",
                "WARN",
                "structural_conformance",
                "pnpm-lock.yaml not found — pnpm may not be in use.",
                "Use pnpm as the package manager and commit pnpm-lock.yaml.",
            )
        )
    return findings
