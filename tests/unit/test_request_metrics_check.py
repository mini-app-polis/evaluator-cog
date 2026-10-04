"""CD-036: APIs register the shared request-metrics middleware.

Each case is a throwaway repo under ``tmp_path``. The factory case is a
trimmed copy of api-kaianolevine-com's ``main.py``, the only API the rule
passes today — if the check stops recognising that shape, it starts
failing the one repo that complies.
"""

from __future__ import annotations

from pathlib import Path

from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.deterministic.delivery import (
    check_request_metrics_middleware,
)


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _cd036(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["rule_id"] == "CD-036"]


def _text(findings: list[dict]) -> str:
    return " || ".join(f["finding"] for f in findings)


# --- passes -------------------------------------------------------------------


def test_module_level_app_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "from mini_app_polis.request_metrics import RequestMetricsMiddleware\n"
        "\n"
        "app = FastAPI()\n"
        'app.add_middleware(RequestMetricsMiddleware, service="svc")\n',
    )
    assert check_request_metrics_middleware(tmp_path) == []


def test_app_factory_passes(tmp_path: Path) -> None:
    """api-kaianolevine-com's shape: the app is built inside a function."""
    _write(
        tmp_path,
        "src/kaianolevine_api/main.py",
        "from fastapi import FastAPI\n"
        "from fastapi.middleware.cors import CORSMiddleware\n"
        "from mini_app_polis.request_metrics import RequestMetricsMiddleware\n"
        "\n"
        "from . import activity\n"
        "\n"
        "\n"
        "def _build_app() -> FastAPI:\n"
        '    app = FastAPI(title="kaianolevine-api")\n'
        "    app.add_middleware(CORSMiddleware, allow_origins=['*'])\n"
        '    app.middleware("http")(activity.activity_middleware)\n'
        "    app.add_middleware(\n"
        "        RequestMetricsMiddleware,\n"
        '        service="api-kaianolevine-com",\n'
        '        exclude_paths=["/health", "/version"],\n'
        "    )\n"
        "    return app\n"
        "\n"
        "\n"
        "app = _build_app()\n",
    )
    assert check_request_metrics_middleware(tmp_path) == []


def test_top_level_package_import_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "from mini_app_polis import RequestMetricsMiddleware\n"
        "app = FastAPI()\n"
        "app.add_middleware(RequestMetricsMiddleware)\n",
    )
    assert check_request_metrics_middleware(tmp_path) == []


def test_aliased_class_import_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "from mini_app_polis.request_metrics import RequestMetricsMiddleware as RMM\n"
        "app = FastAPI()\n"
        "app.add_middleware(RMM)\n",
    )
    assert check_request_metrics_middleware(tmp_path) == []


def test_module_attribute_reference_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "import fastapi\n"
        "import mini_app_polis.request_metrics as rm\n"
        "app = fastapi.FastAPI()\n"
        "app.add_middleware(rm.RequestMetricsMiddleware)\n",
    )
    assert check_request_metrics_middleware(tmp_path) == []


def test_unaliased_dotted_import_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "import mini_app_polis.request_metrics\n"
        "app = FastAPI()\n"
        "app.add_middleware(mini_app_polis.request_metrics.RequestMetricsMiddleware)\n",
    )
    assert check_request_metrics_middleware(tmp_path) == []


def test_constructor_middleware_list_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "from starlette.middleware import Middleware\n"
        "from mini_app_polis.request_metrics import RequestMetricsMiddleware\n"
        "app = FastAPI(middleware=[Middleware(RequestMetricsMiddleware, service='svc')])\n",
    )
    assert check_request_metrics_middleware(tmp_path) == []


def test_starlette_app_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from starlette.applications import Starlette\n"
        "from mini_app_polis.request_metrics import RequestMetricsMiddleware\n"
        "app = Starlette()\n"
        "app.add_middleware(RequestMetricsMiddleware)\n",
    )
    assert check_request_metrics_middleware(tmp_path) == []


def test_same_named_class_in_tests_is_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "from mini_app_polis.request_metrics import RequestMetricsMiddleware\n"
        "app = FastAPI()\n"
        "app.add_middleware(RequestMetricsMiddleware)\n",
    )
    _write(tmp_path, "tests/fakes.py", "class RequestMetricsMiddleware:\n    pass\n")
    assert check_request_metrics_middleware(tmp_path) == []


# --- failures -----------------------------------------------------------------


def test_app_without_registration_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\napp = FastAPI()\n",
    )
    findings = check_request_metrics_middleware(tmp_path)
    assert len(_cd036(findings)) == 1
    assert "does not register RequestMetricsMiddleware" in _text(findings)
    assert "src/svc/main.py" in _text(findings)


def test_imported_but_never_registered_fails(tmp_path: Path) -> None:
    """The import alone is what a substring scan would have passed."""
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "from mini_app_polis.request_metrics import RequestMetricsMiddleware  # noqa\n"
        "app = FastAPI()\n",
    )
    assert len(_cd036(check_request_metrics_middleware(tmp_path))) == 1


def test_name_only_in_comment_or_string_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "# app.add_middleware(RequestMetricsMiddleware)\n"
        'NOTE = "app.add_middleware(RequestMetricsMiddleware)"\n',
    )
    assert len(_cd036(check_request_metrics_middleware(tmp_path))) == 1


def test_middleware_from_a_local_module_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "from svc.observability import RequestMetricsMiddleware\n"
        "app = FastAPI()\n"
        "app.add_middleware(RequestMetricsMiddleware)\n",
    )
    findings = check_request_metrics_middleware(tmp_path)
    assert len(_cd036(findings)) == 1
    assert "not the shared one" in _text(findings)
    assert "svc.observability.RequestMetricsMiddleware" in _text(findings)


def test_local_copy_under_src_fails_even_beside_the_shared_import(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "from mini_app_polis.request_metrics import RequestMetricsMiddleware\n"
        "app = FastAPI()\n"
        "app.add_middleware(RequestMetricsMiddleware)\n",
    )
    _write(
        tmp_path,
        "src/svc/legacy_metrics.py",
        "class RequestMetricsMiddleware:\n    pass\n",
    )
    findings = check_request_metrics_middleware(tmp_path)
    assert len(_cd036(findings)) == 1
    assert "src/svc/legacy_metrics.py" in _text(findings)


def test_registration_outside_the_app_module_does_not_count(
    tmp_path: Path,
) -> None:
    """A registration only counts in a module that builds an app."""
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\napp = FastAPI()\n",
    )
    _write(
        tmp_path,
        "src/svc/helpers.py",
        "from mini_app_polis.request_metrics import RequestMetricsMiddleware\n"
        "def wire(app):\n"
        "    app.add_middleware(RequestMetricsMiddleware)\n",
    )
    assert len(_cd036(check_request_metrics_middleware(tmp_path))) == 1


def test_no_asgi_app_fails_so_a_deferral_has_something_to_defer(
    tmp_path: Path,
) -> None:
    """A TypeScript API: nothing under src/ is Python."""
    _write(tmp_path, "src/index.ts", "import { Hono } from 'hono'\n")
    findings = check_request_metrics_middleware(tmp_path)
    assert len(_cd036(findings)) == 1
    assert "No module under src/ constructs" in _text(findings)
    assert "deferral" in findings[0]["suggestion"]


def test_no_src_dir_fails(tmp_path: Path) -> None:
    assert len(_cd036(check_request_metrics_middleware(tmp_path))) == 1


def test_unparseable_module_is_skipped_not_raised(tmp_path: Path) -> None:
    _write(tmp_path, "src/svc/broken.py", "def (:\n")
    _write(
        tmp_path,
        "src/svc/main.py",
        "from fastapi import FastAPI\n"
        "from mini_app_polis.request_metrics import RequestMetricsMiddleware\n"
        "app = FastAPI()\n"
        "app.add_middleware(RequestMetricsMiddleware)\n",
    )
    assert check_request_metrics_middleware(tmp_path) == []


# --- registration ---------------------------------------------------------------


def test_runner_checks_cd036_for_api_services(tmp_path: Path) -> None:
    """Registered under its catalog ID, so EVAL-007 stops reporting it."""
    _write(
        tmp_path, "src/svc/main.py", "from fastapi import FastAPI\napp = FastAPI()\n"
    )
    result = run_all_checks(tmp_path, dod_type="new_fastapi_service")
    assert "CD-036" in result.checked_rule_ids
    assert _cd036(result.findings)


def test_runner_does_not_check_cd036_for_pipeline_cogs(tmp_path: Path) -> None:
    _write(
        tmp_path, "src/cog/main.py", "def handler(event, context):\n    return None\n"
    )
    result = run_all_checks(tmp_path, dod_type="new_cog", cog_subtype="pipeline")
    assert "CD-036" not in result.checked_rule_ids
