"""Routing of standards rules to deterministic vs LLM evaluation paths.

Per the 2026-04 standards audit, every checkable rule's `check_notes` is
expected to begin with either:

    DETERMINISTIC CHECK.
    LLM CHECK.

on its own line. This marker routes the rule to the correct engine:

  - DETERMINISTIC CHECK → an explicit check function in engine/deterministic.py
  - LLM CHECK           → the Claude-judged soft-rule pass in engine/llm.py

The marker is advisory at this stage — the standards catalog contains many
pre-audit rules whose check_notes have not yet been back-filled with the
marker. For those rules we log a warning and default to 'deterministic',
matching the handoff guidance ("treat unknown as deterministic for now").
This keeps old rules on their existing execution paths without silently
leaking them into the LLM prompt.

Downstream consumers should prefer the `check_mode` field attached to each
rule by `_fetch_standards_for_service` rather than re-parsing `check_notes`
themselves.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

log = logging.getLogger(__name__)

CheckMode = Literal["deterministic", "llm"]

_MARKER_RE = re.compile(r"^\s*(DETERMINISTIC|LLM)\s+CHECK\s*\.", re.IGNORECASE)

# Rules we've already warned about in this process — prevents log spam
# when the same rule appears in multiple flow invocations within one
# Prefect worker process.
_warned_once: set[str] = set()


def classify_check_mode(
    rule_id: str | None,
    check_notes: str | None,
) -> CheckMode:
    """Classify a rule as deterministic or LLM based on its check_notes marker.

    Returns 'deterministic' for rules whose check_notes starts with
    'DETERMINISTIC CHECK.' or which lack a marker entirely (legacy rules).
    Returns 'llm' for rules whose check_notes starts with 'LLM CHECK.'.

    For rules missing a marker, logs a warning once per rule_id per process.
    Never raises.
    """
    notes = (check_notes or "").lstrip()
    if not notes:
        _warn_once(
            rule_id,
            "check_notes is empty — defaulting to deterministic routing",
        )
        return "deterministic"

    first_line = notes.splitlines()[0]
    m = _MARKER_RE.match(first_line)
    if not m:
        _warn_once(
            rule_id,
            f"check_notes has no DETERMINISTIC/LLM marker "
            f"(first line: {first_line[:80]!r}) — defaulting to deterministic routing",
        )
        return "deterministic"

    return "llm" if m.group(1).upper() == "LLM" else "deterministic"


def _warn_once(rule_id: str | None, message: str) -> None:
    key = str(rule_id or "<unknown>")
    if key in _warned_once:
        return
    _warned_once.add(key)
    log.warning("routing: rule %s: %s", key, message)


def reset_warning_cache() -> None:
    """Clear the warn-once cache. Intended for tests."""
    _warned_once.clear()
