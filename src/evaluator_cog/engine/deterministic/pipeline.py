"""Prefect / pipeline rule checks (flow presence, observability, retry, serve pattern)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
    _is_checker_self_source,
    _is_inside_string_literal,
    production_python_text,
)


def check_healthchecks_integration(
    repo_path: Path,
    cog_subtype: str | None = None,
) -> list[Finding]:
    """CD-007: Healthchecks.io for trigger cogs."""
    CHECK_ID = "CD-007"
    findings = []
    if cog_subtype != "trigger":
        return findings
    env_example = repo_path / ".env.example"
    env_text = env_example.read_text() if env_example.exists() else ""
    src_text = production_python_text(repo_path) if (repo_path / "src").is_dir() else ""
    if "HEALTHCHECKS_URL_" not in env_text or (
        "HEALTHCHECKS_URL_" not in src_text and "healthchecks" not in src_text.lower()
    ):
        findings.append(
            _finding(
                "CD-007",
                "WARN",
                "cd_readiness",
                "Trigger cog is missing Healthchecks.io integration signals.",
                "Declare HEALTHCHECKS_URL_<SERVICE> in .env.example and ping it in trigger loop code.",
            )
        )
    return findings


def _pyproject_and_requirements_text(repo_path: Path) -> str:
    parts: list[str] = []
    py = repo_path / "pyproject.toml"
    if py.exists():
        parts.append(py.read_text().lower())
    for rel in ("requirements.txt", "requirements/base.txt"):
        p = repo_path / rel
        if p.exists():
            parts.append(p.read_text().lower())
    return "\n".join(parts)


def check_prefect_present(
    repo_path: Path,
    cog_subtype: str = "pipeline",
) -> list[Finding]:
    """PIPE-001: Prefect dependency and usage signals.

    Pipeline cogs must declare ``prefect`` and use ``@flow`` in Python sources.
    Trigger cogs must declare ``prefect`` and call into deployment APIs such as
    ``run_deployment``.

    If ``prefect`` is not declared, emit only the dependency finding — do not
    also flag missing usage (the dependency gap is the root cause).
    """
    CHECK_ID = "PIPE-001"
    findings: list[Finding] = []
    blob = _pyproject_and_requirements_text(repo_path)
    if "prefect" not in blob:
        findings.append(
            _finding(
                "PIPE-001",
                "WARN",
                "pipeline_consistency",
                "Prefect is not declared as a dependency.",
                "Add prefect to pyproject.toml (or requirements.txt) for orchestrated flows.",
            )
        )
        return findings

    src = repo_path / "src"
    if not src.is_dir():
        findings.append(
            _finding(
                "PIPE-001",
                "WARN",
                "pipeline_consistency",
                "src/ tree missing — cannot verify Prefect usage in application code.",
                "Add a src/ package with flow entrypoints.",
            )
        )
        return findings

    py_src = production_python_text(repo_path)
    if cog_subtype == "trigger":
        # Trigger cogs satisfy PIPE-001 by using any of the three standard
        # Prefect client patterns for creating flow runs. The rule's
        # check_notes explicitly lists `run_deployment` OR PrefectClient —
        # this signal list is the implementation of that OR.
        trigger_signals = (
            "run_deployment",  # prefect.deployments.run_deployment helper
            "create_flow_run_from_deployment",  # PrefectClient method
            "get_client(",  # explicit PrefectClient pattern
        )
        if not any(signal in py_src for signal in trigger_signals):
            findings.append(
                _finding(
                    "PIPE-001",
                    "WARN",
                    "pipeline_consistency",
                    (
                        "Trigger cog source does not reference Prefect's "
                        "flow-run creation API (run_deployment, PrefectClient "
                        "get_client(), or create_flow_run_from_deployment)."
                    ),
                    (
                        "Use Prefect's Python client to trigger downstream "
                        "deployments from the watcher/trigger cog — any of "
                        "prefect.deployments.run_deployment(), "
                        "prefect.get_client() + "
                        "create_flow_run_from_deployment(), or equivalent."
                    ),
                )
            )
    elif "@flow" not in py_src:
        findings.append(
            _finding(
                "PIPE-001",
                "WARN",
                "pipeline_consistency",
                "Pipeline cog source has no @flow-decorated Prefect flow.",
                "Define orchestration entrypoints with @flow and register them from the cog main module.",
            )
        )
    return findings


def check_prefect_cloud_observability(
    repo_path: Path,
    cog_subtype: str = "pipeline",
) -> list[Finding]:
    """CD-005: Prefect Cloud wiring is documented for orchestrated cogs.

    When ``prefect`` is declared as a dependency, ``.env.example`` should
    document how the process reaches Prefect Cloud (``PREFECT_API_URL`` or
    equivalent). If Prefect is not a declared dependency, return no findings —
    PIPE-001 already covers the missing-dependency case.

    Competing schedulers referenced from application source (for example
    APScheduler) are surfaced at INFO with a manual review prompt — a
    deterministic scan cannot tell dev-only fallback from primary scheduling.

    GitHub Actions workflow scanning for competing orchestrators is owned by
    CD-006 (future wave) to avoid double-reporting once that check lands.
    """
    CHECK_ID = "CD-005"
    findings: list[Finding] = []
    blob = _pyproject_and_requirements_text(repo_path)
    if "prefect" not in blob:
        return findings

    env_example = repo_path / ".env.example"
    env_text = env_example.read_text() if env_example.exists() else ""
    lowered = env_text.lower()
    if not any(
        token in lowered
        for token in (
            "prefect_api_url",
            "prefect_api_key",
            "api.prefect.cloud",
            "prefect_cloud",
        )
    ):
        findings.append(
            _finding(
                "CD-005",
                "WARN",
                "cd_readiness",
                "Prefect Cloud connection is not documented in .env.example (expected PREFECT_API_URL or equivalent).",
                "Document Prefect Cloud API URL / workspace auth vars for operators.",
            )
        )

    src = repo_path / "src"
    if src.is_dir():
        py_src = production_python_text(repo_path)
        if (
            "apscheduler" in py_src.lower()
            and not _is_inside_string_literal(py_src, "APScheduler")
            and not _is_inside_string_literal(py_src, "apscheduler")
        ):
            findings.append(
                _finding(
                    "CD-005",
                    "INFO",
                    "cd_readiness",
                    "APScheduler is referenced — confirm it is only a local dev fallback, not the primary scheduler.",
                    "Primary orchestration should remain Prefect Cloud; document intentional APScheduler use if applicable.",
                )
            )
    return findings


def check_shared_resource_concurrency(repo_path: Path) -> list[Finding]:
    """PIPE-004: Flows writing shared resources use concurrency guards."""
    CHECK_ID = "PIPE-004"

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        if _is_checker_self_source(py_file):
            continue
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_flow = any(
                (isinstance(d, ast.Name) and d.id == "flow")
                or (
                    isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Name)
                    and d.func.id == "flow"
                )
                or (isinstance(d, ast.Attribute) and d.attr == "flow")
                for d in node.decorator_list
            )
            if not is_flow:
                continue
            body_src = ast.unparse(node)
            writes_shared_resource = any(
                m in body_src
                for m in [
                    "session.commit()",
                    "session.add(",
                    "drive_service.files().move",
                    "drive_service.files().update",
                    ".post(",
                    ".patch(",
                ]
            )
            if not writes_shared_resource:
                continue
            has_concurrency_param = False
            for d in node.decorator_list:
                if isinstance(d, ast.Call):
                    for kw in d.keywords:
                        if kw.arg == "concurrency_limit":
                            has_concurrency_param = True
            has_concurrency_block = (
                "with concurrency(" in body_src or "concurrency.sync" in body_src
            )
            if not (has_concurrency_param or has_concurrency_block):
                findings.append(
                    _finding(
                        "PIPE-004",
                        "ERROR",
                        "pipeline_consistency",
                        f"{rel}::{node.name}: flow writes to shared resource without concurrency guard.",
                        "Add concurrency_limit= on the @flow decorator, or wrap the write "
                        "block with a 'with concurrency(...)' slot from prefect.concurrency.sync.",
                    )
                )
    return findings


def check_prefect_run_logger(repo_path: Path) -> list[Finding]:
    """PIPE-006: Prefect flows use get_run_logger() with fallback.

    Accepts two equivalent patterns:

      1. Direct: the flow body calls get_run_logger() itself.
      2. Wrapper: the flow body calls a function whose name contains
         'logger' and whose body (defined in this repo) calls
         get_run_logger().

    The wrapper pattern is common — it centralizes the
    fallback-to-stdlib behavior in one place.
    """

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    # First pass: collect repo-local logger-wrapper functions —
    # functions whose name contains "logger" and whose body calls
    # get_run_logger.
    wrapper_names: set[str] = set()
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "logger" not in node.name.lower():
                continue
            try:
                body_src = ast.unparse(node)
            except Exception:
                continue
            if "get_run_logger" in body_src:
                wrapper_names.add(node.name)

    # Second pass: check each @flow-decorated function.
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_flow = any(
                (isinstance(d, ast.Name) and d.id == "flow")
                or (
                    isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Name)
                    and d.func.id == "flow"
                )
                or (isinstance(d, ast.Attribute) and d.attr == "flow")
                for d in node.decorator_list
            )
            if not is_flow:
                continue
            try:
                body_src = ast.unparse(node)
            except Exception:
                continue
            if "get_run_logger" in body_src:
                continue
            if any(name in body_src for name in wrapper_names):
                continue
            findings.append(
                _finding(
                    "PIPE-006",
                    "WARN",
                    "pipeline_consistency",
                    f"{rel}::{node.name}: flow does not call get_run_logger() "
                    f"(directly or via a logger-wrapper defined in this repo).",
                    "Use get_run_logger() inside flows for Prefect-integrated logging; "
                    "fall back to stdlib logging outside Prefect context. A wrapper "
                    "whose name contains 'logger' and which internally calls "
                    "get_run_logger() is also accepted.",
                )
            )
    return findings


def check_final_evaluation_task(
    repo_path: Path, cog_subtype: str | None = None
) -> list[Finding]:
    """PIPE-011: Pipeline cogs end with an AI evaluation task.

    Exempt: trigger-cogs (they fire flow runs, don't run pipelines),
    and evaluator-cog itself.
    """
    CHECK_ID = "PIPE-011"
    findings: list[Finding] = []
    if cog_subtype == "trigger":
        return findings
    # Check if this is evaluator-cog itself
    if (repo_path / "src" / "evaluator_cog").is_dir():
        return findings
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    # NOTE: ``/v1/pipeline_evaluations`` is intentionally absent. That
    # path was a documentation bug — the canonical route is
    # ``/v1/evaluations`` (router prefix ``/v1`` + in-router path
    # ``/evaluations``); ``pipeline_evaluations`` is the underlying SQL
    # table name. Cogs still on the wrong path 404 silently and produce
    # no pipeline-health rows, so PIPE-011 must NOT treat the wrong
    # path as evidence that an evaluation task is wired up.
    evaluation_markers = (
        "pipeline_eval",
        "evaluation_client",
        "/v1/evaluations",
        "PipelineEvaluator",
    )
    found_marker = False
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
        except Exception:
            continue
        if any(m in text for m in evaluation_markers):
            found_marker = True
            break
    if not found_marker:
        findings.append(
            _finding(
                "PIPE-011",
                "WARN",
                "pipeline_consistency",
                "No AI evaluation task found in pipeline-cog source.",
                "Add a final task that writes to pipeline_evaluations (via the evaluation "
                "client from common-python-utils) so quality can be tracked.",
            )
        )
    return findings


def check_hardcoded_retry_delay(repo_path: Path) -> list[Finding]:
    """PIPE-012: retry_delay_seconds not hardcoded to non-zero."""
    CHECK_ID = "PIPE-012"

    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
            tree = ast.parse(text)
        except Exception:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "retry_delay_seconds":
                    continue
                # Must be a hardcoded non-zero numeric literal
                if (
                    isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, (int, float))
                    and kw.value.value != 0
                ):
                    # Check for PYTEST_CURRENT_TEST guard nearby
                    py_text = py_file.read_text()
                    if "PYTEST_CURRENT_TEST" not in py_text:
                        findings.append(
                            _finding(
                                "PIPE-012",
                                "WARN",
                                "pipeline_consistency",
                                f"{rel}: retry_delay_seconds={kw.value.value} hardcoded without PYTEST_CURRENT_TEST guard.",
                                "Source from Settings field or wrap with os.getenv('PYTEST_CURRENT_TEST') "
                                "conditional so tests don't sleep.",
                            )
                        )
    return findings


def check_retry_logic(repo_path: Path) -> list[Finding]:
    """PIPE-007: Retry logic on external API calls."""
    CHECK_ID = "PIPE-007"

    findings = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in src.rglob("*.py"):
        text = py_file.read_text()
        if "DriveFacade" in text or "LLMClient" in text:
            continue
        try:
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            task_decorator = None
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Name) and dec.id == "task") or (
                    isinstance(dec, ast.Call)
                    and (
                        (isinstance(dec.func, ast.Name) and dec.func.id == "task")
                        or (
                            isinstance(dec.func, ast.Attribute)
                            and dec.func.attr == "task"
                        )
                    )
                ):
                    task_decorator = dec
            if task_decorator is None:
                continue

            body_text = ast.get_source_segment(text, node) or ""
            external_signal = any(
                s in body_text for s in ("httpx.", "anthropic", "drive", "sheets")
            )
            if not external_signal:
                continue

            has_retries = isinstance(task_decorator, ast.Call) and any(
                kw.arg == "retries" for kw in task_decorator.keywords
            )
            if not has_retries:
                findings.append(
                    _finding(
                        "PIPE-007",
                        "WARN",
                        "pipeline_consistency",
                        f"External-calling task missing retries= in {py_file.relative_to(repo_path)}::{node.name}.",
                        "Add retries= to @task decorators that call external APIs.",
                    )
                )
    return findings


def check_no_retired_trigger_patterns(repo_path: Path) -> list[Finding]:
    """PIPE-008: Narrowed retired GitHub / GAS / gh CLI trigger patterns (2026-04).

    Fires only when: (1) a workflow uses ``repository_dispatch`` together with
    app-invoking steps, (2) Python/JS source actively POSTs to GitHub
    ``/dispatches``, (3) the retired ``google-app-script-trigger`` string appears,
    or (4) ``gh workflow run`` is invoked (shell or argv list form). Bare URL
    literals are intentionally ignored — see LLM rule PIPE-014 for consistency
    reasoning across input types.
    """
    CHECK_ID = "PIPE-008"
    findings: list[Finding] = []
    wf_dir = repo_path / ".github" / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.rglob("*.yml")) + sorted(wf_dir.rglob("*.yaml")):
            try:
                text = wf.read_text()
            except OSError:
                continue
            low = text.lower()
            if "repository_dispatch" not in low:
                continue
            relay = any(
                k in low
                for k in (
                    "prefect deployment run",
                    "prefect deploy",
                    "run_deployment(",
                    "npx prefect",
                )
            ) or (
                "/dispatches" in text
                and any(k in low for k in ("curl ", "httpx.", "requests."))
            )
            pure_ci = ("pytest" in low or "ruff" in low) and not relay
            if relay and not pure_ci:
                findings.append(
                    _finding(
                        "PIPE-008",
                        "WARN",
                        "structural_conformance",
                        f"repository_dispatch in GitHub workflow with app-triggering steps ({wf.relative_to(repo_path)}).",
                        "Use watcher-cog + Prefect instead of GHA repository_dispatch relays.",
                    )
                )

    code_exts = {".py", ".ts", ".tsx", ".js"}
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        if "tests/" in str(path).replace("\\", "/"):
            continue
        if path.suffix.lower() not in code_exts:
            continue
        if ".github/workflows/" in str(path).replace("\\", "/"):
            continue
        try:
            body = path.read_text()
        except OSError:
            continue
        low = body.lower()
        if re.search(
            r"(httpx|requests)\.(post|put)\([^\)]*api\.github\.com/[^\"'\)]+/dispatches",
            body,
            re.I,
        ):
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"Active HTTP client call to GitHub dispatches API ({path.relative_to(repo_path)}).",
                    "Use watcher-cog + Prefect instead of repository_dispatch HTTP relays.",
                )
            )
        if "google-app-script-trigger" in body:
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"Retired google-app-script-trigger reference in {path.relative_to(repo_path)}.",
                    "Use watcher-cog + Prefect; remove legacy Apps Script trigger hooks.",
                )
            )
        if re.search(r"\bgh\s+workflow\s+run\b", low):
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"gh workflow run invocation in {path.relative_to(repo_path)}.",
                    "Use watcher-cog + Prefect instead of driving workflows via gh CLI.",
                )
            )
        if re.search(
            r"\[\s*['\"]gh['\"]\s*,\s*['\"]workflow['\"]\s*,\s*['\"]run['\"]",
            body,
        ):
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"gh workflow run argv-style invocation in {path.relative_to(repo_path)}.",
                    "Use watcher-cog + Prefect instead of subprocess gh workflow relays.",
                )
            )
    return findings


def check_evaluation_step(repo_path: Path) -> list[Finding]:
    """PIPE-009: AI evaluation step as final pipeline task."""
    CHECK_ID = "PIPE-009"
    findings = []
    if repo_path.name == "evaluator-cog":
        return findings
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    text = production_python_text(repo_path)
    signals = (
        "pipeline_eval",
        "post_evaluation",
        "/v1/evaluations",
        "evaluate_pipeline_run",
    )
    if not any(s in text for s in signals):
        findings.append(
            _finding(
                "PIPE-009",
                "WARN",
                "pipeline_consistency",
                "Pipeline cog source has no clear evaluation-step signal.",
                "Add or document an evaluation step that posts findings to /v1/evaluations.",
            )
        )
    return findings


def check_prefect_serve_pattern(repo_path: Path) -> list[Finding]:
    """CD-015: Prefect serve() — no work pool.

    Detects three equivalent serve() call shapes:

      1. prefect.serve(...)
      2. flow.serve(...)
      3. ``from prefect import serve`` followed by ``serve(...)``

    Also flags incompatible patterns: flow.deploy(), work_pool_name
    references, and work_pool: in prefect.yaml.
    """
    findings = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    content = "\n".join(
        f.read_text(errors="replace")
        for f in src.rglob("*.py")
        if not _is_checker_self_source(f)
    )

    # Incompatible patterns.
    if "flow.deploy(" in content or "work_pool_name" in content:
        findings.append(
            _finding(
                "CD-015",
                "ERROR",
                "cd_readiness",
                "work pool pattern detected — flow.deploy() or work_pool_name found.",
                "Use prefect.serve() running in-process on Railway instead of work pool deployments.",
            )
        )
    prefect_yaml = repo_path / "prefect.yaml"
    if prefect_yaml.exists() and "work_pool" in prefect_yaml.read_text():
        findings.append(
            _finding(
                "CD-015",
                "ERROR",
                "cd_readiness",
                "work_pool configuration found in prefect.yaml.",
                "Remove work pool config and use prefect.serve() instead.",
            )
        )

    # Accepted serve patterns.
    has_qualified_serve = "prefect.serve(" in content or "flow.serve(" in content
    # ``from prefect import serve`` (optionally with other names) followed
    # anywhere by a bare ``serve(`` call.
    imports_serve = bool(
        re.search(
            r"from\s+prefect\s+import\s+[^\n]*\bserve\b",
            content,
        )
    )
    has_bare_serve_call = bool(re.search(r"(?:^|[\s(=,])serve\s*\(", content))
    has_imported_serve = imports_serve and has_bare_serve_call

    if not (has_qualified_serve or has_imported_serve):
        findings.append(
            _finding(
                "CD-015",
                "WARN",
                "cd_readiness",
                "No prefect.serve() call found in source — flow registration "
                "pattern missing or unverifiable.",
                "Ensure flows are registered via prefect.serve() (or "
                "`from prefect import serve; serve(...)`) at the cog entry point.",
            )
        )
    return findings
