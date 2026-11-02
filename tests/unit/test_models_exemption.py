"""Exemptions that name the models they cover (`models:`)."""

from __future__ import annotations

from pathlib import Path

from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.evaluator_config import (
    EvaluatorConfig,
    NarrowedExemption,
    _parse_evaluator_yaml,
)

_MODELS = """\
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(primary_key=True)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(primary_key=True)
"""


def _finding(model: str | None, file: str = "src/app/models.py") -> dict:
    out = {"rule_id": "API-006", "file": file}
    if model is not None:
        out["model"] = model
    return out


def test_models_alone_cover_the_named_model_in_any_file() -> None:
    ex = NarrowedExemption(reason="r", models=("Run",))
    assert ex.covers(_finding("Run"))
    assert ex.covers(_finding("Run", file="src/other.py"))
    assert not ex.covers(_finding("Event"))


def test_models_with_paths_cover_only_the_named_model_in_the_named_files() -> None:
    ex = NarrowedExemption(reason="r", paths=("src/app/models.py",), models=("Run",))
    assert ex.covers(_finding("Run"))
    assert not ex.covers(_finding("Event"))
    assert not ex.covers(_finding("Run", file="src/other.py"))


def test_models_take_globs() -> None:
    ex = NarrowedExemption(reason="r", models=("Wcs*",))
    assert ex.covers(_finding("WcsQaEvalRun"))
    assert not ex.covers(_finding("Event"))


def test_a_finding_with_no_model_is_not_covered_by_a_models_exemption() -> None:
    assert not NarrowedExemption(reason="r", models=("Run",)).covers(_finding(None))
    assert not NarrowedExemption(
        reason="r", paths=("src/app/models.py",), models=("Run",)
    ).covers(_finding(None))


def test_paths_and_routes_alone_behave_as_before() -> None:
    ex = NarrowedExemption(reason="r", paths=("src/app/models.py",))
    assert ex.covers(_finding("Event")) and ex.covers(_finding(None))
    assert NarrowedExemption(reason="r", routes=("/internal/*",)).covers(
        {"route": "/internal/tick"}
    )


def _parse(exemption: dict) -> EvaluatorConfig:
    return _parse_evaluator_yaml({"type": "api-service", "exemptions": [exemption]})


def test_models_are_read_from_evaluator_yaml() -> None:
    cfg = _parse(
        {
            "rule": "API-006",
            "reason": "eval runs are not user data",
            "paths": ["src/app/models.py"],
            "models": ["Run"],
        }
    )
    (ex,) = cfg.narrowed_exemptions["API-006"]
    assert ex.models == ("Run",) and ex.paths == ("src/app/models.py",)
    assert "API-006" not in cfg.exemption_ids


def test_a_malformed_models_list_narrows_to_nothing() -> None:
    cfg = _parse(
        {
            "rule": "API-006",
            "reason": "typo",
            "paths": ["src/app/models.py"],
            "models": "Run",
        }
    )
    (ex,) = cfg.narrowed_exemptions["API-006"]
    assert not ex.covers(_finding("Run")) and not ex.covers(_finding("Event"))


def test_a_models_exemption_drops_only_that_model_in_a_real_run(tmp_path: Path) -> None:
    models = tmp_path / "src/app/models.py"
    models.parent.mkdir(parents=True)
    models.write_text(_MODELS)
    cfg = EvaluatorConfig(
        repo_type="api-service",
        narrowed_exemptions={
            "API-006": [
                NarrowedExemption(
                    reason="r", paths=("src/app/models.py",), models=("Run",)
                )
            ]
        },
    )
    cfg.rule_catalog = {
        "API-006": {
            "applies_to": ["api-service"],
            "modifies": [],
            "status": "requirement",
        }
    }
    cfg.catalog_schema = {"traits": {}}
    result = run_all_checks(tmp_path, language="python", evaluator_config=cfg)
    api006 = [f for f in result.findings if f.get("rule_id") == "API-006"]
    assert [f.get("model") for f in api006 if f.get("severity") == "WARN"] == ["Event"]
