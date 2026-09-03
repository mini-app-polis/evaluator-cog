"""Deterministic dispatcher — run_all_checks and its dedup helper."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    CheckResult,
    Finding,
    _finding,
)
from evaluator_cog.engine.deterministic.api import (
    check_async_sqlalchemy,
    check_cors_config,
    check_db_writes_use_upserts,
    check_fetch_error_handling,
    check_health_endpoint,
    check_inputs_not_deleted,
    check_orm_usage,
    check_owner_id_column,
    check_postgres_only_data_store,
    check_pydantic_for_external_data,
    check_railway_hosted_api,
    check_response_envelope_presence,
    check_response_shape_parity,
    check_v1_route_prefix,
)
from evaluator_cog.engine.deterministic.auth import (
    check_auth_header_parity,
    check_clerk_auth_dep,
    check_unauthenticated_routes,
)
from evaluator_cog.engine.deterministic.config import (
    check_env_example_settings_parity,
    check_env_var_prefix,
    check_hardcoded_standards_version,
    check_hardcoded_time_values,
    check_logger_misuse,
    check_settings_field_consistency,
    check_shared_library_used,
    check_standards_freshness,
)
from evaluator_cog.engine.deterministic.containers import (
    check_cd_017,
    check_cd_021,
    check_cd_022,
    check_cd_023,
    check_cd_024,
)
from evaluator_cog.engine.deterministic.delivery import (
    check_ci,
    check_gha_not_trigger_relay,
    check_migration_in_ci,
    check_no_hardcoded_secrets,
    check_no_hardcoded_urls,
    check_no_print_statements,
    check_pnpm_lockfile,
    check_pytest_coverage_in_ci,
    check_structured_logging,
    check_three_layer_observability,
)
from evaluator_cog.engine.deterministic.docs import (
    check_adrs_present,
    check_changelog,
    check_env_example,
    check_fastapi_route_docs,
    check_public_docstrings,
    check_pydantic_field_descriptions,
    check_readme,
    check_readme_running_locally,
    check_split_package_identity,
    check_unauthenticated_routes_documented,
)
from evaluator_cog.engine.deterministic.frontend import (
    check_astro_build_time_data,
    check_astro_framework,
    check_astro_pinned_versions,
    check_astro_runtime_queries,
    check_cloudflare_pages_deploy,
    check_react_hook_form_zod,
    check_shadcn,
    check_tailwind,
    check_vite_react_ts,
)
from evaluator_cog.engine.deterministic.identity import (
    check_auth_003,
    check_auth_004,
    check_cd_019,
)
from evaluator_cog.engine.deterministic.meta import (
    check_meta_005_check_notes_prefix,
    check_meta_006_prefix_file_correlation,
    check_meta_007_rule_ids_unique,
    check_meta_canonical_enums_are_dicts,
    check_meta_no_scattered_metadata,
    check_meta_release_pipeline_wired,
)
from evaluator_cog.engine.deterministic.operations import (
    check_ops_002,
)
from evaluator_cog.engine.deterministic.packaging import (
    check_cd_016,
    check_cd_020,
)
from evaluator_cog.engine.deterministic.pipeline import (
    check_evaluation_step,
    check_final_evaluation_task,
    check_hardcoded_retry_delay,
    check_healthchecks_integration,
    check_no_retired_trigger_patterns,
    check_prefect_cloud_observability,
    check_prefect_present,
    check_prefect_run_logger,
    check_prefect_serve_pattern,
    check_retry_logic,
    check_shared_resource_concurrency,
)
from evaluator_cog.engine.deterministic.python import (
    check_common_python_utils_dep,
    check_dedup_handling_present,
    check_failed_prefix,
    check_finally_cleanup,
    check_mypy_in_ci,
    check_naming_conventions,
    check_no_setup_py,
    check_pre_commit,
    check_pyproject,
    check_src_layout,
)
from evaluator_cog.engine.deterministic.security import (
    check_sec_001,
    check_sec_002,
    check_sec_003,
    check_sec_004,
    check_sec_005,
    check_sec_006,
)
from evaluator_cog.engine.deterministic.testing import (
    check_db_test_fixtures,
    check_mock_assertions,
    check_pytest_config,
    check_respx_for_http_mocking,
    check_route_contract_tests,
    check_test_gap_critical_paths,
    check_testclient_for_v1_routes,
)
from evaluator_cog.engine.deterministic.versioning import (
    check_breaking_change_footer,
    check_conventional_commits,
    check_no_manual_changelog,
    check_releaserc,
    check_releaserc_assets,
)
from evaluator_cog.engine.evaluator_config import EvaluatorConfig

_SUPERSEDED_BY: dict[str, str] = {
    "CD-002": "CD-010",
    "CD-009": "CD-010",
}


def _deduplicate_same_repo_findings(findings: list[Finding]) -> list[Finding]:
    """
    Remove findings for rules that are superseded by a higher-level rule
    that also fired in the same check run.

    Example: if CD-010 fires, drop any CD-002 and CD-009 findings — they
    are sub-components of CD-010 and generating all three is redundant.
    """
    fired_rule_ids = {str(f.get("rule_id", "")) for f in findings}
    return [
        f
        for f in findings
        if not (
            str(f.get("rule_id", "")) in _SUPERSEDED_BY
            and _SUPERSEDED_BY[str(f.get("rule_id", ""))] in fired_rule_ids
        )
    ]


def _type_to_dod(repo_type: str, language: str = "python") -> str | None:
    """Map new repo type taxonomy back to dod_type string for check_readme_running_locally."""
    mapping = {
        "pipeline-cog": "new_cog",
        "trigger-cog": "new_cog",
        "api-service": "new_fastapi_service"
        if language == "python"
        else "new_hono_service",
        # Libraries have no standardized "running locally" section like cogs — avoid
        # routing through the Python cog README path (uv sync, pytest, etc.).
        "shared-library": None,
        "static-site": "new_frontend_site",
        "react-app": "new_react_app",
        "standards-repo": None,
    }
    return mapping.get(repo_type)


# A single check slower than this is worth naming in the log. Most run
# in well under a tenth of a second; anything at this scale is either
# doing network I/O or scanning quadratically.
SLOW_CHECK_SECONDS = 2.0


def run_all_checks(
    repo_path: Path,
    language: str = "python",
    service_type: str = "worker",
    cog_subtype: str | None = None,
    dod_type: str | None = None,
    check_exceptions: list[str] | None = None,
    exception_reasons: dict[str, str] | None = None,
    monorepo_root: Path | None = None,
    workspace_package_json_text: str | None = None,
    evaluator_config: EvaluatorConfig | None = None,
    rule_catalog: dict[str, dict] | None = None,
    catalog_schema: dict | None = None,
    progress: Callable[[str], None] | None = None,
) -> CheckResult:
    """Run deterministic checks against a repo and return combined findings.

    When evaluator_config is provided (from the repo's evaluator.yaml), it
    takes precedence over the legacy dod_type/service_type/check_exceptions
    parameters for type-based branching and exception scoping.

    `progress` is an optional sink for operational notes — currently, any
    single check that runs longer than SLOW_CHECK_SECONDS. Roughly ninety
    checks run per repo behind one "processing <repo>" log line, so when
    one of them stalls there is nothing to say which. A CD-005 that took
    129 seconds on this repo was invisible for exactly that reason: the
    flow looked hung for nine minutes and the log had nothing between
    "processing evaluator-cog" and the next repo.
    """
    # ── Resolve type-based flags ─────────────────────────────────────────────
    # Prefer evaluator_config (from evaluator.yaml) over legacy dod_type fields.
    if evaluator_config is not None:
        if rule_catalog is not None:
            evaluator_config.rule_catalog = rule_catalog
        if catalog_schema is not None:
            evaluator_config.catalog_schema = catalog_schema
        cfg = evaluator_config
        # Type says "could be Python" (e.g. shared-library); ecosystem language is authoritative.
        is_python = (language == "python") and cfg.is_python_service
        is_library = cfg.is_shared_library
        is_pipeline_cog = cfg.is_pipeline_cog
        is_fastapi = cfg.is_api_service and language == "python"
        # Language-agnostic api-service flag — for rules that apply to both
        # FastAPI (Python) and Hono (TypeScript) API services.
        is_api_service = cfg.is_api_service
        is_frontend = cfg.is_frontend
    else:
        # Legacy path — used during migration when evaluator.yaml is absent
        is_python = language == "python" or dod_type in (
            "new_cog",
            "new_fastapi_service",
        )
        is_library = service_type == "library" or dod_type is None
        is_pipeline_cog = (dod_type == "new_cog" and cog_subtype != "trigger") or (
            is_python and service_type == "worker" and cog_subtype == "pipeline"
        )
        is_fastapi = dod_type == "new_fastapi_service"
        is_api_service = dod_type in ("new_fastapi_service", "new_hono_service")
        is_frontend = dod_type in ("new_frontend_site", "new_react_app")

    # Legacy skip list is still used by a handful of checker functions that
    # accept "exceptions" lists directly.
    _exceptions = (
        evaluator_config.all_skipped_ids
        if evaluator_config is not None
        else frozenset(check_exceptions or [])
    )

    # Also handle cog_subtype trigger for trigger-cog type
    is_trigger_cog = (
        evaluator_config is not None and evaluator_config.is_trigger_cog
    ) or cog_subtype == "trigger"

    # Repo type as the dispatcher resolved it — CD-019 needs it to pick
    # its caller half from its receiver half.
    _repo_type_for_checks = (
        evaluator_config.repo_type
        if evaluator_config is not None
        else {
            "new_cog": "pipeline-cog",
            "new_fastapi_service": "api-service",
            "new_hono_service": "api-service",
            "new_frontend_site": "static-site",
            "new_react_app": "react-app",
        }.get(dod_type or "", "")
    )

    checked_rule_ids: set[str] = set()

    def _mark_checked(*rule_ids: str) -> None:
        checked_rule_ids.update(rule_ids)

    def _run(check_fn, rule_id: str | None = None) -> None:
        if rule_id:
            checked_rule_ids.add(rule_id)
        if not rule_id:
            # Legacy call without a rule_id: just run and collect.
            try:
                findings.extend(check_fn(repo_path))
            except Exception as exc:
                findings.append(
                    _finding(
                        "CHECKER",
                        "WARN",
                        "structural_conformance",
                        f"Check {check_fn.__name__} raised an unexpected error: {exc}",
                        "Investigate the checker itself.",
                    )
                )
            return

        # Guard: never fire a rule whose catalog check_mode is "llm" on the
        # deterministic path. This used to be possible — any rule registered
        # here would fire regardless of its intended routing — producing
        # findings with source="conformance_deterministic" for rules that
        # should only be assessed by engine/llm.py. The rule is still marked
        # as "checked" above (so EVAL-007's coverage math is unchanged) but
        # no check function is invoked here.
        if evaluator_config is not None and evaluator_config.rule_catalog:
            meta = evaluator_config.rule_catalog.get(rule_id) or {}
            if meta.get("check_mode") == "llm":
                return

        if evaluator_config is None:
            # No catalog available → honor explicit legacy exceptions only.
            # Exemptions (trait or repo-level) emit no finding: "this rule
            # does not apply here" is not a signal worth reporting on the
            # dashboard. INFO remains reserved for genuinely noted weak
            # signals from a check that actually ran.
            if rule_id in _exceptions:
                return
            disposition_info_reason = ""
            severity_override: str | None = None
            is_deferred = False
            deferral_expired_on = None
            rule_status = ""
        else:
            result = evaluator_config.resolve_dispatch(rule_id)
            rule_meta = (evaluator_config.rule_catalog or {}).get(rule_id, {})
            rule_status = rule_meta.get("status", "")
            if not result.should_run:
                # SKIP_SCOPE, SKIP_TRAIT_EXEMPT, SKIP_REPO_EXEMPT all fall
                # through here. None of them emit findings — a rule that
                # does not apply to this repo shape, or is exempted by
                # trait/reason, is simply absent from the report.
                return
            disposition_info_reason = result.reason
            severity_override = (
                result.downgraded_severity
                if result.disposition.value == "run_downgraded"
                else None
            )
            is_deferred = result.disposition.value == "run_deferred"
            # A deferral whose `until:` has passed is no longer a
            # deferral — resolve_dispatch has already dropped it out of
            # RUN_DEFERRED — but the finding should say a deadline was
            # missed rather than simply reappearing at full severity
            # with no account of why.
            deferral_expired_on = result.deferral_expired_on

        try:
            _started = time.monotonic()
            new_findings = check_fn(repo_path)
            _elapsed = time.monotonic() - _started
            if progress is not None and _elapsed >= SLOW_CHECK_SECONDS:
                progress(f"{rule_id} took {_elapsed:.1f}s")
            for f in new_findings:
                if is_deferred:
                    f["severity"] = "INFO"
                    f["deferred"] = True
                    if disposition_info_reason and "deferred_reason" not in f:
                        f["deferred_reason"] = disposition_info_reason
                elif severity_override:
                    f["severity"] = severity_override
                    f["downgraded"] = True
                    if disposition_info_reason and "downgrade_reason" not in f:
                        f["downgrade_reason"] = disposition_info_reason
                if deferral_expired_on is not None:
                    f["deferral_expired"] = True
                    f["deferral_expired_on"] = deferral_expired_on.isoformat()
                    f["finding"] = (
                        f"{f.get('finding', '')} "
                        f"(deferral expired {deferral_expired_on.isoformat()} — "
                        f"this finding is no longer deferred)"
                    ).strip()
                if rule_status and "status" not in f:
                    f["status"] = rule_status
            findings.extend(new_findings)
        except Exception as exc:
            findings.append(
                _finding(
                    "CHECKER",
                    "WARN",
                    "structural_conformance",
                    f"Check {check_fn.__name__} raised an unexpected error: {exc}",
                    "Investigate the checker itself.",
                )
            )

    findings: list[Finding] = []

    _run(lambda p: check_readme(p, monorepo_root=monorepo_root), "DOC-001")
    _run(lambda p: check_changelog(p, monorepo_root=monorepo_root), "DOC-003")
    _run(lambda p: check_releaserc(p, monorepo_root=monorepo_root), "VER-003")
    _run(check_conventional_commits, "VER-001")
    _run(check_breaking_change_footer, "VER-002")
    _run(check_split_package_identity, "DOC-009")

    if not is_library:
        _run(lambda p: check_env_example(p, monorepo_root=monorepo_root), "DOC-004")

    if is_python and not is_frontend:
        _run(check_pre_commit, "PY-008")
        _run(check_src_layout, "PY-005")
        _run(check_no_setup_py, "PY-007")
        _mark_checked("PY-001", "PY-002", "PY-003", "PY-009", "PY-010", "CD-002")
        try:
            findings.extend(check_pyproject(repo_path, exceptions=_exceptions))
        except Exception as exc:
            findings.append(
                _finding(
                    "CHECKER",
                    "WARN",
                    "structural_conformance",
                    f"check_pyproject raised an unexpected error: {exc}",
                    "",
                )
            )
        _run(check_no_print_statements, "CD-003")
        _run(check_naming_conventions, "PY-011")
        _run(check_failed_prefix, "PY-012")
        _run(check_dedup_handling_present, "PY-013")
        _run(check_finally_cleanup, "PY-014")

    if (is_python or is_fastapi) and not is_library and not is_frontend:
        _run(check_common_python_utils_dep, "PY-006")

    # Healthchecks only applies to trigger cogs
    _mark_checked("CD-007")
    if is_trigger_cog:
        findings.extend(
            check_healthchecks_integration(repo_path, cog_subtype="trigger")
        )

    _run(check_structured_logging, "CD-009")
    _run(check_no_hardcoded_secrets, "CD-011")
    _run(check_no_manual_changelog, "VER-004")

    _mark_checked("XSTACK-001")
    if (evaluator_config is None and "XSTACK-001" not in _exceptions) or (
        evaluator_config is not None
        and evaluator_config.resolve_dispatch("XSTACK-001").should_run
    ):
        # Static sites are excluded from XSTACK-001 by type scoping
        if not is_frontend or (
            evaluator_config is not None and not evaluator_config.is_static_site
        ):
            findings.extend(
                check_shared_library_used(
                    repo_path,
                    language=language,
                    workspace_package_json_text=workspace_package_json_text,
                )
            )
    else:
        # XSTACK-001 does not apply to this repo shape (e.g. logger-primitive
        # trait) or is explicitly exempted. Emit no finding — see the main
        # _run() skip branch for the rationale.
        pass

    # Standards freshness check only applies to standards-repo type
    if evaluator_config is not None:
        if evaluator_config.is_standards_repo:
            _run(check_standards_freshness, "PRIN-009")
            _run(check_meta_release_pipeline_wired, "META-001")
            _run(check_meta_no_scattered_metadata, "META-002")
            _run(check_meta_canonical_enums_are_dicts, "META-003")
            _run(check_meta_005_check_notes_prefix, "META-005")
            _run(check_meta_006_prefix_file_correlation, "META-006")
            _run(check_meta_007_rule_ids_unique, "META-007")
    elif dod_type is None:
        _run(check_standards_freshness, "PRIN-009")

    _run(check_no_hardcoded_urls, "FE-007")

    _mark_checked("VER-003", "VER-005", "VER-006")
    try:
        findings.extend(
            check_ci(
                repo_path,
                exceptions=_exceptions,
                monorepo_root=monorepo_root,
            )
        )
    except Exception as exc:
        findings.append(
            _finding(
                "CHECKER",
                "WARN",
                "structural_conformance",
                f"check_ci raised an unexpected error: {exc}",
                "",
            )
        )

    if is_python:
        _run(check_pytest_coverage_in_ci, "TEST-006")
        _run(check_respx_for_http_mocking, "TEST-007")
        _run(check_mypy_in_ci, "TEST-012")

    if is_python:
        _run(check_pytest_config, "TEST-005")

    # DOC-013 README running locally — use new type for dod_type hint
    if (
        evaluator_config is None
        or evaluator_config.resolve_dispatch("DOC-013").should_run
    ):
        _mark_checked("DOC-013")
        # Map type to dod_type string for check_readme_running_locally
        if evaluator_config is not None:
            _readme_dod = _type_to_dod(evaluator_config.repo_type, language)
        else:
            _readme_dod = dod_type
        findings.extend(check_readme_running_locally(repo_path, dod_type=_readme_dod))

    if is_frontend:
        _run(check_tailwind, "FE-003")
    # Static site specific
    if evaluator_config is not None:
        if evaluator_config.is_static_site:
            _run(check_astro_framework, "FE-001")
            _run(check_astro_pinned_versions, "FE-008")
            _run(check_astro_build_time_data, "FE-009")
            _run(check_astro_runtime_queries, "FE-010")
        elif evaluator_config.is_react_app:
            _run(check_vite_react_ts, "FE-002")
            _run(check_shadcn, "FE-004")
            _run(check_react_hook_form_zod, "FE-005")
    else:
        if dod_type == "new_frontend_site":
            _run(check_astro_framework, "FE-001")
            _run(check_astro_pinned_versions, "FE-008")
            _run(check_astro_build_time_data, "FE-009")
            _run(check_astro_runtime_queries, "FE-010")
        if dod_type == "new_react_app":
            _run(check_vite_react_ts, "FE-002")
            _run(check_shadcn, "FE-004")
            _run(check_react_hook_form_zod, "FE-005")

    if is_pipeline_cog:
        _run(check_retry_logic, "PIPE-007")
        _run(check_no_retired_trigger_patterns, "PIPE-008")
        _run(check_evaluation_step, "PIPE-009")
        _run(check_prefect_serve_pattern, "CD-015")
        _run(check_db_writes_use_upserts, "PIPE-002")
        _run(check_inputs_not_deleted, "PIPE-005")

    # PIPE-001 applies to both pipeline-cogs and trigger-cogs — Prefect is
    # required on both, with slightly different usage patterns (see the
    # check function for the pipeline-vs-trigger branch).
    if is_pipeline_cog or is_trigger_cog:
        _cog_subtype = "trigger" if is_trigger_cog else "pipeline"

        def _pipe_001_check(p: Path) -> list[Finding]:
            return check_prefect_present(p, cog_subtype=_cog_subtype)

        _run(_pipe_001_check, "PIPE-001")

        # CD-005 also covers pipeline-cogs and trigger-cogs. It overlaps with
        # PIPE-001's condition 1 by design (see the rule body) — a repo missing
        # prefect entirely will produce two findings, which is correct.
        def _cd_005_check(p: Path) -> list[Finding]:
            return check_prefect_cloud_observability(p, cog_subtype=_cog_subtype)

        _run(_cd_005_check, "CD-005")

    # API-001 / API-002 apply to api-service repos regardless of language.
    if is_api_service:

        def _api_001_check(p: Path) -> list[Finding]:
            return check_railway_hosted_api(p, language=language)

        def _api_002_check(p: Path) -> list[Finding]:
            return check_postgres_only_data_store(p, language=language)

        _run(_api_001_check, "API-001")
        _run(_api_002_check, "API-002")
    # CD-006 applies to pipeline-cogs, trigger-cogs, and api-services —
    # any repo type where GHA relaying would be a genuine anti-pattern.
    if is_pipeline_cog or is_trigger_cog or is_api_service:
        _run(check_gha_not_trigger_relay, "CD-006")

    # XSTACK-002 (response shape parity) applies to api-service only per
    # the narrowed applies_to in the audit.
    if is_api_service:

        def _xstack_002_check(p: Path) -> list[Finding]:
            return check_response_shape_parity(p, language=language)

        _run(_xstack_002_check, "XSTACK-002")

    # DOC-005 (ADRs present) applies to pipeline-cogs, trigger-cogs,
    # api-services, shared-libraries, and standards-repo per the catalog.
    if (
        is_pipeline_cog
        or is_trigger_cog
        or is_api_service
        or is_library
        or (evaluator_config is not None and evaluator_config.is_standards_repo)
    ):
        _run(check_adrs_present, "DOC-005")

    _is_static = (
        evaluator_config is not None and evaluator_config.is_static_site
    ) or dod_type == "new_frontend_site"
    # Wave 9 — coverage sweep for 35 rules previously only in catalog
    # ==================================================================

    # API domain — api-service only
    if is_api_service:

        def _api_003(p: Path) -> list[Finding]:
            return check_orm_usage(p, language=language)

        def _api_004(p: Path) -> list[Finding]:
            return check_v1_route_prefix(p, language=language)

        def _api_007(p: Path) -> list[Finding]:
            return check_clerk_auth_dep(p, language=language)

        def _api_008(p: Path) -> list[Finding]:
            return check_unauthenticated_routes(p, language=language)

        def _api_009(p: Path) -> list[Finding]:
            return check_cors_config(p, language=language)

        def _api_010(p: Path) -> list[Finding]:
            return check_health_endpoint(p, language=language)

        _run(_api_003, "API-003")
        _run(_api_004, "API-004")
        _run(check_response_envelope_presence, "API-005")
        _run(_api_007, "API-007")
        _run(_api_008, "API-008")
        _run(_api_009, "API-009")
        _run(_api_010, "API-010")

        def _api_011(p: Path) -> list[Finding]:
            return check_migration_in_ci(
                p, language=language, monorepo_root=monorepo_root
            )

        _run(_api_011, "API-011")

        # API-006 and AUTH-002 — Python-only shape checks
        if language == "python":
            _run(check_owner_id_column, "API-006")
            _run(check_auth_header_parity, "AUTH-002")

    # CD-008 logger misuse — applies broadly; language-gated to Python.
    if language == "python":
        _run(check_logger_misuse, "CD-008")

    # CD-010 three-layer observability — applies to all runtime services.
    if is_pipeline_cog or is_trigger_cog or is_api_service:
        _cog_st_010 = (
            "trigger" if is_trigger_cog else ("pipeline" if is_pipeline_cog else None)
        )

        def _cd_010_check(p: Path) -> list[Finding]:
            return check_three_layer_observability(
                p, cog_subtype=_cog_st_010, language=language
            )

        _run(_cd_010_check, "CD-010")

    # CD-014 — static-site deploy target
    if _is_static:
        _run(check_cloudflare_pages_deploy, "CD-014")

    # DOC-006 / DOC-007 — Python docstring / Pydantic descriptions.
    if language == "python":
        _run(check_public_docstrings, "DOC-006")
    if language == "python" and (is_pipeline_cog or is_api_service):
        _run(check_pydantic_field_descriptions, "DOC-007")

    # DOC-010 / DOC-011 — FastAPI route docs + unauthenticated-route intent.
    if is_api_service and language == "python":
        _run(check_fastapi_route_docs, "DOC-010")
        _run(check_unauthenticated_routes_documented, "DOC-011")

    # FE-006 — fetch error handling on static sites + react apps.
    if (
        _is_static
        or (evaluator_config is not None and evaluator_config.is_react_app)
        or dod_type == "new_react_app"
    ):
        _run(check_fetch_error_handling, "FE-006")

    # Pipeline rules — PIPE-004, PIPE-006, PIPE-011, PIPE-012.
    # PIPE-003 is LLM-routed per ecosystem-standards v3.8.0.
    if is_pipeline_cog or is_trigger_cog:
        _cog_st_pipe = "trigger" if is_trigger_cog else "pipeline"

        def _pipe_011_check(p: Path) -> list[Finding]:
            return check_final_evaluation_task(p, cog_subtype=_cog_st_pipe)

        _run(check_shared_resource_concurrency, "PIPE-004")
        _run(check_prefect_run_logger, "PIPE-006")
        _run(_pipe_011_check, "PIPE-011")
        _run(check_hardcoded_retry_delay, "PIPE-012")

    # Python — PY-004, PY-015
    if language == "python" and (is_pipeline_cog or is_api_service or is_library):
        _run(check_pydantic_for_external_data, "PY-004")
    if is_api_service and language == "python":
        _run(check_async_sqlalchemy, "PY-015")

    # Configuration — CFG-001, CFG-002
    if language == "python" and (is_pipeline_cog or is_api_service or is_library):
        _run(check_settings_field_consistency, "CFG-001")
        _run(check_env_example_settings_parity, "CFG-002")

    # Testing — TEST-008, TEST-009, TEST-010, TEST-011, TEST-013, TEST-GAP-001
    if is_api_service:
        _run(check_testclient_for_v1_routes, "TEST-008")
        if language == "python":
            _run(check_db_test_fixtures, "TEST-009")
            _run(check_route_contract_tests, "TEST-010")
    if is_pipeline_cog or is_api_service:
        _run(check_mock_assertions, "TEST-011")
        _run(check_test_gap_critical_paths, "TEST-GAP-001")
        _run(check_hardcoded_standards_version, "EVAL-002")

    def _test_013(p: Path) -> list[Finding]:
        return check_hardcoded_time_values(p, language=language)

    if (
        is_pipeline_cog
        or is_api_service
        or (evaluator_config is not None and evaluator_config.is_react_app)
        or dod_type == "new_react_app"
    ):
        _run(_test_013, "TEST-013")

    # XSTACK-004 — env var prefix. Frontend + api-service.
    if (
        _is_static
        or (evaluator_config is not None and evaluator_config.is_react_app)
        or dod_type == "new_react_app"
        or is_api_service
    ):
        _run(check_env_var_prefix, "XSTACK-004")

    _run(
        lambda p: check_releaserc_assets(p, monorepo_root=monorepo_root),
        "VER-008",
    )

    # XSTACK-003 pnpm — applies to api-service (TS) and react-app
    if evaluator_config is not None:
        _needs_pnpm = evaluator_config.is_react_app or (
            evaluator_config.is_api_service and language == "typescript"
        )
    else:
        _needs_pnpm = dod_type in ("new_hono_service", "new_react_app")

    if _needs_pnpm:

        def _pnpm_lock_check(p: Path) -> list[Finding]:
            return check_pnpm_lockfile(p, monorepo_root=monorepo_root)

        _run(_pnpm_lock_check, "XSTACK-003")

    # ── Rules added with ecosystem-standards v5.x ────────────────────────
    # Registered unconditionally: `resolve_dispatch()` already skips a
    # rule whose `applies_to` excludes this repo type (SKIP_SCOPE), so
    # gating here as well would duplicate the catalog in code and drift
    # from it. The only conditionals below are ones a rule's own
    # check_notes asks for.
    #
    # XSTACK-006 and XSTACK-007 are deliberately absent: both have
    # `applies_to: None`, which makes resolve_dispatch return SKIP_SCOPE
    # for them on every repo. They read the org listing and the registry
    # rather than any one repo's source, and so run once per flow
    # invocation from _run_applies_to_absent_checks(), alongside EVAL-003
    # / MONO-003 / EVAL-007.

    # security_posture — SEC-001..006.
    _run(check_sec_001, "SEC-001")
    _run(check_sec_002, "SEC-002")
    _run(check_sec_003, "SEC-003")
    _run(check_sec_004, "SEC-004")
    _run(check_sec_005, "SEC-005")
    _run(check_sec_006, "SEC-006")

    # operational_readiness — only OPS-002 is checkable.
    #
    # OPS-001/003/004/005/006 were retired to `checkable: false` in
    # ecosystem-standards v6.5.0. Each demanded a hand-written file whose
    # only reader was this check — a restore date, an SLO nothing
    # consumed, a classification maintained per migration, an egress list
    # the check already derived from source, a rotation date. A rule
    # satisfied by editing a date measures paperwork, not the control,
    # and its cheapest fix is to falsify it. Their check_notes record
    # what would make each checkable again; those will be different
    # checks (a job's recorded run, not a file), so the old ones are
    # deleted rather than left dormant.
    #
    # OPS-007 has no check by design: its gap is a missing shared drain
    # helper in common-python-utils.
    _run(check_ops_002, "OPS-002")

    # cd_readiness — container and platform-descriptor rules.
    # CD-022 and CD-023 skip silently where no Dockerfile exists; the
    # absence of an image definition is CD-021's finding, and reporting
    # it from three rules would obscure which one is actually open.
    def _cd_017_check(p: Path) -> list[Finding]:
        return check_cd_017(p, monorepo_path=None)

    _run(_cd_017_check, "CD-017")
    _run(check_cd_021, "CD-021")
    _run(check_cd_022, "CD-022")
    _run(check_cd_023, "CD-023")
    _run(check_cd_024, "CD-024")

    # cd_readiness — packaging. CD-016 carries its own applicability
    # gate (a repo with no serve() call has no subject) inside the check.
    _run(check_cd_016, "CD-016")
    _run(check_cd_020, "CD-020")

    # structural_conformance — the identity contract. CD-019 splits into
    # caller and receiver halves on repo type, so it needs the type the
    # rest of the dispatcher already resolved.
    def _cd_019_check(p: Path) -> list[Finding]:
        return check_cd_019(p, repo_type=_repo_type_for_checks)

    _run(check_auth_003, "AUTH-003")
    _run(check_auth_004, "AUTH-004")
    _run(_cd_019_check, "CD-019")

    # EVAL-008: check for evaluator.yaml presence
    _mark_checked("EVAL-008")
    if not (repo_path / "evaluator.yaml").exists():
        findings.append(
            _finding(
                "EVAL-008",
                "WARN",
                "structural_conformance",
                "evaluator.yaml is absent from repo root.",
                "Add evaluator.yaml declaring type, traits, exemptions, and deferrals.",
            )
        )

    if evaluator_config is not None:
        catalog = evaluator_config.rule_catalog or {}
        for finding in findings:
            if "status" in finding:
                continue
            rid = str(finding.get("rule_id") or "")
            if not rid:
                continue
            rule_status = str((catalog.get(rid) or {}).get("status") or "").strip()
            if rule_status:
                finding["status"] = rule_status

    findings = _deduplicate_same_repo_findings(findings)
    return CheckResult(findings=findings, checked_rule_ids=checked_rule_ids)
