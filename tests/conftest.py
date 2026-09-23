"""Shared pytest configuration for evaluator-cog.

Pins STANDARDS_VERSION and ENVIRONMENT so tests do not depend on the
launching shell or reach the network for standards metadata.

There is no catalog-cache fixture here any more. The catalog lives on
the run's :class:`RunContext` rather than in module state, so a test
that builds its own context cannot inherit another test's catalog and
there is nothing between tests to clear.

There is no Prefect fixture here any more. This file used to open an
ephemeral Prefect backend for the whole session, disable task retries and
silence three Prefect loggers — all of it guarding against flows and tasks
that no longer exist. ``evaluator_cog`` imports nothing from ``prefect``
(ADR-0004), and ``mini_app_polis`` reaches for it lazily and falls back to
the stdlib logger when it is absent, so the harness was isolating the
suite from orchestration the suite cannot perform. Every session paid for
starting a server and a SQLite database to prove it.
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
    monkeypatch.delenv("PREFECT_TRIGGER_ENABLED", raising=False)
    monkeypatch.delenv("HEALTHCHECKS_ENABLED", raising=False)
