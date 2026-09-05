"""Shared types and helpers used across the deterministic check modules."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Finding = dict[str, Any]


# ── Shared-library distribution names ────────────────────────────────────
#
# Two Python names per library, not one, because the fleet migrates one
# consumer at a time. `miniapppolis-common-utils` is what PyPI has served
# since September 2026; `common-python-utils` is what a repo still pinned to
# a pre-5.0.0 git ref declares. A check that accepted only the new name would
# fire on every unmigrated repo — which is most of them, for as long as the
# migration takes — and a check that accepted only the old one would go
# quietly blind the moment a repo switched. Both are wrong in the same way:
# they report on a name rather than on the dependency.
#
# When the last consumer is off git refs, drop the legacy entries and resolve
# the survivor from ecosystem.yaml's `package:` field instead of listing it
# here. See common-python-utils/docs/pypi-package-publishing.md §7.
PYTHON_SHARED_LIBRARY_NAMES = ("miniapppolis-common-utils", "common-python-utils")
IDENTITY_LIBRARY_NAMES = ("miniapppolis-identity", "identity")
TYPESCRIPT_SHARED_LIBRARY_NAMES = ("common-typescript-utils",)


def declares_shared_library(text: str, names: tuple[str, ...]) -> bool:
    """True when `text` mentions any accepted name for a shared library.

    Substring matching, matching the checks this replaces. The import name
    (`mini_app_polis`) is deliberately not accepted: these checks are about
    the dependency being *declared*, and an import proves only that someone
    wrote an import.
    """
    lowered = text.lower()
    return any(name in lowered for name in names)


@dataclass
class CheckResult:
    """Return value of run_all_checks.

    Carries both the list of findings produced by the run and the set of
    rule IDs the deterministic engine actually exercised. The LLM pass
    uses `checked_rule_ids` to suppress soft-rule assessment of rules
    already covered deterministically.
    """

    findings: list[Finding]
    checked_rule_ids: set[str]


def _finding(
    rule_id: str,
    severity: str,
    dimension: str,
    finding: str,
    suggestion: str = "",
) -> Finding:
    return {
        "rule_id": rule_id,
        "violation_id": rule_id or None,
        "severity": severity,
        "dimension": dimension,
        "finding": finding,
        "suggestion": suggestion,
    }


def _dict_key_constant_ids(tree: ast.AST) -> set[int]:
    """ids of every Constant used as a key in a dict display.

    Collected in one walk. The previous shape asked the question per
    constant — ``_ast_constant_is_dict_key(const, tree)`` re-walked the
    whole tree for each one — which made the caller below O(n^2) in AST
    nodes. On evaluator-cog that cost 129 seconds of a 138-second run,
    because CD-005 concatenates every file under ``src/`` into one
    string and asks twice, and this repo's own source doubled in size.
    One walk up front, then a set lookup, is O(n).
    """
    keys: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if key is not None:
                    keys.add(id(key))
    return keys


def _ast_constant_is_dict_key(const: ast.Constant, tree: ast.AST) -> bool:
    """True if this Constant is the key expression of a dict display.

    Retained for callers outside this module; prefer
    :func:`_dict_key_constant_ids` when testing more than one constant
    against the same tree.
    """
    return id(const) in _dict_key_constant_ids(tree)


def _is_inside_string_literal(source: str, match_substring: str) -> bool:
    """Return True if every occurrence of match_substring in source sits
    inside a Python string literal.

    Used by checkers that scan Python files with substring containment
    (e.g. ``if "X-Internal-API-Key" in text``). When the scanned file is
    the checker itself — or a test fixture containing source snippets
    built as string literals — every match is a self-scan artifact, not
    a real occurrence.

    Implementation: parse ``source`` with ast.parse(). If parsing fails,
    return False (conservative — let the caller flag). Walk the AST for
    ast.Constant nodes whose .value is a str containing match_substring.
    Dict literal keys are excluded — ``{{"X-Internal-API-Key": "x"}}`` is
    real usage, not a quoted pattern string.

    Count how many times match_substring appears in total (plain
    source.count(match_substring)) vs. how many times it appears inside
    counted string-literal Constant nodes. If every occurrence is inside
    a string literal, return True; otherwise return False.

    Caveat: this handles the common case of bare string literals. It
    does NOT try to reason about f-strings, concatenated literals, or
    triple-quoted docstrings beyond what ast represents — ast.Constant
    already covers those correctly for our purposes.
    """
    if match_substring not in source:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    dict_key_ids = _dict_key_constant_ids(tree)
    literal_hits = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in dict_key_ids:
            continue
        literal_hits += node.value.count(match_substring)
    total_hits = source.count(match_substring)
    return literal_hits >= total_hits


def _is_checker_self_source(py: Path) -> bool:
    """True if `py` is inside the deterministic checker's own source tree.

    The deterministic checkers pattern-match on literal strings like
    ``"session.add("`` or ``"flow.deploy("`` to detect violations in other
    repos. When a checker runs against evaluator-cog itself, those same
    literals in the checker's own source trigger false positives — the
    checker flags its own detection logic. Any checker that scans Python
    source for pattern strings should skip files where this returns True.

    Implementation note: we match on the POSIX-normalized path substring
    ``/engine/deterministic/`` rather than resolving against repo_path.
    That keeps the check robust to how the scanner was invoked (rglob on
    ``src/`` always produces paths containing this segment for checker
    source under ``src/evaluator_cog/engine/deterministic/``).
    """
    return "/engine/deterministic/" in str(py).replace("\\", "/")


def production_python_text(repo_path: Path) -> str:
    """All Python under `repo_path/src`, concatenated, checker source excluded.

    Six checks each did ``"\n".join(f.read_text() for f in
    src.rglob("*.py"))`` independently, so a repo's source was read and
    joined once per check. Doing it once per repo is the obvious saving,
    but the reason this exists is the exclusion.

    When the evaluator runs against itself, ``src/`` contains the
    deterministic checkers, and every literal they match on becomes a
    hit. CD-005 looks for ``apscheduler`` and finds it in pipeline.py's
    own pattern list; the guard against reporting that then cost 129
    seconds. Other checks already skip their own tree via
    ``_is_checker_self_source``; the ones that concatenate did not,
    because there was no single place to put it.

    Deliberately not cached. Memoising on the directory path saved
    0.7 ms per repo and would return stale text for any caller that
    reads a directory, writes to it, and reads again — which is what
    every test doing so would do. Correctness is worth more than the
    0.7 ms.
    """
    root = repo_path / "src"
    if not root.is_dir():
        return ""
    parts: list[str] = []
    for py in sorted(root.rglob("*.py")):
        if _is_checker_self_source(py):
            continue
        try:
            parts.append(py.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(parts)
