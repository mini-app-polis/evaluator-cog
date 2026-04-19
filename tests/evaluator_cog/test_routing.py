"""Tests for engine/routing.py — DETERMINISTIC/LLM check_notes marker classifier."""

from __future__ import annotations

import logging

import pytest

from evaluator_cog.engine.routing import (
    classify_check_mode,
    reset_warning_cache,
)


@pytest.fixture(autouse=True)
def _clear_warn_cache() -> None:
    """Reset the warn-once cache before every test."""
    reset_warning_cache()


def test_deterministic_marker_is_classified_as_deterministic() -> None:
    notes = "DETERMINISTIC CHECK. Scan pyproject.toml for foo."
    assert classify_check_mode("FOO-001", notes) == "deterministic"


def test_llm_marker_is_classified_as_llm() -> None:
    notes = "LLM CHECK. Assess whether the code structure is sensible."
    assert classify_check_mode("FOO-002", notes) == "llm"


def test_marker_is_case_insensitive() -> None:
    assert classify_check_mode("FOO-003", "llm check. assess.") == "llm"
    assert (
        classify_check_mode("FOO-004", "Deterministic Check. Scan.") == "deterministic"
    )


def test_marker_tolerates_leading_whitespace() -> None:
    assert (
        classify_check_mode("FOO-005", "   DETERMINISTIC CHECK. Scan.")
        == "deterministic"
    )
    assert classify_check_mode("FOO-006", "\n   LLM CHECK. Assess.") == "llm"


def test_marker_only_applies_to_first_line() -> None:
    # A marker on a later line should not be respected — the convention is
    # strict about first-line placement to keep the classifier trivial.
    notes = "Some preamble text.\nLLM CHECK. Assess."
    assert classify_check_mode("FOO-007", notes) == "deterministic"


def test_missing_marker_defaults_to_deterministic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="evaluator_cog.engine.routing")
    notes = "Check for README.md at repo root. Flag if absent."
    assert classify_check_mode("DOC-001", notes) == "deterministic"
    assert any("DOC-001" in r.message for r in caplog.records)


def test_empty_notes_defaults_to_deterministic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="evaluator_cog.engine.routing")
    assert classify_check_mode("FOO-008", "") == "deterministic"
    assert classify_check_mode("FOO-009", None) == "deterministic"
    assert any("FOO-008" in r.message for r in caplog.records)
    assert any("FOO-009" in r.message for r in caplog.records)


def test_missing_marker_warns_at_most_once_per_rule(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="evaluator_cog.engine.routing")
    notes = "Check for something."
    for _ in range(5):
        classify_check_mode("FOO-010", notes)
    warnings_for_rule = [r for r in caplog.records if "FOO-010" in r.message]
    assert len(warnings_for_rule) == 1


def test_none_rule_id_does_not_crash() -> None:
    assert classify_check_mode(None, "DETERMINISTIC CHECK. X.") == "deterministic"
    assert classify_check_mode(None, None) == "deterministic"


def test_marker_with_different_punctuation_is_not_matched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The convention is 'DETERMINISTIC CHECK.' with a trailing dot.
    # A variant without the dot should NOT match — if we allowed it, we'd be
    # classifying rules whose check_notes happened to start with the words
    # 'DETERMINISTIC CHECK' in a sentence, rather than as a routing marker.
    caplog.set_level(logging.WARNING, logger="evaluator_cog.engine.routing")
    notes = "DETERMINISTIC CHECK you should do: scan pyproject."
    assert classify_check_mode("FOO-011", notes) == "deterministic"
    # But this should still log a warning — we fell through to the default.
    assert any("FOO-011" in r.message for r in caplog.records)
