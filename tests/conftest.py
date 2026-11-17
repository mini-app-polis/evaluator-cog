"""Shared pytest configuration for evaluator-cog.

Pins STANDARDS_VERSION and ENVIRONMENT so tests do not depend on the
launching shell or reach the network for standards metadata.

There is no catalog-cache fixture. The catalog lives on the run's
:class:`RunContext` rather than in module state, so a test that builds
its own context cannot inherit another test's catalog and there is
nothing between tests to clear.

There is no Prefect fixture either. ``evaluator_cog`` imports nothing
from ``prefect`` (ADR-0004), and ``mini_app_polis`` reaches for it lazily
and falls back to the stdlib logger when it is absent, so there is no
orchestration for the suite to isolate itself from.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _production_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the environment so assertions do not depend on the host shell.

    Effect gates and the Discord title prefix both resolve from the
    environment, and an unset one resolves to local. Left to inherit
    whatever ENVIRONMENT the launching shell carries, this suite asserts
    different rendered titles on a laptop than in CI — and the lenient
    run is the one that hides the regression. Tests that want the
    non-production path set ENVIRONMENT themselves.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")


@pytest.fixture(autouse=True)
def _pipeline_config_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve CD-041's pipeline.yaml from memory instead of GitHub.

    Every run_all_checks call reads ``current_major`` for CD-041. Tests
    that care about the value set their own; the rest get a fixed one so
    the suite never reaches raw.githubusercontent.com.
    """
    from evaluator_cog.engine.deterministic import delivery

    monkeypatch.setattr(
        delivery, "_fetch_pipeline_config", lambda: {"current_major": 1}
    )
