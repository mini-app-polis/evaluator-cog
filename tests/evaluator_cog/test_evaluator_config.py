"""Tests for evaluator.yaml loading and EvaluatorConfig (ADR-001 / ADR-002)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluator_cog.engine.evaluator_config import (
    VALID_REPO_TYPES,
    EvaluatorConfig,
    _map_legacy_type,
    _parse_evaluator_yaml,
    load_evaluator_config,
)


def test_load_evaluator_config_valid_yaml(tmp_path: Path) -> None:
    """Valid evaluator.yaml yields correct type, traits, exemptions, deferrals."""
    (tmp_path / "evaluator.yaml").write_text(
        """
type: pipeline-cog
traits:
  - logger-primitive
exemptions:
  - rule: DOC-999
    reason: genuinely N/A for this repo
deferrals:
  - rule: DOC-888
    reason: backlog
""".strip()
    )
    cfg = load_evaluator_config(tmp_path)
    assert cfg.repo_type == "pipeline-cog"
    assert cfg.traits == ["logger-primitive"]
    assert cfg.exemption_ids == ["DOC-999"]
    assert cfg.exemption_reasons["DOC-999"] == "genuinely N/A for this repo"
    assert cfg.deferral_ids == ["DOC-888"]
    assert cfg.deferral_reasons["DOC-888"] == "backlog"
    assert cfg.source == "evaluator.yaml"


def test_load_evaluator_config_absent_uses_fallback(tmp_path: Path) -> None:
    """Missing evaluator.yaml uses fallback_type and fallback_exceptions."""
    cfg = load_evaluator_config(
        tmp_path,
        fallback_type="library",
        fallback_exceptions=["FE-001"],
        fallback_exception_reasons={"FE-001": "legacy reason"},
    )
    assert cfg.repo_type == "shared-library"
    assert cfg.traits == []
    assert "FE-001" in cfg.exemption_ids
    assert cfg.exemption_reasons.get("FE-001") == "legacy reason"
    assert cfg.source == "ecosystem.yaml (fallback)"


def test_load_evaluator_config_malformed_yaml_falls_back(tmp_path: Path) -> None:
    """Broken YAML logs and falls back like an absent file."""
    (tmp_path / "evaluator.yaml").write_text("{{not valid yaml::: [[[\n")
    cfg = load_evaluator_config(
        tmp_path,
        fallback_type="api",
        fallback_exceptions=["X-1"],
    )
    assert cfg.repo_type == "api-service"
    assert "X-1" in cfg.exemption_ids
    assert cfg.source == "ecosystem.yaml (fallback)"


def test_parse_evaluator_yaml_invalid_type_raises() -> None:
    with pytest.raises(ValueError, match="invalid type"):
        _parse_evaluator_yaml({"type": "not-a-valid-type"})


def test_parse_evaluator_yaml_unknown_trait_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown traits are ignored with a warning (no exception)."""
    with caplog.at_level("WARNING"):
        cfg = _parse_evaluator_yaml(
            {"type": "pipeline-cog", "traits": ["not-a-real-trait"]}
        )
    assert cfg.traits == []
    assert any("unknown trait" in r.message for r in caplog.records)


_FAKE_CATALOG = {
    "rule_catalog": {
        "PY-006": {
            "applies_to": ["pipeline-cog", "api-service", "shared-library"],
            "modifies": [],
            "status": "requirement",
        },
        "XSTACK-001": {
            "applies_to": [
                "pipeline-cog",
                "trigger-cog",
                "api-service",
                "react-app",
                "shared-library",
            ],
            "modifies": [],
            "status": "requirement",
        },
        "CD-009": {
            "applies_to": ["pipeline-cog", "trigger-cog", "api-service", "react-app"],
            "modifies": [],
            "status": "requirement",
        },
        "CD-015": {
            "applies_to": ["pipeline-cog", "trigger-cog"],
            "modifies": [],
            "status": "requirement",
        },
        "VER-003": {
            "applies_to": [
                "pipeline-cog",
                "trigger-cog",
                "api-service",
                "static-site",
                "react-app",
            ],
            "modifies": [],
            "status": "requirement",
        },
        "VER-005": {
            "applies_to": [
                "pipeline-cog",
                "trigger-cog",
                "api-service",
                "static-site",
                "react-app",
            ],
            "modifies": [],
            "status": "requirement",
        },
        "VER-006": {
            "applies_to": [
                "pipeline-cog",
                "trigger-cog",
                "api-service",
                "static-site",
                "react-app",
            ],
            "modifies": [],
            "status": "requirement",
        },
        "MONO-001": {
            "applies_to": ["api-service", "react-app"],
            "modifies": ["XSTACK-001"],
            "status": "requirement",
        },
        "EVAL-003": {"applies_to": None, "modifies": [], "status": "requirement"},
        "DOC-001": {"applies_to": ["all"], "modifies": [], "status": "requirement"},
    },
    "catalog_schema": {
        "traits": {
            "logger-primitive": {
                "description": "is the logger",
                "exempts": ["CD-009"],
                "downgrades": [],
            },
            "cloudflare-pages": {
                "description": "pages",
                "exempts": ["VER-003", "VER-005", "VER-006"],
                "downgrades": [],
            },
            "multi-flow": {
                "description": "multi",
                "exempts": [],
                "downgrades": [
                    {"rule": "CD-015", "to": "INFO", "reason": "scanner limit"}
                ],
            },
        },
        "repo_types": {"pipeline-cog", "api-service"},
        "statuses": {"requirement", "convention", "gap"},
    },
}


def _cfg_with_catalog(**kwargs) -> EvaluatorConfig:
    """Build an EvaluatorConfig with the fake catalog attached."""
    cfg = EvaluatorConfig(**kwargs)
    cfg.rule_catalog = _FAKE_CATALOG["rule_catalog"]
    cfg.catalog_schema = _FAKE_CATALOG["catalog_schema"]
    return cfg


def test_dispatch_scope_skips_rule_not_applying_to_type() -> None:
    """Step 1 — scope. A rule's applies_to excludes this repo type → SKIP_SCOPE."""
    cfg = _cfg_with_catalog(repo_type="shared-library")
    result = cfg.resolve_dispatch("CD-015")
    assert result.disposition.value == "skip_scope"


def test_dispatch_scope_applies_to_all_matches() -> None:
    """applies_to=[all] means every repo type matches."""
    cfg = _cfg_with_catalog(repo_type="standards-repo")
    result = cfg.resolve_dispatch("DOC-001")
    assert result.disposition.value == "run_default"


def test_dispatch_trait_exempt_short_circuits() -> None:
    """Step 2 — trait exemption beats repo exemption / deferral."""
    cfg = _cfg_with_catalog(
        repo_type="api-service",
        traits=["logger-primitive"],
        exemption_ids=["CD-009"],
        exemption_reasons={"CD-009": "ignored because trait wins first"},
    )
    result = cfg.resolve_dispatch("CD-009")
    assert result.disposition.value == "skip_trait_exempt"
    assert "logger-primitive" in result.reason


def test_dispatch_repo_exempt() -> None:
    """Step 3 — per-repo exemption fires when no trait exempts."""
    cfg = _cfg_with_catalog(
        repo_type="shared-library",
        exemption_ids=["PY-006"],
        exemption_reasons={"PY-006": "not a consumer"},
    )
    result = cfg.resolve_dispatch("PY-006")
    assert result.disposition.value == "skip_repo_exempt"
    assert result.reason == "not a consumer"


def test_dispatch_repo_deferral_runs_with_downgrade() -> None:
    """Step 4 — deferral runs the check but produces RUN_DEFERRED."""
    cfg = _cfg_with_catalog(
        repo_type="api-service",
        deferral_ids=["PY-006"],
        deferral_reasons={"PY-006": "later"},
    )
    result = cfg.resolve_dispatch("PY-006")
    assert result.disposition.value == "run_deferred"
    assert result.reason == "later"


def test_dispatch_trait_downgrade() -> None:
    """Step 5 — trait downgrade. Check runs; severity overridden."""
    cfg = _cfg_with_catalog(
        repo_type="pipeline-cog",
        traits=["multi-flow"],
    )
    result = cfg.resolve_dispatch("CD-015")
    assert result.disposition.value == "run_downgraded"
    assert result.downgraded_severity == "INFO"


def test_dispatch_rule_modifier() -> None:
    """Step 6 — when another rule's modifies: includes this rule
    and the modifier's applies_to matches, RUN_MODIFIED is emitted."""
    cfg = _cfg_with_catalog(repo_type="api-service")
    result = cfg.resolve_dispatch("XSTACK-001")
    assert result.disposition.value == "run_modified"
    assert result.modifier_rule_id == "MONO-001"


def test_dispatch_default_when_nothing_matches() -> None:
    """Step 7 — no scope mismatch, no trait, no exemption, no modifier."""
    cfg = _cfg_with_catalog(repo_type="api-service")
    result = cfg.resolve_dispatch("CD-009")  # applies, no trait active
    assert result.disposition.value == "run_default"


def test_dispatch_applies_to_absent_skip_scope() -> None:
    """ADR-004 — rules without applies_to return SKIP_SCOPE from the
    standard dispatcher. PR 4 routes them via a separate path."""
    cfg = _cfg_with_catalog(repo_type="pipeline-cog")
    result = cfg.resolve_dispatch("EVAL-003")
    assert result.disposition.value == "skip_scope"


def test_all_skipped_ids_no_catalog_returns_only_explicit_exemptions() -> None:
    """Without a catalog attached, all_skipped_ids returns just the
    explicit exemption list."""
    cfg = EvaluatorConfig(
        repo_type="pipeline-cog",
        exemption_ids=["PY-006"],
    )
    assert cfg.all_skipped_ids == frozenset({"PY-006"})


def test_is_deferred() -> None:
    cfg = EvaluatorConfig(
        repo_type="pipeline-cog",
        deferral_ids=["CD-003"],
        deferral_reasons={"CD-003": "later"},
    )
    assert cfg.is_deferred("CD-003") is True
    assert cfg.is_deferred("DOC-001") is False


def test_is_skipped_auto_and_explicit() -> None:
    cfg = _cfg_with_catalog(
        repo_type="shared-library",
        exemption_ids=["FE-777"],
        exemption_reasons={"FE-777": "custom"},
    )
    # CD-015 is scoped to pipeline-cog/trigger-cog — skipped by scope
    assert cfg.is_skipped("CD-015") is True
    # FE-777 is an explicit exemption
    assert cfg.is_skipped("FE-777") is True
    # DOC-001 has applies_to=[all] — not skipped
    assert cfg.is_skipped("DOC-001") is False


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("new_cog", "pipeline-cog"),
        ("new_fastapi_service", "api-service"),
        ("new_hono_service", "api-service"),
        ("new_frontend_site", "static-site"),
        ("new_react_app", "react-app"),
        ("worker", "pipeline-cog"),
        ("api", "api-service"),
        ("library", "shared-library"),
        ("site", "static-site"),
        ("standards", "standards-repo"),
        ("pipeline-cog", "pipeline-cog"),
        ("trigger-cog", "trigger-cog"),
        ("api-service", "api-service"),
        ("shared-library", "shared-library"),
        ("static-site", "static-site"),
        ("react-app", "react-app"),
        ("standards-repo", "standards-repo"),
        (None, "shared-library"),
        ("unknown-legacy-value", "pipeline-cog"),
    ],
)
def test_map_legacy_type(legacy: str | None, expected: str) -> None:
    assert _map_legacy_type(legacy) == expected


@pytest.mark.parametrize(
    (
        "repo_type",
        "pipeline",
        "trigger",
        "api",
        "shared",
        "static",
        "react",
        "std",
        "front",
    ),
    [
        ("pipeline-cog", True, False, False, False, False, False, False, False),
        ("trigger-cog", False, True, False, False, False, False, False, False),
        ("api-service", False, False, True, False, False, False, False, False),
        ("shared-library", False, False, False, True, False, False, False, False),
        ("static-site", False, False, False, False, True, False, False, True),
        ("react-app", False, False, False, False, False, True, False, True),
        ("standards-repo", False, False, False, False, False, False, True, False),
    ],
)
def test_evaluator_config_boolean_properties(
    repo_type: str,
    pipeline: bool,
    trigger: bool,
    api: bool,
    shared: bool,
    static: bool,
    react: bool,
    std: bool,
    front: bool,
) -> None:
    cfg = EvaluatorConfig(repo_type=repo_type)
    assert cfg.is_pipeline_cog is pipeline
    assert cfg.is_trigger_cog is trigger
    assert cfg.is_api_service is api
    assert cfg.is_shared_library is shared
    assert cfg.is_static_site is static
    assert cfg.is_react_app is react
    assert cfg.is_standards_repo is std
    assert cfg.is_frontend is front


def test_language_property_returns_typescript_for_non_python_types() -> None:
    """Repo types that are not Python services return 'typescript' from .language."""
    for repo_type in ("react-app", "static-site", "frontend"):
        cfg = EvaluatorConfig(repo_type=repo_type)
        assert cfg.language == "typescript", (
            f"Expected 'typescript' for repo_type={repo_type!r}, got {cfg.language!r}"
        )


def test_parse_evaluator_yaml_rejects_evaluator_service_type() -> None:
    """evaluator-service was removed from the type taxonomy in v4.0.0
    (ADR-004). Any evaluator.yaml declaring it must be rejected."""
    with pytest.raises(ValueError, match="invalid type"):
        _parse_evaluator_yaml({"type": "evaluator-service"})


def test_parse_evaluator_yaml_rejects_pre_rule_trait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """pre-rule was removed from the trait taxonomy in v4.0.0. It is
    now an unknown trait; unknown traits are warned and ignored."""
    with caplog.at_level("WARNING"):
        cfg = _parse_evaluator_yaml({"type": "pipeline-cog", "traits": ["pre-rule"]})
    assert cfg.traits == []
    assert any("unknown trait" in r.message for r in caplog.records)


# --- deferral `until:` -------------------------------------------------------
#
# `until:` is specified in index.yaml ("If set to a date that has passed,
# the deferral expires and the finding resurfaces at full severity") and
# was parsed nowhere, so every deferral was silently permanent. These
# tests pin the four states: no deadline, a future deadline, a past
# deadline, and an unparseable one.


def _catalog(rule="CD-019"):
    return {rule: {"applies_to": ["all"], "modifies": [], "status": "requirement"}}


def test_until_absent_defers_open_endedly() -> None:
    import datetime as dt

    from evaluator_cog.engine.evaluator_config import Disposition, EvaluatorConfig

    cfg = EvaluatorConfig(
        repo_type="api-service",
        deferral_ids=["CD-019"],
        deferral_reasons={"CD-019": "not yet"},
        rule_catalog=_catalog(),
    )
    result = cfg.resolve_dispatch("CD-019", today=dt.date(2099, 1, 1))
    assert result.disposition == Disposition.RUN_DEFERRED
    assert result.deferral_expired_on is None
    assert cfg.is_deferred("CD-019") is True


def test_until_in_the_future_still_defers() -> None:
    import datetime as dt

    from evaluator_cog.engine.evaluator_config import Disposition, EvaluatorConfig

    cfg = EvaluatorConfig(
        repo_type="api-service",
        deferral_ids=["CD-019"],
        deferral_until={"CD-019": dt.date(2026, 12, 1)},
        rule_catalog=_catalog(),
    )
    result = cfg.resolve_dispatch("CD-019", today=dt.date(2026, 9, 3))
    assert result.disposition == Disposition.RUN_DEFERRED
    assert result.deferral_expired_on is None


def test_until_on_the_deadline_day_still_defers() -> None:
    """The boundary is inclusive — the deferral is live through its last day."""
    import datetime as dt

    from evaluator_cog.engine.evaluator_config import Disposition, EvaluatorConfig

    cfg = EvaluatorConfig(
        repo_type="api-service",
        deferral_ids=["CD-019"],
        deferral_until={"CD-019": dt.date(2026, 9, 3)},
        rule_catalog=_catalog(),
    )
    result = cfg.resolve_dispatch("CD-019", today=dt.date(2026, 9, 3))
    assert result.disposition == Disposition.RUN_DEFERRED


def test_until_in_the_past_expires_the_deferral() -> None:
    """The whole point: a passed deadline resurfaces the finding."""
    import datetime as dt

    from evaluator_cog.engine.evaluator_config import Disposition, EvaluatorConfig

    cfg = EvaluatorConfig(
        repo_type="api-service",
        deferral_ids=["CD-019"],
        deferral_reasons={"CD-019": "deejaytools-com-api predates the contract"},
        deferral_until={"CD-019": dt.date(2026, 6, 1)},
        rule_catalog=_catalog(),
    )
    result = cfg.resolve_dispatch("CD-019", today=dt.date(2026, 9, 3))
    assert result.disposition == Disposition.RUN_DEFAULT
    assert result.deferral_expired_on == dt.date(2026, 6, 1)
    assert result.should_run is True


def test_expired_deferral_still_yields_to_a_trait_downgrade() -> None:
    """An expired deferral falls through; it does not jump to full severity."""
    import datetime as dt

    from evaluator_cog.engine.evaluator_config import Disposition, EvaluatorConfig

    cfg = EvaluatorConfig(
        repo_type="api-service",
        traits=["logger-primitive"],
        deferral_ids=["CD-019"],
        deferral_until={"CD-019": dt.date(2026, 6, 1)},
        rule_catalog=_catalog(),
        catalog_schema={
            "traits": {
                "logger-primitive": {
                    "downgrades": [{"rule": "CD-019", "to": "WARN", "reason": "x"}]
                }
            }
        },
    )
    result = cfg.resolve_dispatch("CD-019", today=dt.date(2026, 9, 3))
    assert result.disposition == Disposition.RUN_DOWNGRADED
    assert result.downgraded_severity == "WARN"
    assert result.deferral_expired_on == dt.date(2026, 6, 1)


def test_expired_deferral_reports_not_deferred() -> None:
    import datetime as dt

    from evaluator_cog.engine.evaluator_config import EvaluatorConfig

    cfg = EvaluatorConfig(
        repo_type="api-service",
        deferral_ids=["CD-019"],
        deferral_until={"CD-019": dt.date(2020, 1, 1)},
    )
    assert cfg.is_deferred("CD-019") is False


def test_until_is_parsed_from_evaluator_yaml(tmp_path) -> None:
    """A bare YAML date, a quoted one, and null all round-trip correctly."""
    import datetime as dt

    from evaluator_cog.engine.evaluator_config import load_evaluator_config

    (tmp_path / "evaluator.yaml").write_text(
        "type: api-service\n"
        "deferrals:\n"
        "  - rule: CD-019\n"
        "    reason: predates the credential contract\n"
        "    until: 2026-12-01\n"
        "  - rule: OPS-001\n"
        "    reason: no restore drill yet\n"
        '    until: "2027-01-15"\n'
        "  - rule: OPS-006\n"
        "    reason: open ended\n"
        "    until: null\n"
    )
    cfg = load_evaluator_config(tmp_path)
    assert cfg.deferral_until["CD-019"] == dt.date(2026, 12, 1)
    assert cfg.deferral_until["OPS-001"] == dt.date(2027, 1, 15)
    assert "OPS-006" not in cfg.deferral_until


def test_unparseable_until_is_treated_as_open_ended(tmp_path) -> None:
    """A typo must not resurface a finding at full severity."""
    from evaluator_cog.engine.evaluator_config import load_evaluator_config

    (tmp_path / "evaluator.yaml").write_text(
        "type: api-service\n"
        "deferrals:\n"
        "  - rule: CD-019\n"
        "    reason: predates the credential contract\n"
        '    until: "next quarter"\n'
    )
    cfg = load_evaluator_config(tmp_path)
    assert "CD-019" in cfg.deferral_ids
    assert "CD-019" not in cfg.deferral_until
    assert cfg.is_deferred("CD-019") is True


def test_repo_type_vocabulary_comes_from_the_catalog_when_available() -> None:
    """A type added to index.yaml works without releasing this cog.

    VALID_REPO_TYPES is a copy of the catalog's schema.repo_types, and a
    copy goes stale. When the catalog fetch succeeded it is authoritative.
    """
    cfg = _parse_evaluator_yaml(
        {"type": "brand-new-type"},
        catalog_schema={"repo_types": {"brand-new-type": "docs"}},
    )
    assert cfg.repo_type == "brand-new-type"


def test_shared_workflows_is_a_known_type() -> None:
    """The hardcoded fallback set had gone stale against index.yaml.

    .github declares type: shared-workflows. Before this, the parse
    raised, the loader fell back, and _map_legacy_type defaulted the repo
    to pipeline-cog — so four YAML files were graded as a deployed
    Prefect worker and collected fifteen findings.
    """
    assert "shared-workflows" in VALID_REPO_TYPES
    assert _parse_evaluator_yaml({"type": "shared-workflows"}).repo_type == (
        "shared-workflows"
    )


def test_unknown_type_still_falls_back_but_warns(caplog) -> None:
    """The default is kept so one bad entry cannot kill a run — loudly."""
    with caplog.at_level("WARNING"):
        assert _map_legacy_type("not-a-real-type") == "pipeline-cog"
    assert any("unrecognised repo type" in r.message for r in caplog.records)
