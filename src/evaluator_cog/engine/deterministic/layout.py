"""Repository layout checks: LAYOUT-001 and LAYOUT-002.

Both read the written layout from the catalog (``schema.layouts`` in
ecosystem-standards' index.yaml), never from a copy here: the layout is
meant to grow as the fleet does, and a copy would make every addition a
two-repo change.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
    _tracked_paths,
)

_DIMENSION = "structural_conformance"


def _entries_for(layouts: dict | None, repo_type: str) -> list[dict]:
    """The layout entries that apply to `repo_type`."""
    entries = (layouts or {}).get("entries") or []
    return [
        e
        for e in entries
        if isinstance(e, dict)
        and e.get("path")
        and (not e.get("types") or repo_type in e.get("types"))
    ]


def _top_level(repo_path: Path) -> dict[str, bool]:
    """Tracked top-level names mapped to whether each is a directory.

    Uses git's index when the repo is its own working tree, so local
    clutter (`.venv/`, caches) never counts; otherwise — the evaluator's
    download, which holds only tracked files — the directory listing.
    """
    tracked = _tracked_paths(repo_path) if (repo_path / ".git").exists() else None
    if tracked is not None:
        names: dict[str, bool] = {}
        for path in tracked:
            head, sep, _ = path.partition("/")
            names[head] = names.get(head, False) or bool(sep)
        return names
    return {p.name: p.is_dir() for p in repo_path.iterdir() if p.name != ".git"}


def _matches(entry_path: str, name: str, is_dir: bool) -> bool:
    if entry_path.endswith("/"):
        return is_dir and fnmatch.fnmatchcase(name, entry_path[:-1])
    return fnmatch.fnmatchcase(name, entry_path)


def check_layout_required(
    repo_path: Path, *, repo_type: str, layouts: dict | None
) -> list[Finding]:
    """LAYOUT-001: the entries the written layout requires are present.

    Entries that name a `rule` are that rule's to report, so a missing
    README.md is DOC-001's finding and not also this one's. With no
    layout in the catalog there is nothing to check against.
    """
    CHECK_ID = "LAYOUT-001"
    if not layouts:
        return []
    present = _top_level(repo_path)
    missing = [
        e["path"]
        for e in _entries_for(layouts, repo_type)
        if e.get("required")
        and not e.get("rule")
        and not any(_matches(e["path"], n, d) for n, d in present.items())
    ]
    if not missing:
        return []
    return [
        _finding(
            CHECK_ID,
            "WARN",
            _DIMENSION,
            f"Required by the written layout for {repo_type} and missing: "
            f"{', '.join(missing)}.",
            "Add each, or, if the layout is wrong for this type, change "
            "schema.layouts in ecosystem-standards' index.yaml.",
        )
    ]


def check_layout_drift(
    repo_path: Path,
    *,
    repo_type: str,
    layouts: dict | None,
    exceptions: dict[str, str] | None = None,
) -> list[Finding]:
    """LAYOUT-002: every top-level entry is in the written layout.

    Reported as one INFO finding naming each unlisted entry. The finding
    asks for a decision rather than a fix — move or delete it, or add it
    to the layout — because either the repo or the layout may be the one
    that is behind. A path in the repo's `layout_exceptions:` is not
    reported; it is a one-off with its reason recorded.
    """
    CHECK_ID = "LAYOUT-002"
    if not layouts:
        return []
    entries = _entries_for(layouts, repo_type)
    excepted = set(exceptions or {})
    unlisted = sorted(
        name + ("/" if is_dir else "")
        for name, is_dir in _top_level(repo_path).items()
        if not any(_matches(e["path"], name, is_dir) for e in entries)
        and name not in excepted
        and f"{name}/" not in excepted
    )
    if not unlisted:
        return []
    return [
        _finding(
            CHECK_ID,
            "INFO",
            _DIMENSION,
            f"Not in the written layout for {repo_type}: {', '.join(unlisted)}.",
            "For each, decide: move or delete it; add it to schema.layouts in "
            "ecosystem-standards' index.yaml if more than one repo will carry "
            "it; or record it under layout_exceptions: in this repo's "
            "evaluator.yaml, with the reason, if only this one will.",
        )
    ]
