"""Release / versioning rule checks (.releaserc, conventional commits, changelog)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)

#: EVALUATOR_GIT_TIMEOUT_SECONDS when unset: seconds one git command may take.
GIT_TIMEOUT_SECONDS_DEFAULT = "10"


def check_releaserc(repo_path: Path) -> list[Finding]:
    """VER-003: semantic-release on all repos."""
    CHECK_ID = "VER-003"
    findings = []
    exists = (repo_path / ".releaserc.json").exists()
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

    findings: list[Finding] = []
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


def check_releaserc_assets(repo_path: Path) -> list[Finding]:
    """VER-008: .releaserc.json assets must include all version-managed files."""
    CHECK_ID = "VER-008"
    import json as _json

    findings: list[Finding] = []
    releaserc = repo_path / ".releaserc.json"
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
        timeout = float(
            os.environ.get("EVALUATOR_GIT_TIMEOUT_SECONDS", GIT_TIMEOUT_SECONDS_DEFAULT)
        )
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


#: Markers that stop GitHub Actions on the release commit (VER-009). Both
#: work; `[skip actions]` is the one Cloudflare Pages does not read, so a
#: Pages site uses it to get its release commit built and deployed.
SKIP_ACTIONS_MARKERS = ("[skip ci]", "[skip actions]")


def check_release_commit_message(repo_path: Path) -> list[Finding]:
    """VER-009: the release commit skips Actions and carries the release notes."""
    CHECK_ID = "VER-009"
    findings: list[Finding] = []
    rc = repo_path / ".releaserc.json"
    if not rc.exists():
        # VER-003 owns the absent-config case.
        return findings
    try:
        config = json.loads(rc.read_text())
    except Exception:
        return findings

    message = None
    for plugin in config.get("plugins") or []:
        if (
            isinstance(plugin, list)
            and plugin
            and plugin[0] == "@semantic-release/git"
            and len(plugin) > 1
            and isinstance(plugin[1], dict)
        ):
            message = plugin[1].get("message")
            break
    if not isinstance(message, str):
        # No git plugin means no release commit to annotate.
        return findings

    if not any(marker in message for marker in SKIP_ACTIONS_MARKERS):
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                "cd_readiness",
                "The @semantic-release/git message has neither [skip ci] nor "
                "[skip actions], so the release commit pushed to main "
                "re-triggers the full pipeline, which then finds no "
                "releasable commits and exits.",
                "Add `[skip ci]` to the message: "
                "`chore(release): ${nextRelease.version} [skip ci]` — or "
                "`[skip actions]` on a Cloudflare Pages site, so Pages still "
                "builds the release commit.",
            )
        )
    if "${nextRelease.notes}" not in message:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                "cd_readiness",
                "The @semantic-release/git message omits ${nextRelease.notes}, "
                "so the release commit body is a bare version number and git "
                "log on main does not record what shipped.",
                "Append the notes to the message body: "
                "`...[skip ci]\\n\\n${nextRelease.notes}`.",
            )
        )
    return findings
