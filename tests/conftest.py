"""Pytest configuration — shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _default_standards_version_for_pipeline_eval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin STANDARDS_VERSION so evaluate_pipeline_run does not hit the network in tests."""
    monkeypatch.setenv("STANDARDS_VERSION", "8.8.8-test")
