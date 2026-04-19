"""Git-history-dependent checks.

These checks run `git log` against the cloned repo. They require the repo
to be cloned (not zipball-extracted) so .git/ is available.

If the repo doesn't have a .git directory (older zipball path, or clone
failure), each check returns an empty finding list silently rather than
raising.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

Finding = dict[str, Any]


def _finding(
    rule_id: str,
    severity: str,
    dimension: str,
    finding: str,
    suggestion: str,
) -> Finding:
    return {
        "rule_id": rule_id,
        "violation_id": rule_id or None,
        "severity": severity,
        "dimension": dimension,
        "finding": finding,
        "suggestion": suggestion,
    }


def _has_git_history(repo_path: Path) -> bool:
    """True if the repo was cloned (has .git), False if zipball-extracted."""
    return (repo_path / ".git").is_dir()


def _git(repo_path: Path, *args: str, timeout: int = 30) -> str | None:
    """Run a git command in the repo; return stdout as text, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def check_conventional_commits(repo_path: Path) -> list[Finding]:
    """VER-001: Last 20 commits follow conventional commits pattern.

    Exempt: merge commits and semantic-release chore(release) commits.
    """
    findings: list[Finding] = []
    if not _has_git_history(repo_path):
        return findings

    # %s = subject line, %H = full SHA for referencing, %P = parent SHAs
    # (if %P has two tokens, it's a merge commit).
    out = _git(repo_path, "log", "-20", "--format=%H|%P|%s")
    if not out:
        return findings

    cc_re = re.compile(
        r"^(feat|fix|docs|refactor|chore|test|ci|perf|build|style|revert)"
        r"(\([^)]+\))?: .+"
    )

    for line in out.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, parents, subject = parts
        # Merge commits have two parents — skip
        if len(parents.split()) > 1:
            continue
        # semantic-release release commits — skip
        if subject.startswith("chore(release):") or subject.startswith(
            "chore(release)"
        ):
            continue
        if not cc_re.match(subject):
            findings.append(
                _finding(
                    "VER-001",
                    "WARN",
                    "versioning",
                    f"Commit {sha[:8]} subject does not match conventional-commits "
                    f"pattern: {subject[:80]!r}.",
                    "Use 'type(scope): description' format — e.g. 'fix(auth): handle "
                    "expired tokens'.",
                )
            )
    return findings


def check_breaking_change_on_major_tags(repo_path: Path) -> list[Finding]:
    """VER-002: Major version tags have BREAKING CHANGE in their commit message.

    Scans CHANGELOG.md for `## [X.0.0]` entries. For each, looks up the
    corresponding `vX.0.0` git tag and inspects its commit message for a
    BREAKING CHANGE footer.
    """
    findings: list[Finding] = []
    if not _has_git_history(repo_path):
        return findings

    changelog = repo_path / "CHANGELOG.md"
    if not changelog.exists():
        return findings

    try:
        cl_text = changelog.read_text()
    except Exception:
        return findings

    # Match ## [X.0.0] where X is any major version digit
    major_re = re.compile(r"^##\s*\[(\d+)\.0\.0\]", re.MULTILINE)
    majors = [m.group(1) for m in major_re.finditer(cl_text)]

    for major in majors:
        tag = f"v{major}.0.0"
        # Resolve tag to commit message; try with and without v prefix
        msg = _git(repo_path, "tag", "-l", "--format=%(contents)", tag)
        if not msg:
            # Try without v prefix
            msg = _git(repo_path, "tag", "-l", "--format=%(contents)", f"{major}.0.0")
        if msg is None or not msg.strip():
            # Tag might not exist — that's its own problem but not VER-002's.
            continue
        if "BREAKING CHANGE" not in msg:
            findings.append(
                _finding(
                    "VER-002",
                    "WARN",
                    "versioning",
                    f"Major release {tag} has no BREAKING CHANGE footer in its tag commit.",
                    "Include a 'BREAKING CHANGE:' footer in commits that bump the major version.",
                )
            )
    return findings


def check_fix_commits_touch_tests(repo_path: Path) -> list[Finding]:
    """PRIN-008: Recent fix commits modify test files.

    Fetches all `fix:` / `fix(...):` commits in the last 90 days. For each,
    verifies at least one file under tests/ or matching test_*.py or *.test.ts
    was modified in the commit. Exempt commits that touch only docs or
    build config files.
    """
    findings: list[Finding] = []
    if not _has_git_history(repo_path):
        return findings

    # Get fix commits + their changed files in one pass.
    # --since=90.days.ago filters the time window.
    # --grep=^fix with --extended-regexp filters by subject prefix.
    # --name-only lists changed files per commit.
    out = _git(
        repo_path,
        "log",
        "--since=90.days.ago",
        "--extended-regexp",
        "--grep=^fix(\\([^)]+\\))?:",
        "--format=COMMIT|%H|%s",
        "--name-only",
    )
    if not out:
        return findings

    # Parse output: alternating "COMMIT|sha|subject" lines and file lists.
    blocks: list[tuple[str, str, list[str]]] = []
    current_sha: str | None = None
    current_subject: str = ""
    current_files: list[str] = []
    for line in out.splitlines():
        if line.startswith("COMMIT|"):
            if current_sha is not None:
                blocks.append((current_sha, current_subject, current_files))
            _, sha, subject = line.split("|", 2)
            current_sha = sha
            current_subject = subject
            current_files = []
        elif line.strip():
            current_files.append(line.strip())
    if current_sha is not None:
        blocks.append((current_sha, current_subject, current_files))

    def _is_test_file(path: str) -> bool:
        if path.startswith("tests/") or "/tests/" in path:
            return True
        fname = path.rsplit("/", 1)[-1]
        if fname.startswith("test_") and fname.endswith(".py"):
            return True
        return fname.endswith((".test.ts", ".test.tsx"))

    def _is_exempt_file(path: str) -> bool:
        # Docs-only or build-config-only commits are exempt
        if path.startswith("docs/") or path == "README.md":
            return True
        if path in (
            "pyproject.toml",
            "package.json",
            "uv.lock",
            "pnpm-lock.yaml",
            ".releaserc.json",
        ):
            return True
        return path.startswith(".github/")

    for sha, subject, files in blocks:
        if not files:
            continue
        # Exempt: all files are docs/build
        if all(_is_exempt_file(f) for f in files):
            continue
        # Pass: at least one test file touched
        if any(_is_test_file(f) for f in files):
            continue
        findings.append(
            _finding(
                "PRIN-008",
                "ERROR",
                "principles",
                f"fix commit {sha[:8]} ({subject[:80]!r}) modified no test files.",
                "Every fix should include a test that catches the regression. "
                "Add a test in the same commit or a follow-up within 24 hours.",
            )
        )
    return findings
