"""Exemptions that name the paths or routes they cover, API-004's root and
per-module router prefixes, API-006's models file, and API-011's raw-SQL
runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.deterministic.api import (
    check_owner_id_column,
    check_v1_route_prefix,
)
from evaluator_cog.engine.deterministic.delivery import check_migration_in_ci
from evaluator_cog.engine.evaluator_config import (
    EvaluatorConfig,
    NarrowedExemption,
    _parse_evaluator_yaml,
)

_CATALOG = {
    "API-004": {"applies_to": ["api-service"], "modifies": [], "status": "requirement"},
    "API-006": {"applies_to": ["api-service"], "modifies": [], "status": "requirement"},
}


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


_APP = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/internal/tick")
def tick():
    return {}


@app.get("/things")
def things():
    return {}
"""


def _cfg(**kwargs) -> EvaluatorConfig:
    cfg = EvaluatorConfig(repo_type="api-service", **kwargs)
    cfg.rule_catalog = _CATALOG
    cfg.catalog_schema = {"traits": {}}
    return cfg


def _api004(tmp_path: Path, cfg: EvaluatorConfig) -> list[dict]:
    _write(tmp_path, {"src/app/main.py": _APP})
    result = run_all_checks(tmp_path, language="python", evaluator_config=cfg)
    return [f for f in result.findings if f.get("rule_id") == "API-004"]


# ── parsing ─────────────────────────────────────────────────────────────────


def test_entry_with_paths_or_routes_is_narrowed() -> None:
    cfg = _parse_evaluator_yaml(
        {
            "type": "api-service",
            "exemptions": [
                {"rule": "API-004", "reason": "tick", "routes": ["/internal/*"]},
                {"rule": "CD-003", "reason": "cli", "paths": ["scripts/*.py"]},
                {"rule": "DOC-016", "reason": "whole"},
            ],
        }
    )
    assert cfg.exemption_ids == ["DOC-016"]
    assert cfg.narrowed_exemptions["API-004"] == [
        NarrowedExemption(reason="tick", routes=("/internal/*",))
    ]
    assert cfg.narrowed_exemptions["CD-003"][0].paths == ("scripts/*.py",)


def test_entry_with_only_unknown_keys_is_whole_rule() -> None:
    cfg = _parse_evaluator_yaml(
        {
            "type": "api-service",
            "exemptions": [{"rule": "API-004", "reason": "r", "files": ["x.py"]}],
        }
    )
    assert cfg.exemption_ids == ["API-004"]
    assert cfg.narrowed_exemptions == {}


def test_malformed_paths_narrow_to_nothing() -> None:
    cfg = _parse_evaluator_yaml(
        {
            "type": "api-service",
            "exemptions": [{"rule": "API-004", "reason": "r", "paths": []}],
        }
    )
    assert cfg.exemption_ids == []
    assert cfg.narrowed_exemptions["API-004"][0].paths == ()


# ── matching ────────────────────────────────────────────────────────────────


def test_path_globs_stay_within_a_segment() -> None:
    ex = NarrowedExemption(reason="r", paths=("scripts/*.py",))
    assert ex.covers({"file": "scripts/run.py"})
    assert not ex.covers({"file": "scripts/sub/run.py"})
    assert NarrowedExemption(reason="r", paths=("scripts/**",)).covers(
        {"file": "scripts/sub/run.py"}
    )
    assert NarrowedExemption(reason="r", paths=(".github/x.yml",)).covers(
        {"file": ".github/x.yml"}
    )


def test_finding_with_no_file_or_route_is_never_covered() -> None:
    ex = NarrowedExemption(reason="r", paths=("**",), routes=("/**",))
    assert not ex.covers({"rule_id": "API-004", "finding": "src/app/main.py"})


# ── dispatch ────────────────────────────────────────────────────────────────


def test_narrowed_exemption_runs_the_rule() -> None:
    cfg = _cfg(
        narrowed_exemptions={
            "API-004": [NarrowedExemption(reason="r", routes=("/internal/tick",))]
        }
    )
    result = cfg.resolve_dispatch("API-004")
    assert result.should_run
    assert result.narrowed_exemptions
    assert "API-004" not in cfg.all_skipped_ids


def test_route_exemption_drops_only_the_matching_finding(tmp_path: Path) -> None:
    cfg = _cfg(
        narrowed_exemptions={
            "API-004": [
                NarrowedExemption(reason="cron tick", routes=("/internal/tick",))
            ]
        }
    )
    findings = _api004(tmp_path, cfg)
    by_route = {f["route"]: f for f in findings}
    assert set(by_route) == {"/internal/tick", "/things"}
    tick = by_route["/internal/tick"]
    assert tick["severity"] == "INFO"
    assert tick["exempted"] is True
    assert "cron tick" in tick["finding"]
    assert by_route["/things"]["severity"] == "ERROR"
    assert "exempted" not in by_route["/things"]


def test_path_exemption_covers_findings_in_that_file(tmp_path: Path) -> None:
    cfg = _cfg(
        narrowed_exemptions={
            "API-004": [NarrowedExemption(reason="legacy", paths=("src/app/*.py",))]
        }
    )
    findings = _api004(tmp_path, cfg)
    assert findings
    assert all(f["severity"] == "INFO" for f in findings)


def test_no_paths_or_routes_leaves_findings_unchanged(tmp_path: Path) -> None:
    findings = _api004(tmp_path, _cfg())
    assert {f["severity"] for f in findings} == {"ERROR"}
    assert not any("exempted" in f for f in findings)


def test_whole_rule_exemption_still_skips(tmp_path: Path) -> None:
    cfg = _cfg(exemption_ids=["API-004"], exemption_reasons={"API-004": "r"})
    assert _api004(tmp_path, cfg) == []


# ── API-004: the root ───────────────────────────────────────────────────────


def test_api004_never_flags_the_root(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "src/app/main.py": (
                "from fastapi import FastAPI\napp = FastAPI()\n\n"
                '@app.get("/")\ndef root():\n    return {}\n\n'
                '@app.get("/other")\ndef other():\n    return {}\n'
            ),
            "src/app/index.ts": "app.get('/', (c) => c.redirect('/docs'))\n",
        },
    )
    py = check_v1_route_prefix(tmp_path, language="python")
    assert [f["route"] for f in py] == ["/other"]
    assert check_v1_route_prefix(tmp_path, language="typescript") == []


# ── API-004: router prefixes resolve per module ─────────────────────────────

# api-deejaytools' shape: every router module names its router `router`, the
# substantive ones carry an APIRouter(prefix="/v1/..."), and main.py mounts
# them all with a bare include_router — so one unprefixed module's routes
# must not borrow another module's prefix.
_PER_MODULE_ROUTERS = {
    "src/app/__init__.py": "",
    "src/app/routers/__init__.py": "",
    "src/app/routers/events.py": (
        "from fastapi import APIRouter\n"
        'router = APIRouter(prefix="/v1/events", tags=["events"])\n\n'
        '@router.get("/{event_id}")\ndef get_event(event_id: str):\n    return {}\n'
    ),
    "src/app/routers/internal.py": (
        "from fastapi import APIRouter\n"
        'router = APIRouter(tags=["internal"])\n\n'
        '@router.get("/internal/tick")\ndef tick():\n    return {}\n'
    ),
    "src/app/main.py": (
        "from fastapi import FastAPI\n"
        "from .routers import events, internal\n\n"
        "app = FastAPI()\n"
        "routers = (internal.router, events.router)\n"
        "for router in routers:\n"
        "    app.include_router(router)\n"
    ),
}


def test_api004_router_prefix_is_not_borrowed_across_modules(tmp_path: Path) -> None:
    _write(tmp_path, _PER_MODULE_ROUTERS)
    findings = check_v1_route_prefix(tmp_path, language="python")
    assert [(f["route"], f["file"]) for f in findings] == [
        ("/internal/tick", "src/app/routers/internal.py")
    ]


def test_api004_unprefixed_module_route_is_covered_by_route_exemption(
    tmp_path: Path,
) -> None:
    _write(tmp_path, _PER_MODULE_ROUTERS)
    cfg = _cfg(
        narrowed_exemptions={
            "API-004": [
                NarrowedExemption(reason="stable tick", routes=("/internal/tick",))
            ]
        }
    )
    result = run_all_checks(tmp_path, language="python", evaluator_config=cfg)
    api004 = [f for f in result.findings if f.get("rule_id") == "API-004"]
    assert [(f["route"], f["severity"], f.get("exempted")) for f in api004] == [
        ("/internal/tick", "INFO", True)
    ]


# api-kaianolevine-com's shape: unprefixed `router`s, each mounted under /v1
# by include_router(<module>.router, prefix="/v1").
@pytest.mark.parametrize(
    "import_line, sets_ref",
    [
        ("from .routers import sets, tasks", "sets.router"),
        ("from app.routers import sets, tasks", "sets.router"),
        ("import app.routers.sets as sets", "sets.router"),
        ("from .routers.sets import router as sets_router", "sets_router"),
    ],
)
def test_api004_include_prefix_applies_only_to_the_included_module(
    tmp_path: Path, import_line: str, sets_ref: str
) -> None:
    route = '@router.get("/{name}")\ndef handler(name: str):\n    return {}\n'
    _write(
        tmp_path,
        {
            "src/app/__init__.py": "",
            "src/app/routers/__init__.py": "",
            "src/app/routers/sets.py": (
                "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
                + route.replace("{name}", "sets")
            ),
            "src/app/routers/tasks.py": (
                "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
                + route.replace("{name}", "tasks")
            ),
            "src/app/main.py": (
                f"from fastapi import FastAPI\n{import_line}\n\n"
                "app = FastAPI()\n"
                f'app.include_router({sets_ref}, prefix="/v1")\n'
            ),
        },
    )
    findings = check_v1_route_prefix(tmp_path, language="python")
    assert [f["route"] for f in findings] == ["/tasks"]


def test_api004_include_prefix_for_a_local_router(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "src/app/main.py": (
                "from fastapi import APIRouter, FastAPI\n"
                "app = FastAPI()\nrouter = APIRouter()\n\n"
                '@router.get("/things")\ndef things():\n    return {}\n\n'
                'app.include_router(router, prefix="/v1")\n'
            ),
        },
    )
    assert check_v1_route_prefix(tmp_path, language="python") == []


# ── API-006: findings carry the models file ─────────────────────────────────

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


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[str] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column()
"""


def test_api006_reports_one_finding_per_model_with_its_file(tmp_path: Path) -> None:
    _write(tmp_path, {"src/app/models.py": _MODELS})
    findings = check_owner_id_column(tmp_path)
    assert [(f["finding"].split(":")[2], f["file"]) for f in findings] == [
        ("Event", "src/app/models.py"),
        ("Run", "src/app/models.py"),
    ]


def test_api006_path_exemption_covers_the_models_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "src/app/models.py": _MODELS,
            "src/app/other_models.py": (
                "from .models import Base\n\n\n"
                "class Tally(Base):\n"
                '    __tablename__ = "tallies"\n'
            ),
        },
    )
    cfg = _cfg(
        narrowed_exemptions={
            "API-006": [
                NarrowedExemption(reason="shared data", paths=("src/app/models.py",))
            ]
        }
    )
    result = run_all_checks(tmp_path, language="python", evaluator_config=cfg)
    api006 = [f for f in result.findings if f.get("rule_id") == "API-006"]
    by_file: dict[str, set[str]] = {}
    for f in api006:
        by_file.setdefault(f["file"], set()).add(f["severity"])
    assert by_file == {
        "src/app/models.py": {"INFO"},
        "src/app/other_models.py": {"WARN"},
    }


# ── API-011: the raw-SQL runner ─────────────────────────────────────────────

_RUNNER = 'from pathlib import Path\nMIGRATIONS = Path("migrations")\n'


def _raw_sql_repo(tmp_path: Path, **overrides: str) -> Path:
    files = {
        "migrations/001_baseline.sql": "create table t (id int);\n",
        "scripts/apply_migrations.py": _RUNNER,
        "railway.json": (
            '{"deploy": {"startCommand": '
            '"python scripts/apply_migrations.py && uvicorn app.main:app"}}'
        ),
    }
    files.update(overrides)
    return _write(tmp_path, files)


def test_api011_accepts_the_raw_sql_runner(tmp_path: Path) -> None:
    repo = _raw_sql_repo(tmp_path)
    assert check_migration_in_ci(repo, language="python") == []
    assert check_migration_in_ci(repo, language="typescript") == []


def test_api011_accepts_the_runner_from_the_dockerfile(tmp_path: Path) -> None:
    repo = _raw_sql_repo(
        tmp_path,
        **{
            "railway.json": "{}",
            "Dockerfile": (
                "FROM python:3.12\n"
                'CMD ["/bin/sh", "-c", "python scripts/apply_migrations.py '
                '&& uvicorn app.main:app"]\n'
            ),
        },
    )
    assert check_migration_in_ci(repo) == []


def test_api011_runner_must_be_chained_before_the_server(tmp_path: Path) -> None:
    repo = _raw_sql_repo(
        tmp_path,
        **{
            "railway.json": (
                '{"deploy": {"startCommand": "python scripts/apply_migrations.py"}}'
            )
        },
    )
    assert [f["rule_id"] for f in check_migration_in_ci(repo)] == ["API-011"]


def test_api011_needs_numbered_sql_files(tmp_path: Path) -> None:
    repo = _raw_sql_repo(tmp_path)
    (repo / "migrations" / "001_baseline.sql").rename(
        repo / "migrations" / "baseline.sql"
    )
    assert [f["rule_id"] for f in check_migration_in_ci(repo)] == ["API-011"]


def test_api011_script_must_name_migrations(tmp_path: Path) -> None:
    repo = _raw_sql_repo(
        tmp_path, **{"scripts/apply_migrations.py": "print('hello')\n"}
    )
    assert [f["rule_id"] for f in check_migration_in_ci(repo)] == ["API-011"]
