"""evaluator_config.py

Loads and validates a repo's evaluator.yaml file (ADR-001, ADR-002).

The evaluator.yaml lives at the root of each repo and declares:
  - type: repo type (pipeline-cog, trigger-cog, api-service, etc.)
  - traits: optional list of composable flags (logger-primitive, cloudflare-pages, etc.)
  - exemptions: rules that genuinely do not apply, with required reason strings
  - deferrals: rules that apply but are not currently prioritized

This module is the single point of truth for reading that config.
It falls back gracefully when evaluator.yaml is absent (migration period).
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


class Disposition(StrEnum):
    """One of the 7 dispatch outcomes per ecosystem-standards v4.0.0
    schema.dispatch.precedence. Order matches index.yaml."""

    SKIP_SCOPE = "skip_scope"
    SKIP_TRAIT_EXEMPT = "skip_trait_exempt"
    SKIP_REPO_EXEMPT = "skip_repo_exempt"
    RUN_DEFERRED = "run_deferred"
    RUN_DOWNGRADED = "run_downgraded"
    RUN_MODIFIED = "run_modified"
    RUN_DEFAULT = "run_default"


@dataclass
class DispositionResult:
    """Resolved dispatch for a (rule, repo) pair.

    - `disposition`: which of the 7 precedence branches applies.
    - `reason`: human-readable explanation (trait name, exemption
      reason, deferral reason, etc.) — appears on emitted INFO findings.
    - `downgraded_severity`: for RUN_DOWNGRADED only, the target
      severity the finding should be emitted at.
    - `modifier_rule_id`: for RUN_MODIFIED only, the rule whose
      `modifies:` list includes this rule.
    - `deferral_expired_on`: set when this rule carried a deferral whose
      `until:` date has passed. The deferral no longer applies — the
      disposition is whatever the remaining precedence steps resolve to
      — but the date is carried so the finding can say that a deadline
      was missed rather than silently appearing at full severity.
    """

    disposition: Disposition
    reason: str = ""
    downgraded_severity: str | None = None
    modifier_rule_id: str | None = None
    deferral_expired_on: _dt.date | None = None

    @property
    def should_run(self) -> bool:
        """True when the check must actually execute."""
        return self.disposition in (
            Disposition.RUN_DEFERRED,
            Disposition.RUN_DOWNGRADED,
            Disposition.RUN_MODIFIED,
            Disposition.RUN_DEFAULT,
        )

    @property
    def emits_skip_finding(self) -> bool:
        """True when a skip produces an INFO finding citing why."""
        return self.disposition in (
            Disposition.SKIP_TRAIT_EXEMPT,
            Disposition.SKIP_REPO_EXEMPT,
        )


# Valid repo types per index.yaml schema.repo_types (migration fallback).
# Should match catalog_schema.repo_types when catalog fetch succeeds.
VALID_REPO_TYPES = {
    "pipeline-cog",
    "trigger-cog",
    "api-service",
    "shared-library",
    "static-site",
    "react-app",
    "standards-repo",
    "shared-workflows",
}

# Valid traits per index.yaml schema.traits (migration fallback).
# Should match catalog_schema.traits keys when catalog fetch succeeds.
VALID_TRAITS = {
    "logger-primitive",
    "cloudflare-pages",
    "multi-flow",
    "pipeline-cog-evaluator",
}

# One-shot warning latch — when an EvaluatorConfig is constructed without
# a catalog (rule_catalog unset), dispatch cannot derive scope/trait behavior.
# We warn
# once per process so tests and migration-period callers don't spam logs.
_WARNED_NO_CATALOG = False


def _warn_once_no_catalog() -> None:
    global _WARNED_NO_CATALOG
    if _WARNED_NO_CATALOG:
        return
    _WARNED_NO_CATALOG = True
    log.warning(
        "evaluator_config: EvaluatorConfig constructed without a catalog "
        "(rule_catalog unset) — dispatch falls back to explicit-repo "
        "exemptions only. This is "
        "expected in unit tests; a warning in production indicates the "
        "catalog fetch failed."
    )


@dataclass
class EvaluatorConfig:
    """Parsed and validated contents of a repo's evaluator.yaml."""

    repo_type: str
    traits: list[str] = field(default_factory=list)
    exemption_ids: list[str] = field(default_factory=list)
    exemption_reasons: dict[str, str] = field(default_factory=dict)
    deferral_ids: list[str] = field(default_factory=list)
    deferral_reasons: dict[str, str] = field(default_factory=dict)
    # rule_id -> the deferral's `until:` date. A rule deferred
    # open-endedly (`until: null`, or the key omitted) is absent from
    # this mapping rather than mapped to None, so "no deadline" and
    # "deadline unparseable" stay distinguishable.
    deferral_until: dict[str, _dt.date] = field(default_factory=dict)
    source: str = "evaluator.yaml"

    # Catalog data — populated by load_evaluator_config when available.
    # When None, resolve_dispatch falls back to conservative behavior
    # (type-scope skipped, no trait effects applied).
    rule_catalog: dict[str, dict] | None = None
    catalog_schema: dict | None = None

    @property
    def all_skipped_ids(self) -> frozenset[str]:
        """Set of rule IDs the evaluator will skip for this repo.

        This is a convenience view over resolve_dispatch() — it returns
        rule IDs whose disposition is any SKIP_* variant. For full
        dispatch semantics (including deferrals and downgrades), call
        resolve_dispatch directly.

        Returns frozenset() when no catalog is available (migration
        period or fetch failure).
        """
        if self.rule_catalog is None:
            # No catalog → only explicit exemptions skip.
            _warn_once_no_catalog()
            return frozenset(self.exemption_ids)
        skipped: set[str] = set(self.exemption_ids)
        for rule_id in self.rule_catalog:
            result = self.resolve_dispatch(rule_id)
            if not result.should_run:
                skipped.add(rule_id)
        return frozenset(skipped)

    def resolve_dispatch(
        self, rule_id: str, *, today: _dt.date | None = None
    ) -> DispositionResult:
        """Walk the 7-step dispatch precedence from
        ecosystem-standards index.yaml schema.dispatch for this rule
        and this repo's config. Returns a DispositionResult telling
        the caller what to do.

        Precedence (index.yaml is authoritative):
          1. scope           — applies_to vs repo_type
          2. trait_exemption — trait.exempts includes rule
          3. repo_exemption  — evaluator.yaml exemptions includes rule
          4. repo_deferral   — evaluator.yaml deferrals includes rule
          5. trait_downgrade — trait.downgrades includes rule
          6. rule_modifier   — some other rule's modifies: includes rule
          7. default         — run at declared severity

        A SKIP_* disposition short-circuits; subsequent steps are not
        consulted.
        """
        rule_meta = (self.rule_catalog or {}).get(rule_id, {})

        # Step 1 — scope.
        applies_to = rule_meta.get("applies_to")
        if applies_to is None:
            return DispositionResult(disposition=Disposition.SKIP_SCOPE)
        if "all" not in applies_to and self.repo_type not in applies_to:
            return DispositionResult(disposition=Disposition.SKIP_SCOPE)

        # Step 2 — trait exemption.
        for trait in self.traits:
            trait_spec = (self.catalog_schema or {}).get("traits", {}).get(trait) or {}
            if rule_id in (trait_spec.get("exempts") or []):
                return DispositionResult(
                    disposition=Disposition.SKIP_TRAIT_EXEMPT,
                    reason=f"Exempted by trait: {trait}",
                )

        # Step 3 — per-repo exemption.
        if rule_id in self.exemption_ids:
            return DispositionResult(
                disposition=Disposition.SKIP_REPO_EXEMPT,
                reason=self.exemption_reasons.get(rule_id, ""),
            )

        # Step 4 — per-repo deferral.
        #
        # `until:` is honoured here. index.yaml specifies it as
        # "optional — null means open-ended. If set to a date that has
        # passed, the deferral expires and the finding resurfaces at
        # full severity." Until this was parsed, every deferral was
        # silently permanent: a deadline could be written down, pass,
        # and never change anything, which is worse than not offering
        # the field at all — the repo believes it has a commitment on
        # record and the evaluator never checks it.
        #
        # An expired deferral does not short-circuit. It falls through
        # to the remaining precedence steps, so a rule that is both
        # expired-deferred and trait-downgraded still resolves to the
        # downgrade. The expiry date rides along on the result so the
        # finding can name the missed deadline instead of appearing at
        # full severity with no explanation.
        expired_on: _dt.date | None = None
        if rule_id in self.deferral_ids:
            until = self.deferral_until.get(rule_id)
            if until is None or until >= (today or _dt.date.today()):
                return DispositionResult(
                    disposition=Disposition.RUN_DEFERRED,
                    reason=self.deferral_reasons.get(rule_id, ""),
                )
            expired_on = until

        # Step 5 — trait downgrade.
        for trait in self.traits:
            trait_spec = (self.catalog_schema or {}).get("traits", {}).get(trait) or {}
            for dg in trait_spec.get("downgrades") or []:
                if dg.get("rule") == rule_id:
                    return DispositionResult(
                        disposition=Disposition.RUN_DOWNGRADED,
                        reason=f"Downgraded by trait: {trait}. {dg.get('reason', '')}".strip(),
                        downgraded_severity=dg.get("to") or "INFO",
                        deferral_expired_on=expired_on,
                    )

        # Step 6 — rule modifier.
        for other_id, other_meta in (self.rule_catalog or {}).items():
            if rule_id not in (other_meta.get("modifies") or []):
                continue
            other_applies = other_meta.get("applies_to")
            if other_applies is None:
                continue
            if "all" in other_applies or self.repo_type in other_applies:
                return DispositionResult(
                    disposition=Disposition.RUN_MODIFIED,
                    reason=f"Modified by rule: {other_id}",
                    modifier_rule_id=other_id,
                    deferral_expired_on=expired_on,
                )

        # Step 7 — default.
        return DispositionResult(
            disposition=Disposition.RUN_DEFAULT, deferral_expired_on=expired_on
        )

    def is_deferred(self, rule_id: str) -> bool:
        """True when `rule_id` is deferred *and* the deferral is still live.

        A deferral whose `until:` has passed is not a deferral any more,
        so this returns False for it — callers asking "is this deferred"
        want the effective answer, not the declared one.

        This deliberately answers from the deferral alone rather than
        delegating to resolve_dispatch(). Dispatch also folds in scope,
        which is unknowable without a catalog: routed through it, a
        config built by hand or during the migration period would report
        every rule as not-deferred because `applies_to` is absent and
        step 1 short-circuits to SKIP_SCOPE. The question here is
        narrower and answerable on its own terms.
        """
        if rule_id not in self.deferral_ids:
            return False
        until = self.deferral_until.get(rule_id)
        return until is None or until >= _dt.date.today()

    def is_skipped(self, rule_id: str) -> bool:
        """True when `rule_id` will be skipped for this repo.

        Convenience wrapper over `all_skipped_ids` — covers scope
        mismatches, trait exemptions, and per-repo exemptions.
        """
        return rule_id in self.all_skipped_ids

    @property
    def language(self) -> str:
        """Primary source language for this repo type.

        Returns 'python' for Python service types (pipeline-cog,
        trigger-cog, api-service, shared-library, standards-repo) and
        'typescript' for frontend types (static-site, react-app).
        Note: api-service may be either — language is authoritative
        from ecosystem.yaml, not from this property.
        """
        if self.repo_type in (
            "pipeline-cog",
            "trigger-cog",
            "api-service",
            "shared-library",
            "standards-repo",
        ):
            return "python"
        return "typescript"

    @property
    def is_python_service(self) -> bool:
        """True for repo types whose canonical implementation is Python."""
        return self.repo_type in (
            "pipeline-cog",
            "trigger-cog",
            "api-service",
            "shared-library",
        )

    @property
    def is_pipeline_cog(self) -> bool:
        """True when repo_type == 'pipeline-cog'."""
        return self.repo_type == "pipeline-cog"

    @property
    def is_trigger_cog(self) -> bool:
        """True when repo_type == 'trigger-cog'."""
        return self.repo_type == "trigger-cog"

    @property
    def is_api_service(self) -> bool:
        """True when repo_type == 'api-service'."""
        return self.repo_type == "api-service"

    @property
    def is_shared_library(self) -> bool:
        """True when repo_type == 'shared-library'."""
        return self.repo_type == "shared-library"

    @property
    def is_static_site(self) -> bool:
        """True when repo_type == 'static-site'."""
        return self.repo_type == "static-site"

    @property
    def is_react_app(self) -> bool:
        """True when repo_type == 'react-app'."""
        return self.repo_type == "react-app"

    @property
    def is_standards_repo(self) -> bool:
        """True when repo_type == 'standards-repo'."""
        return self.repo_type == "standards-repo"

    @property
    def is_frontend(self) -> bool:
        """True when repo_type is a frontend type (static-site or react-app)."""
        return self.repo_type in ("static-site", "react-app")


def load_evaluator_config(
    repo_path: Path,
    *,
    fallback_type: str | None = None,
    fallback_exceptions: list[str] | None = None,
    fallback_exception_reasons: dict[str, str] | None = None,
    rule_catalog: dict[str, dict] | None = None,
    catalog_schema: dict | None = None,
) -> EvaluatorConfig:
    """Load and return the EvaluatorConfig for a repo.

    Reads evaluator.yaml from repo_path if present; falls back to a config
    derived from the provided fallback_type and exception lists if absent or
    unparseable. Never raises.

    When `rule_catalog` and `catalog_schema` are provided (from PR 2's
    flow-start fetch), the returned config's resolve_dispatch() is
    fully functional. When absent, resolve_dispatch falls back to
    conservative behavior (skip non-applicable, run everything else).
    """
    evaluator_yaml = repo_path / "evaluator.yaml"
    cfg: EvaluatorConfig | None = None
    if evaluator_yaml.exists():
        try:
            raw = yaml.safe_load(evaluator_yaml.read_text()) or {}
            cfg = _parse_evaluator_yaml(
                raw, source="evaluator.yaml", catalog_schema=catalog_schema
            )
        except Exception as exc:
            log.warning(
                "evaluator_config: failed to parse evaluator.yaml at %s: %s — falling back",
                repo_path,
                exc,
            )

    if cfg is None:
        cfg = _build_fallback_config(
            fallback_type=fallback_type,
            fallback_exceptions=fallback_exceptions or [],
            fallback_exception_reasons=fallback_exception_reasons or {},
        )

    cfg.rule_catalog = rule_catalog
    cfg.catalog_schema = catalog_schema
    return cfg


def _known_repo_types(catalog_schema: dict | None) -> set[str]:
    """The repo-type vocabulary, catalog first.

    VALID_REPO_TYPES is a copy of index.yaml's ``schema.repo_types``,
    and a copy goes stale. When the catalog fetch succeeded, the catalog
    is authoritative: a type added there works immediately, without
    waiting for a release of this cog.

    This is not cosmetic. A type the copy has not caught up with makes
    _parse_evaluator_yaml raise, the loader fall back, and
    _map_legacy_type default the repo to 'pipeline-cog' — so a repo of
    four YAML files gets graded as a deployed Prefect worker and
    collects a page of findings about Sentry, railway.json and
    semantic-release. That is exactly what happened to .github.
    """
    if isinstance(catalog_schema, dict):
        declared = catalog_schema.get("repo_types")
        if isinstance(declared, dict) and declared:
            return {str(k).strip() for k in declared}
        if isinstance(declared, list) and declared:
            return {str(k).strip() for k in declared}
    return set(VALID_REPO_TYPES)


def _parse_evaluator_yaml(
    raw: dict,
    source: str = "evaluator.yaml",
    catalog_schema: dict | None = None,
) -> EvaluatorConfig:
    repo_type = str(raw.get("type", "")).strip()
    known = _known_repo_types(catalog_schema)
    if repo_type not in known:
        raise ValueError(
            f"evaluator.yaml: invalid type '{repo_type}'. "
            f"Must be one of: {sorted(known)}"
        )

    traits = []
    for t in raw.get("traits", []) or []:
        t = str(t).strip()
        if t in VALID_TRAITS:
            traits.append(t)
        else:
            log.warning("evaluator_config: unknown trait '%s' — ignored", t)

    exemption_ids = []
    exemption_reasons = {}
    for item in raw.get("exemptions", []) or []:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("rule", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if rule_id:
            exemption_ids.append(rule_id)
            if reason:
                exemption_reasons[rule_id] = reason

    deferral_ids = []
    deferral_reasons = {}
    deferral_until: dict[str, _dt.date] = {}
    for item in raw.get("deferrals", []) or []:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("rule", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if rule_id:
            deferral_ids.append(rule_id)
            if reason:
                deferral_reasons[rule_id] = reason
            parsed_until = _parse_until(item.get("until"), rule_id, source)
            if parsed_until is not None:
                deferral_until[rule_id] = parsed_until

    return EvaluatorConfig(
        repo_type=repo_type,
        traits=traits,
        exemption_ids=exemption_ids,
        exemption_reasons=exemption_reasons,
        deferral_ids=deferral_ids,
        deferral_reasons=deferral_reasons,
        deferral_until=deferral_until,
        source=source,
    )


def _parse_until(raw_until: object, rule_id: str, source: str) -> _dt.date | None:
    """Read a deferral's `until:` into a date, or None for open-ended.

    index.yaml types the field `date|null`. PyYAML already hands back a
    `datetime.date` for an unquoted `2026-12-01`, so the common case is
    a passthrough; a quoted ISO string is parsed, and a datetime is
    narrowed to its date.

    An unparseable value returns None — open-ended — rather than
    raising or expiring the deferral immediately. Both alternatives are
    worse: raising would fail the whole run over a typo in one line of
    one repo's config, and expiring would resurface a finding at full
    severity because of a formatting mistake, which reads as the rule
    regressing. A warning is logged so the typo is still visible.
    """
    if raw_until is None:
        return None
    if isinstance(raw_until, _dt.datetime):
        return raw_until.date()
    if isinstance(raw_until, _dt.date):
        return raw_until
    text = str(raw_until).strip()
    if not text or text.lower() in {"null", "none", "~"}:
        return None
    try:
        return _dt.date.fromisoformat(text)
    except ValueError:
        log.warning(
            "evaluator_config: %s: deferral for %s has an unparseable "
            "until: value %r — treating the deferral as open-ended",
            source,
            rule_id,
            raw_until,
        )
        return None


def _build_fallback_config(
    fallback_type: str | None,
    fallback_exceptions: list[str],
    fallback_exception_reasons: dict[str, str],
) -> EvaluatorConfig:
    repo_type = _map_legacy_type(fallback_type)
    log.info(
        "evaluator_config: evaluator.yaml absent — using fallback type '%s' (from '%s')",
        repo_type,
        fallback_type,
    )
    return EvaluatorConfig(
        repo_type=repo_type,
        traits=[],
        exemption_ids=fallback_exceptions,
        exemption_reasons=fallback_exception_reasons,
        deferral_ids=[],
        deferral_reasons={},
        source="ecosystem.yaml (fallback)",
    )


def _map_legacy_type(legacy: str | None) -> str:
    mapping = {
        "worker": "pipeline-cog",
        "api": "api-service",
        "library": "shared-library",
        "site": "static-site",
        "standards": "standards-repo",
        "new_cog": "pipeline-cog",
        "new_fastapi_service": "api-service",
        "new_hono_service": "api-service",
        "new_frontend_site": "static-site",
        "new_react_app": "react-app",
        "pipeline-cog": "pipeline-cog",
        "trigger-cog": "trigger-cog",
        "api-service": "api-service",
        "shared-library": "shared-library",
        "static-site": "static-site",
        "react-app": "react-app",
        "standards-repo": "standards-repo",
    }
    if legacy is None:
        return "shared-library"
    key = str(legacy).strip()
    if key not in mapping:
        # Defaulting an unrecognised type to pipeline-cog grades the repo
        # as a deployed Prefect worker and invents every finding that
        # follows from that. The default is kept so a run never dies on
        # one bad entry, but it is loud: a page of wrong findings is
        # worse than a warning nobody has to read.
        log.warning(
            "evaluator_config: unrecognised repo type '%s' — grading it as "
            "pipeline-cog, which is probably wrong. Add it to index.yaml "
            "schema.repo_types and to VALID_REPO_TYPES.",
            key,
        )
    return mapping.get(key, "pipeline-cog")
