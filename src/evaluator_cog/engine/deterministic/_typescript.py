"""What the TypeScript branches of the logging and docstring rules share.

A TypeScript app has no shared logger (XSTACK-001). It keeps one logger
module of its own — a file under ``src/`` whose basename, less its
extension, is ``logger`` — which emits the CD-009 event shape and is the
only place ``console.*`` is called (CD-003).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Folders under src/ that hold dependencies or build output, not source.
_SKIP_DIRS = frozenset({"node_modules", "dist", "build", ".astro", "coverage"})
#: Test files, by the infix before their extension.
_TEST_FILE = re.compile(r"\.(?:test|spec)\.[^.]+$")
#: The CD-009 event fields a logger module must set.
EVENT_FIELDS = ("timestamp", "service", "level", "category", "event", "context")

CONSOLE_CALL = re.compile(r"\bconsole\.(?:log|info|debug|warn|error)\s*\(")
LOGGER_CALL = re.compile(r"\blogger\.(debug|info|warn|error)\s*\(")


def is_typescript_repo(repo_path: Path) -> bool:
    """A repo with package.json and no pyproject.toml."""
    return (repo_path / "package.json").is_file() and not (
        repo_path / "pyproject.toml"
    ).is_file()


def source_files(
    repo_path: Path, suffixes: tuple[str, ...] = (".ts", ".tsx")
) -> list[Path]:
    """Production TypeScript under src/: no test files, no `*.d.ts`, no
    ``migrate_``/``backfill_`` scripts, no dependencies or build output."""
    src = repo_path / "src"
    if not src.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if set(path.relative_to(src).parts[:-1]) & _SKIP_DIRS:
            continue
        name = path.name
        if _TEST_FILE.search(name) or name.endswith(".d.ts"):
            continue
        if name.startswith(("migrate_", "backfill_")):
            continue
        files.append(path)
    return files


def logger_module(repo_path: Path) -> Path | None:
    """The repo's logger module: the file under src/ named ``logger``.

    When more than one file is so named, the one nearest src/ is taken.
    """
    candidates = [
        p
        for p in source_files(repo_path, (".ts", ".tsx", ".js", ".jsx", ".mjs"))
        if p.stem == "logger"
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (len(p.parts), str(p)))


def missing_event_fields(text: str) -> list[str]:
    """The CD-009 fields a logger module's source never sets, as a property
    (``level: x``) or a shorthand property (``level,``)."""
    return [
        name
        for name in EVENT_FIELDS
        if not re.search(rf"(?<![\w$.]){name}\s*(?::|,|\}}|\n)", text)
    ]


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def cli_entry_points(repo_path: Path) -> set[Path]:
    """Files package.json's ``bin`` names, resolved against the repo."""
    try:
        data = json.loads((repo_path / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return set()
    raw = data.get("bin") if isinstance(data, dict) else None
    targets = (
        [raw]
        if isinstance(raw, str)
        else list(raw.values())
        if isinstance(raw, dict)
        else []
    )
    return {
        (repo_path / t).resolve() for t in targets if isinstance(t, str) and t.strip()
    }


def call_strings(text: str, open_paren: int) -> list[str]:
    """String literals among the arguments of the call whose ``(`` is at
    `open_paren`: quoted strings, template text, and object keys' values."""
    depth = 0
    i = open_paren
    while i < len(text):
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                break
        elif ch in "'\"`":
            end = text.find(ch, i + 1)
            if end == -1:
                break
            i = end
        i += 1
    args = text[open_paren : i + 1]
    return [m.group(2) for m in re.finditer(r"(['\"`])((?:(?!\1).)*)\1", args)]
