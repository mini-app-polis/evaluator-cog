"""Release / versioning rule checks (.releaserc, conventional commits, changelog)."""

from __future__ import annotations

import os
import re
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)


def check_releaserc(
    repo_path: Path, monorepo_root: Path | None = None
) -> list[Finding]:
    """VER-003: semantic-release on all repos."""
    CHECK_ID = "VER-003"
    findings = []
    exists = (repo_path / ".releaserc.json").exists()
    if not exists and monorepo_root:
        exists = (monorepo_root / ".releaserc.json").exists()
    if not exists:
        findings.append(
            _finding(
                "VER-003",
                "ERROR",
                "cd_readiness",
                ".releaserc.json is absent.",
                "Add .releaserc.json and a release job to ci.yml.",
            )
        )
    return findings


def check_no_manual_changelog(repo_path: Path) -> list[Finding]:
    """VER-004: Never manually edit version files or CHANGELOG."""
    CHECK_ID = "VER-004"
    import re

    findings = []
    changelog = repo_path / "CHANGELOG.md"
    if not changelog.exists():
        return findings
    lines = changelog.read_text().splitlines()
    sr_header = re.compile(r"^## \[\d+\.\d+\.\d+\]\(.+\) \(\d{4}-\d{2}-\d{2}\)$")
    bad_header = re.compile(r"^##\s+\d+\.\d+\.\d+")
    for line in lines:
        if bad_header.match(line) and not sr_header.match(line):
            findings.append(
                _finding(
                    "VER-004",
                    "ERROR",
                    "cd_readiness",
                    "CHANGELOG.md appears manually edited with non-semantic-release headers.",
                    "Let semantic-release manage version and changelog sections.",
                )
            )
            break
    return findings


def check_releaserc_assets(
    repo_path: Path, monorepo_root: Path | None = None
) -> list[Finding]:
    """VER-008: .releaserc.json assets must include all version-managed files."""
    CHECK_ID = "VER-008"
    import json as _json

    findings = []
    releaserc = repo_path / ".releaserc.json"
    if not releaserc.exists() and monorepo_root:
        releaserc = monorepo_root / ".releaserc.json"
    if not releaserc.exists():
        return findings
    try:
        data = _json.loads(releaserc.read_text())
    except Exception:
        return findings

    plugins = data.get("plugins", [])
    prepare_cmd = ""
    git_assets: list[str] = []

    for plugin in plugins:
        if isinstance(plugin, list) and len(plugin) >= 2:
            name, config = plugin[0], plugin[1]
            if "@semantic-release/exec" in str(name):
                prepare_cmd = config.get("prepareCmd", "")
            if "@semantic-release/git" in str(name):
                git_assets = config.get("assets", [])

    # Detect files written by prepareCmd
    managed_files = []
    for candidate in ("pyproject.toml", "package.json", "index.yaml"):
        if candidate in prepare_cmd:
            managed_files.append(candidate)

    if "CHANGELOG.md" not in git_assets:
        findings.append(
            _finding(
                "VER-008",
                "ERROR",
                "structural_conformance",
                "CHANGELOG.md is absent from @semantic-release/git assets.",
                "Add CHANGELOG.md to the assets array in the @semantic-release/git plugin config.",
            )
        )

    for f in managed_files:
        if f not in git_assets:
            findings.append(
                _finding(
                    "VER-008",
                    "ERROR",
                    "structural_conformance",
                    f"{f} is written by prepareCmd but absent from @semantic-release/git assets.",
                    f"Add {f} to the assets array in the @semantic-release/git plugin config.",
                )
            )
    return findings


def _git_log_lines(repo_path: Path, args: list[str]) -> list[str]:
    """Run `git log ...` in repo_path and return lines. Empty on failure."""
    import subprocess

    if not (repo_path / ".git").is_dir():
        return []
    try:
        timeout = float(os.environ.get("EVALUATOR_GIT_TIMEOUT_SECONDS", "10"))
        result = subprocess.run(
            ["git", "-C", str(repo_path), "log", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def check_conventional_commits(repo_path: Path) -> list[Finding]:
    """VER-001: Conventional Commits format.

    Scans the last 20 commit subjects on the current branch. Flags
    any subject not matching the conventional-commit grammar.

    Exempts merge commits, semantic-release release commits
    (`chore(release):`), and revert commits (`Revert "`).

    When run against a repo downloaded as a zip (no .git directory —
    this is the evaluator-cog download path), returns an empty list
    silently. VER-001 is best-effort in that environment; the rule
    is enforced primarily by semantic-release at release time.
    """
    CHECK_ID = "VER-001"
    findings: list[Finding] = []

    subjects = _git_log_lines(repo_path, ["-n", "20", "--pretty=format:%s"])
    if not subjects:
        return findings

    cc_re = re.compile(
        r"^(feat|fix|docs|refactor|chore|test|ci|perf|build|style)"
        r"(\([^)]+\))?!?: .+"
    )

    for subj in subjects:
        if subj.startswith("Merge branch ") or subj.startswith("Merge pull request "):
            continue
        if subj.startswith("chore(release):"):
            continue
        if subj.startswith('Revert "'):
            continue
        if cc_re.match(subj):
            continue
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                "cd_readiness",
                f"Commit subject does not follow Conventional Commits: {subj[:100]!r}.",
                "Rewrite the subject as `<type>(<scope>): <summary>` "
                "with type in: feat, fix, docs, refactor, chore, test, "
                "ci, perf, build, style.",
            )
        )
    return findings


def check_breaking_change_footer(repo_path: Path) -> list[Finding]:
    """VER-002: BREAKING CHANGE footer for major bumps.

    Scans CHANGELOG.md for major version entries (vX.0.0 where X >= 1).
    For each, checks the corresponding git tag's commit message for
    a `BREAKING CHANGE:` footer OR a `!:` subject-line shorthand.

    When run against a repo without .git (zip download), returns empty.
    """
    CHECK_ID = "VER-002"
    findings: list[Finding] = []

    changelog = repo_path / "CHANGELOG.md"
    if not changelog.is_file():
        return findings
    try:
        text = changelog.read_text()
    except (OSError, UnicodeDecodeError):
        return findings

    major_re = re.compile(r"^##\s*\[?(\d+)\.0\.0\]?", re.MULTILINE)
    majors = [m.group(1) for m in major_re.finditer(text) if int(m.group(1)) >= 1]
    if not majors:
        return findings

    if not (repo_path / ".git").is_dir():
        return findings

    import subprocess

    try:
        tags_result = subprocess.run(
            ["git", "-C", str(repo_path), "tag", "--list"],
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("EVALUATOR_GIT_TIMEOUT_SECONDS", "10")),
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return findings
    if tags_result.returncode != 0:
        return findings
    available_tags = set(tags_result.stdout.split())

    breaking_subject_re = re.compile(r"^(feat|fix|refactor)(\([^)]+\))?!:")

    for major in majors:
        candidate_tags = [f"v{major}.0.0", f"{major}.0.0"]
        tag = next((t for t in candidate_tags if t in available_tags), None)
        if tag is None:
            # No matching tag — CHANGELOG-only entry; covered by VER-004.
            continue
        body_lines = _git_log_lines(repo_path, ["-1", "--format=%B", tag])
        body = "\n".join(body_lines)
        has_footer = any(ln.strip().startswith("BREAKING CHANGE:") for ln in body_lines)
        subject = body_lines[0] if body_lines else ""
        has_shorthand = bool(breaking_subject_re.match(subject))
        if not (has_footer or has_shorthand):
            findings.append(
                _finding(
                    CHECK_ID,
                    "WARN",
                    "cd_readiness",
                    f"Major version tag {tag} does not declare a breaking "
                    f"change: no `BREAKING CHANGE:` footer and no `!:` "
                    f"subject shorthand.",
                    "Amend the release commit (or the original feat/fix "
                    "commit that drove the major bump) to include a "
                    "`BREAKING CHANGE: <description>` footer.",
                )
            )
    return findings
