"""Introspection rule checks (EVAL-003, MONO-003, EVAL-007 — drift & data quality)."""

from __future__ import annotations

import re

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)
from evaluator_cog.engine.routing import classify_check_mode


def check_eval_003(
    *,
    lookback_days: int = 30,
) -> list[Finding]:
    """EVAL-003: Findings emitted by evaluator-cog must be specific and actionable.

    Reads pipeline_evaluations for findings emitted by evaluator-cog
    internal sources in the last `lookback_days`. The historical source
    name 'conformance_check' is also included so findings stored before
    the conformance_llm / conformance_deterministic / data_quality split
    are still covered by the quality check.

    Scoping rules (to avoid grading things that aren't conformance findings):

    1. **Severity gate** — only WARN and ERROR findings are graded. INFO
       and SUCCESS rows are dispatcher artifacts (Skipped: / deferred /
       downgraded notes, completion markers) — intentionally short,
       rule-ID-free, and remediation-free. They are not conformance
       findings about a target repo.
    2. **Dispatcher "Skipped:" gate** — any finding whose text begins
       with ``"Skipped:"`` is a dispatch meta-finding. Belt-and-suspenders
       with the severity gate.
    3. **Self-reference gate** — EVAL-003's own findings are skipped so
       the check doesn't recursively grade its prior emissions.

    Quality axes on the filtered set:
      - Row is tagged with a rule_id (i.e. `rule_id` / `violation_id`
        column is populated with a real rule ID, not the ``CHECKER``
        infrastructure-error sentinel). This is ground truth — every
        finding emitted via ``_finding()`` sets the column. The
        previous text-regex proxy was flawed because it fired on
        correctly-tagged findings whose text didn't happen to repeat
        the rule ID.
      - Remediation is non-empty and not trivially short. We avoid
        strict proportional checks against finding length because some
        high-quality findings are naturally verbose while remediation
        can still be concise and actionable.

    Note: the pre-2026-04 ≤60-char length check on finding text was
    removed. Short-but-clear actionable findings like
    ``"Layer 1 missing: no HEALTHCHECKS_URL env var..."`` are
    legitimate — length alone is a poor proxy for quality.
    """
    CHECK_ID = "EVAL-003"
    from mini_app_polis.api import KaianoApiClient

    _eval_003_sources = ",".join(
        [
            "conformance_llm",
            "conformance_deterministic",
            "standards_drift",
            "data_quality",
            # Legacy — pre-rename stored rows still present in the DB.
            "conformance_check",
        ]
    )

    try:
        api = KaianoApiClient.from_env("evaluator-cog")
        response = api.get(
            f"/v1/evaluations?source={_eval_003_sources}"
            f"&lookback_days={lookback_days}&limit=1000"
        )
    except Exception as exc:
        return [
            _finding(
                "CHECKER",
                "WARN",
                "pipeline_consistency",
                f"EVAL-003: could not fetch pipeline_evaluations: {exc}",
                "Investigate api-kaianolevine-com connectivity.",
            )
        ]

    if isinstance(response, dict):
        rows = response.get("data") or response.get("items") or []
    elif isinstance(response, list):
        rows = response
    else:
        rows = []

    findings: list[Finding] = []

    _gradeable_severities = {"WARN", "WARNING", "ERROR", "CRITICAL"}
    # "CHECKER" is the sentinel used by runner.py when a check raises
    # unexpectedly — those are infrastructure-error findings, not
    # conformance findings, and they legitimately have no rule ID.
    _non_rule_sentinels = {"", "CHECKER"}

    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("finding") or "").strip()
        remediation = str(row.get("suggestion") or row.get("remediation") or "").strip()
        row_id = row.get("id") or row.get("run_id")

        # Severity gate — skip dispatcher INFO/SUCCESS rows.
        severity = str(row.get("severity") or "").strip().upper()
        if severity and severity not in _gradeable_severities:
            continue

        # Dispatcher "Skipped:" gate (defense-in-depth).
        if text.startswith("Skipped:"):
            continue

        # Non-applicability gate — LLM checks emit a finding whose text
        # explains why a rule doesn't apply (e.g. "Prefect run history is
        # not applicable (this is an API service, not a pipeline)").
        # These are informational notes, not actionable violations; their
        # remediation is intentionally short or absent. Grading them for
        # remediation length produces false positives.
        _not_applicable_markers = (
            "not applicable",
            "does not apply",
            "n/a —",
            "n/a:",
        )
        if any(m in text.lower() for m in _not_applicable_markers):
            continue

        # Don't recursively grade our own emissions.
        row_rule_id = str(row.get("rule_id") or row.get("violation_id") or "").strip()
        if row_rule_id == CHECK_ID:
            continue

        # Don't grade CHECKER infrastructure-error findings — those are
        # emitted when a check function itself raised, and have no
        # associated rule by design.
        if row_rule_id == "CHECKER":
            continue

        problems: list[str] = []
        if row_rule_id in _non_rule_sentinels:
            problems.append("finding is not tagged with a rule_id")
        if not remediation:
            problems.append("empty remediation")
        # Keep a small quality floor for actionable remediation text,
        # but do not require 1:1 proportionality with long findings.
        elif len(remediation) < 40:
            problems.append(
                f"remediation too short ({len(remediation)} chars) "
                f"vs finding ({len(text)} chars)"
            )

        if problems:
            findings.append(
                _finding(
                    CHECK_ID,
                    "WARN",
                    "pipeline_consistency",
                    f"Finding {row_id} violates EVAL-003: "
                    + "; ".join(problems)
                    + f". finding={text[:80]!r}",
                    "Ensure the finding is emitted via _finding() with a "
                    "proper rule_id and a concrete remediation string.",
                )
            )

    return findings


def check_mono_003(
    *,
    ecosystem: dict | None = None,
    lookback_days: int = 30,
) -> list[Finding]:
    """MONO-003: Sibling findings with same root cause must be deduplicated."""
    CHECK_ID = "MONO-003"
    from collections import defaultdict

    from mini_app_polis.api import KaianoApiClient

    if ecosystem is None:
        return []
    monorepo_services: dict[str, str] = {}
    for svc in ecosystem.get("services", []) or []:
        if not isinstance(svc, dict):
            continue
        mono = svc.get("monorepo")
        sid = svc.get("id")
        if mono and sid:
            monorepo_services[str(sid)] = str(mono)

    if not monorepo_services:
        return []

    _PER_APP_EXPECTED = frozenset({"XSTACK-002"})

    try:
        api = KaianoApiClient.from_env("evaluator-cog")
        service_ids = ",".join(monorepo_services.keys())
        response = api.get(
            f"/v1/evaluations?repos={service_ids}&lookback_days={lookback_days}&limit=2000"
        )
    except Exception as exc:
        return [
            _finding(
                "CHECKER",
                "WARN",
                "monorepo_coherence",
                f"MONO-003: could not fetch pipeline_evaluations: {exc}",
                "Investigate api-kaianolevine-com connectivity.",
            )
        ]

    if isinstance(response, dict):
        rows = response.get("data") or response.get("items") or []
    elif isinstance(response, list):
        rows = response
    else:
        rows = []

    buckets: dict[str, dict[tuple, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if not isinstance(row, dict):
            continue
        repo = str(row.get("repo") or "")
        mono = monorepo_services.get(repo)
        if not mono:
            continue
        rule_id = str(row.get("rule_id") or row.get("violation_id") or "")
        if rule_id in _PER_APP_EXPECTED:
            continue
        key = (
            rule_id,
            str(row.get("finding") or ""),
            str(row.get("standards_version") or ""),
            str(row.get("run_id") or ""),
        )
        buckets[mono][key].append(row)

    findings: list[Finding] = []
    for mono_id, groups in buckets.items():
        for key, group in groups.items():
            if len(group) <= 1:
                continue
            rule_id, _text, _version, _run_id = key
            affected = sorted({str(r.get("repo") or "") for r in group})
            findings.append(
                _finding(
                    CHECK_ID,
                    "WARN",
                    "monorepo_coherence",
                    f"Monorepo '{mono_id}' emitted {len(group)} duplicate "
                    f"findings for rule {rule_id} across sibling apps "
                    f"({', '.join(affected)}). Expected one collapsed "
                    f"finding tagged with all affected service IDs.",
                    f"Verify MONO-001 / MONO-002 dedup logic is invoked "
                    f"for rule {rule_id} on this monorepo.",
                )
            )

    return findings


def check_eval_007(
    *,
    rule_catalog: dict[str, dict] | None = None,
    current_standards_version: str = "",
    evaluator_standards_version: str = "",
) -> list[Finding]:
    """EVAL-007: Standards/evaluator check coverage must be tracked and in sync.

    LLM-routed rules are excluded from the unimplemented set — they
    are correctly handled on the LLM path and do not require a
    deterministic CHECK_ID constant.

    Routing is read from the catalog entry's ``check_mode``, which
    ``_fetch_full_rule_catalog`` derives from the
    ``DETERMINISTIC CHECK.`` / ``LLM CHECK.`` marker on each rule's
    ``check_notes``. The marker is the only routing information the
    catalog carries — ``check_mode`` is not a field in the standards
    YAML and never travels from it; it is computed at fetch time.

    An entry that carries neither ``check_mode`` nor ``check_notes``
    is treated as deterministic, which preserves behaviour for tests
    and callers that build rule_catalog dicts by hand. That fallback
    is why an entry carrying ``check_notes`` but no ``check_mode`` is
    classified here rather than assumed: a hand-built catalog that
    includes the notes should route the same way the flow does, and
    without this it would report every LLM-routed rule as
    unimplemented.

    A deterministic rule counts as "implemented" if any of the
    following appear anywhere in the deterministic package
    (``evaluator_cog/engine/deterministic/*.py``):
      - ``CHECK_ID = "<rule-id>"`` — the canonical constant convention.
      - ``_run(fn, "<rule-id>")`` — dispatch wrapper registration.
      - ``_mark_checked("<rule-id>", ...)`` — manual registration for
        rules whose check logic is inlined rather than wrapped.
    The three are equivalent from EVAL-007's perspective — each
    demonstrates that the evaluator has accepted responsibility for
    the rule. Matching on all three avoids false "unimplemented"
    findings for checks that are wired but don't follow the constant
    convention.

    History: prior to the split of the monolithic ``deterministic.py``
    into the current package, this function called ``inspect.getsource``
    on its own module to collect markers. After the split, that
    collected only the introspection module and falsely flagged every
    rule implemented elsewhere in the package as drift. The scan now
    walks every ``.py`` file in the package directory.
    """
    CHECK_ID = "EVAL-007"
    if not rule_catalog:
        return []

    findings: list[Finding] = []

    import inspect
    from pathlib import Path

    _check_id_re = re.compile(r'CHECK_ID\s*=\s*"([A-Z]+-(?:GAP-)?[0-9A-Z]+)"')
    _run_re = re.compile(r'_run\([^,]+,\s*"([A-Z]+-(?:GAP-)?[0-9A-Z]+)"')
    _mark_call_re = re.compile(r"_mark_checked\(([^)]*)\)")
    _id_literal_re = re.compile(r'"([A-Z]+-(?:GAP-)?[0-9A-Z]+)"')

    impl_ids: set[str] = set()
    try:
        package_dir = Path(inspect.getfile(inspect.getmodule(check_eval_007))).parent
        py_files = sorted(package_dir.glob("*.py"))
    except (TypeError, OSError):
        py_files = []

    for py_file in py_files:
        try:
            src = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        impl_ids.update(_check_id_re.findall(src))
        impl_ids.update(_run_re.findall(src))
        for call_args in _mark_call_re.findall(src):
            impl_ids.update(_id_literal_re.findall(call_args))

    # Only deterministic rules require a CHECK_ID constant. LLM rules
    # are dispatched through engine/llm.py and should not be flagged
    # as missing.
    def _mode(meta: dict | None) -> str:
        meta = meta or {}
        mode = meta.get("check_mode")
        if mode:
            return str(mode)
        notes = meta.get("check_notes")
        if notes:
            return classify_check_mode(rid_for_warning, str(notes))
        return "deterministic"

    deterministic_ids = set()
    for rid_for_warning, meta in rule_catalog.items():
        if _mode(meta) != "llm":
            deterministic_ids.add(rid_for_warning)

    unimplemented = sorted(deterministic_ids - impl_ids)
    orphaned = sorted(impl_ids - set(rule_catalog.keys()))

    for rid in unimplemented:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                "standards_currency",
                f"Rule {rid} is a deterministic checkable rule in the catalog "
                f"but is not registered in evaluator-cog's deterministic package "
                f"(no CHECK_ID constant, _run() call, or _mark_checked() "
                f"reference found in any module under "
                f"evaluator_cog/engine/deterministic/).",
                f"Add a check function for {rid} and register it via "
                f'_run(check_fn, "{rid}") or declare CHECK_ID = "{rid}".',
            )
        )

    for rid in orphaned:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                "standards_currency",
                f"Rule {rid} is registered in evaluator-cog's deterministic "
                f"package but has no matching rule in the catalog "
                f"(orphaned check).",
                "Remove the implementation or restore the rule in ecosystem-standards.",
            )
        )

    if current_standards_version and evaluator_standards_version:
        try:
            cur_parts = [int(p) for p in current_standards_version.split(".")[:2]]
            ev_parts = [int(p) for p in evaluator_standards_version.split(".")[:2]]
            if cur_parts[0] > ev_parts[0]:
                findings.append(
                    _finding(
                        CHECK_ID,
                        "WARN",
                        "standards_currency",
                        f"Evaluator pinned to standards v{evaluator_standards_version}, "
                        f"catalog is at v{current_standards_version} — major version skew.",
                        "Rebuild/redeploy evaluator-cog against the current catalog.",
                    )
                )
            elif cur_parts[0] == ev_parts[0] and (cur_parts[1] - ev_parts[1]) > 1:
                findings.append(
                    _finding(
                        CHECK_ID,
                        "WARN",
                        "standards_currency",
                        f"Evaluator pinned to standards v{evaluator_standards_version}, "
                        f"catalog is at v{current_standards_version} — "
                        f">1 minor version behind.",
                        "Rebuild/redeploy evaluator-cog against the current catalog.",
                    )
                )
        except (ValueError, IndexError):
            pass

    return findings
