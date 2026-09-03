"""Shared types and helpers used across the deterministic check modules."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Finding = dict[str, Any]


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
