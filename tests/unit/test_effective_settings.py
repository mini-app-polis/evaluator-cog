"""The worker logs its effective settings once per cold start."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from evaluator_cog.adapters import lambda_worker as lw


def test_settings_are_logged_once_with_values_only_for_allowlisted_names(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sk-ant-not-for-the-log"
    monkeypatch.setattr(lw, "load_secrets", lambda **_: [])
    monkeypatch.setattr(lw, "_settings_logged", False)
    monkeypatch.setenv(
        "SETTINGS_ALLOWLIST",
        "ANTHROPIC_MODEL,EVALUATOR_GIT_TIMEOUT_SECONDS",
    )
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setenv("EVALUATOR_GIT_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    with caplog.at_level(logging.DEBUG):
        lw.lambda_handler({"Records": []}, None)
        lw.lambda_handler({"Records": []}, None)

    lines = [r.getMessage() for r in caplog.records]
    model = lw.conformance.ANTHROPIC_MODEL_DEFAULT
    assert lines.count(f"ANTHROPIC_MODEL={model} (default)") == 1
    assert lines.count("EVALUATOR_GIT_TIMEOUT_SECONDS=15 (set)") == 1
    assert lines.count("ANTHROPIC_API_KEY (set)") == 1
    assert all(secret not in r.getMessage() for r in caplog.records)
    assert secret not in caplog.text


def test_effective_settings_cover_every_name_env_example_lists() -> None:
    example = Path(__file__).resolve().parents[2] / ".env.example"
    listed = set(re.findall(r"(?m)^(?:# )?([A-Z][A-Z0-9_]*)=", example.read_text()))
    assert listed
    assert listed <= set(lw.effective_settings())
