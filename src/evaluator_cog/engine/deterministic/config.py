"""Environment / settings / shared-library rule checks."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)


def check_env_var_prefix(repo_path: Path) -> list[Finding]:
    """XSTACK-004: Client-exposed env vars use PUBLIC_ or VITE_ prefix."""
    CHECK_ID = "XSTACK-004"
    import re

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    # Detect repo flavor from presence of astro vs vite config
    has_astro = (repo_path / "astro.config.mjs").exists() or (
        repo_path / "astro.config.ts"
    ).exists()
    has_vite = (
        (repo_path / "vite.config.ts").exists()
        or (repo_path / "vite.config.js").exists()
        or (repo_path / "vite.config.mjs").exists()
    )

    expected_prefix = None
    wrong_prefix = None
    if has_astro:
        expected_prefix = "PUBLIC_"
        wrong_prefix = "VITE_"
    elif has_vite:
        expected_prefix = "VITE_"
        wrong_prefix = "PUBLIC_"
    else:
        return findings  # Not a frontend repo with client-side env vars

    env_re = re.compile(r"""import\.meta\.env\.(\w+)""")
    for f in (
        list(src.rglob("*.ts"))
        + list(src.rglob("*.tsx"))
        + list(src.rglob("*.astro"))
        + list(src.rglob("*.js"))
        + list(src.rglob("*.jsx"))
    ):
        try:
            text = f.read_text()
        except Exception:
            continue
        rel = f.relative_to(repo_path)
        for m in env_re.finditer(text):
            var = m.group(1)
            if var.startswith(wrong_prefix):
                findings.append(
                    _finding(
                        "XSTACK-004",
                        "WARN",
                        "structural_conformance",
                        f"{rel}: env var {var} uses {wrong_prefix} prefix in a repo expecting {expected_prefix}.",
                        f"Rename to {expected_prefix}{var[len(wrong_prefix) :]}.",
                    )
                )
    return findings


def check_logger_misuse(repo_path: Path) -> list[Finding]:
    """CD-008: logger.error not used for expected outcomes."""
    CHECK_ID = "CD-008"
    import re

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    noise_patterns = (
        r"not found",
        r"no files",
        r"skipping",
        r"already exists",
        r"no new items",
    )
    noise_re = re.compile("|".join(noise_patterns), re.IGNORECASE)
    error_call_re = re.compile(r"""logger\.error\s*\(\s*['"]([^'"]+)['"]""")
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for m in error_call_re.finditer(text):
            msg = m.group(1)
            if noise_re.search(msg):
                findings.append(
                    _finding(
                        "CD-008",
                        "WARN",
                        "cd_readiness",
                        f"{rel}: logger.error used for expected outcome: {msg!r}.",
                        "Downgrade to logger.warning or logger.info — errors should "
                        "indicate unexpected failures.",
                    )
                )
    return findings


def check_settings_field_consistency(repo_path: Path) -> list[Finding]:
    """CFG-001: getattr(settings, X) / settings.X keys declared on Settings."""
    CHECK_ID = "CFG-001"
    import ast

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    declared_fields: set[str] = set()
    # First pass: collect fields on any Settings class
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not (node.name == "Settings" or node.name.endswith("Settings")):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    declared_fields.add(stmt.target.id)
                elif isinstance(stmt, ast.Assign):
                    for t in stmt.targets:
                        if isinstance(t, ast.Name):
                            declared_fields.add(t.id)

    if not declared_fields:
        return findings  # No Settings class; rule doesn't apply here

    # Second pass: find getattr(settings, "X") calls and settings.X access
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            # getattr(settings, "X")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "settings"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                key = node.args[1].value
                if key not in declared_fields:
                    findings.append(
                        _finding(
                            "CFG-001",
                            "WARN",
                            "structural_conformance",
                            f"{rel}: getattr(settings, {key!r}) but {key} not declared on Settings.",
                            "Declare the field on Settings or remove the access.",
                        )
                    )
            # settings.KEY
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
                and node.attr not in declared_fields
                and not node.attr.startswith("_")
            ):
                findings.append(
                    _finding(
                        "CFG-001",
                        "WARN",
                        "structural_conformance",
                        f"{rel}: settings.{node.attr} access but not declared on Settings.",
                        "Declare the field on Settings or remove the access.",
                    )
                )
    return findings


def check_env_example_settings_parity(repo_path: Path) -> list[Finding]:
    """CFG-002: .env.example keys match Settings declared fields."""
    CHECK_ID = "CFG-002"
    import ast

    findings: list[Finding] = []
    env_example = repo_path / ".env.example"
    if not env_example.exists():
        return findings
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    declared_fields: set[str] = set()
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not (node.name == "Settings" or node.name.endswith("Settings")):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    declared_fields.add(stmt.target.id)

    if not declared_fields:
        return findings

    env_text = env_example.read_text()
    lines = env_text.splitlines()
    # An "external tooling" comment heads the *block* of keys under it,
    # not one key. api-kaianolevine-com writes five per-machine API keys
    # under a five-line explanation of why they cannot be Settings
    # fields; clearing the marker on the first key flagged the other
    # four, against a file that had already answered the question.
    #
    # A comment block is read as a whole: the marker may appear on any
    # line of it, and the continuation lines that explain the exemption
    # do not cancel it. Only a blank line ends the block, which is how
    # .env files separate their sections in practice.
    prev_is_external_marker = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if "external" in stripped.lower() or "tooling" in stripped.lower():
                prev_is_external_marker = True
            continue
        if not stripped:
            prev_is_external_marker = False
            continue
        if "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        # Uppercase env vars correspond to Settings fields case-insensitively
        if key in declared_fields or key.upper() in (
            f.upper() for f in declared_fields
        ):
            continue
        if prev_is_external_marker:
            continue
        findings.append(
            _finding(
                "CFG-002",
                "WARN",
                "structural_conformance",
                f".env.example key {key!r} not declared on Settings.",
                "Declare the field on Settings or mark the key with a comment noting "
                "'external tooling'.",
            )
        )
    return findings


def check_hardcoded_time_values(
    repo_path: Path, language: str = "python"
) -> list[Finding]:
    """TEST-013: No hardcoded numeric sleeps/timeouts/delays."""
    CHECK_ID = "TEST-013"
    import re

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    if language == "python":
        patterns = [
            re.compile(r"time\.sleep\(\s*(\d+(?:\.\d+)?)\s*\)"),
            re.compile(r"retry_delay_seconds\s*=\s*(\d+)"),
            re.compile(r"\btimeout\s*=\s*(\d+)"),
        ]
        exts = ("*.py",)
    else:
        patterns = [
            re.compile(r"setTimeout\(\s*\w+\s*,\s*(\d+)"),
            re.compile(r"setInterval\(\s*\w+\s*,\s*(\d+)"),
        ]
        exts = ("*.ts", "*.tsx")

    # UI contexts where setTimeout/setInterval values are visual design
    # choices (animation delays, toast timing, progress-bar completion
    # effects), not production retry/timeout values.
    _ui_path_markers = ("/pages/", "/components/", "/layouts/", "/views/")

    for ext in exts:
        for f in src.rglob(ext):
            if language != "python":
                path_str = str(f).replace("\\", "/")
                if f.suffix == ".tsx" or any(m in path_str for m in _ui_path_markers):
                    continue
            try:
                text = f.read_text()
            except Exception:
                continue
            rel = f.relative_to(repo_path)
            for pat in patterns:
                for m in pat.finditer(text):
                    val = m.group(1)
                    if val == "0":
                        continue
                    # Skip if guarded by env/settings — we check if the line
                    # has "os.getenv", "settings.", or "process.env" nearby.
                    start = max(0, m.start() - 200)
                    context = text[start : m.end()]
                    if (
                        "os.getenv" in context
                        or "settings." in context
                        or "process.env" in context
                        or "PYTEST_CURRENT_TEST" in text
                    ):
                        continue
                    findings.append(
                        _finding(
                            "TEST-013",
                            "INFO",
                            "testing_coverage",
                            f"{rel}: hardcoded numeric value {val} in time/retry/timeout call.",
                            "Source from Settings or env var so tests can override with 0.",
                        )
                    )
                    break  # One finding per file is enough
    return findings


def check_shared_library_used(
    repo_path: Path,
    language: str = "python",
    workspace_package_json_text: str | None = None,
) -> list[Finding]:
    """XSTACK-001: Shared library dependency must be declared (2026-04 narrow).

    Hand-rolled logger/auth/response reimplementation heuristics moved to LLM
    rule XSTACK-005; this check only verifies the dependency is present in
    ``pyproject.toml`` / workspace ``package.json`` (MONO-001).
    """
    CHECK_ID = "XSTACK-001"
    findings: list[Finding] = []
    if language == "python":
        pyproject = repo_path / "pyproject.toml"
        py_text = pyproject.read_text().lower() if pyproject.exists() else ""
        if "common-python-utils" not in py_text:
            findings.append(
                _finding(
                    "XSTACK-001",
                    "ERROR",
                    "cross_repo_coherence",
                    "common-python-utils is not declared for this Python service.",
                    "Depend on common-python-utils and consume shared behaviors from it.",
                )
            )
    else:
        pkg = repo_path / "package.json"
        per_app_text = pkg.read_text().lower() if pkg.exists() else ""
        pkg_text = per_app_text + (workspace_package_json_text or "").lower()
        if "common-typescript-utils" not in pkg_text:
            findings.append(
                _finding(
                    "XSTACK-001",
                    "ERROR",
                    "cross_repo_coherence",
                    "common-typescript-utils is not declared for this TypeScript service.",
                    "Depend on common-typescript-utils to avoid re-implementing shared utilities.",
                )
            )
    return findings


def check_standards_freshness(repo_path: Path) -> list[Finding]:
    """PRIN-009: Standards are a living document.

    Checks the timestamp of the most recent commit on main in the
    ecosystem-standards repo via the GitHub API. Flags if more than
    90 days have elapsed. Degrades gracefully on fetch failure
    (rate limit, network, etc.) — returns empty rather than a false
    positive, matching the pre-existing behavior of this check.
    """
    CHECK_ID = "PRIN-009"
    import datetime

    findings: list[Finding] = []
    try:
        import httpx

        url = "https://api.github.com/repos/mini-app-polis/ecosystem-standards/commits/main"
        r = httpx.get(
            url,
            timeout=20.0,
            headers={"Accept": "application/vnd.github+json"},
        )
        r.raise_for_status()
        data = r.json() or {}
        committer = (data.get("commit") or {}).get("committer") or {}
        date_str = str(committer.get("date") or "").strip()
        if not date_str:
            return findings

        # GitHub returns ISO-8601 with trailing 'Z' (e.g. "2026-04-18T14:23:01Z").
        # Python's fromisoformat handles 'Z' natively as of 3.11.
        try:
            commit_dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return findings

        now = datetime.datetime.now(datetime.UTC)
        age_days = (now - commit_dt).days
        if age_days > 90:
            findings.append(
                _finding(
                    "PRIN-009",
                    "WARN",
                    "standards_currency",
                    f"Standards repo appears stale ({age_days} days since last commit).",
                    "Review ecosystem-standards — if no recent changes are warranted, commit an explicit review-attestation entry.",
                )
            )
    except Exception:
        return findings
    return findings


def check_hardcoded_standards_version(repo_path: Path) -> list[Finding]:
    """EVAL-002: Every evaluation references a standards version.

    The pipeline_evaluations column requirement is enforced server-side
    (the write endpoint requires standards_version). The source-side
    portion of this rule: flag hardcoded standards_version string
    literals assigned as defaults, which freeze a stale version into
    the evaluator at build time.

    Flags:
      - `standards_version="X.Y.Z"` as a keyword default or assignment.
      - `STANDARDS_VERSION = "X.Y.Z"` as a module-level constant.

    Exempts:
      - Test files (conftest.py, tests/, test_*.py, *_test.py).
      - Values sourced from env vars or package.json reads (pattern:
        `os.environ.get(...)`, `os.getenv(...)`, or a function call
        returning the version — if the RHS is not a plain string
        literal, do not flag).
    """
    CHECK_ID = "EVAL-002"
    findings: list[Finding] = []

    version_re = re.compile(r"^\d+\.\d+\.\d+(?:[-+][\w.]+)?$")

    def _is_test_file(p: Path) -> bool:
        parts = set(p.parts)
        if "tests" in parts or "test" in parts:
            return True
        name = p.name
        return (
            name == "conftest.py"
            or name.startswith("test_")
            or name.endswith("_test.py")
        )

    scan_roots = [repo_path / "src", repo_path / "engine", repo_path / "flows"]
    py_files: list[Path] = []
    for root in scan_roots:
        if root.is_dir():
            py_files.extend(
                p for p in root.rglob("*.py") if p.is_file() and not _is_test_file(p)
            )

    for py_file in py_files:
        try:
            src = py_file.read_text()
            tree = ast.parse(src)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue

        rel = py_file.relative_to(repo_path)

        for node in ast.walk(tree):
            # Module-level: STANDARDS_VERSION = "X.Y.Z"
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id.upper() == "STANDARDS_VERSION"
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                        and version_re.match(node.value.value)
                    ):
                        findings.append(
                            _finding(
                                CHECK_ID,
                                "ERROR",
                                "standards_currency",
                                f"{rel}:{node.lineno}: hardcoded "
                                f"STANDARDS_VERSION = {node.value.value!r}. "
                                "This freezes a stale version into the build.",
                                "Read standards_version at runtime from "
                                "ecosystem-standards/package.json or from a "
                                "STANDARDS_VERSION environment variable.",
                            )
                        )
            # Function calls with `standards_version="X.Y.Z"` keyword
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if (
                        kw.arg == "standards_version"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                        and version_re.match(kw.value.value)
                    ):
                        findings.append(
                            _finding(
                                CHECK_ID,
                                "ERROR",
                                "standards_currency",
                                f"{rel}:{kw.value.lineno}: hardcoded "
                                f"standards_version={kw.value.value!r} "
                                "passed as a keyword argument.",
                                "Pass standards_version from a runtime "
                                "source (package.json fetch or env var), "
                                "not as a literal.",
                            )
                        )
    return findings
