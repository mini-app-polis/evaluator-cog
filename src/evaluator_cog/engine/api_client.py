"""API client helpers for posting evaluation findings.

Deduplication is the API's job and only the API's job (PIPE-002). This
module used to guess as well, comparing each finding against the single
most recent stored row for the repo — best-effort by construction, since
it could not see a row two positions back or a redelivery that arrived
after something else had been written. Two guards where one is unsound is
worse than either alone: that one once compared deejay-cog's CD-021
against watcher-cog's identical CD-021 and dropped a true finding.

What remains is reading the answer. A suppressed write returns 200 with
``deduplicated: true``, and counting that as a delivered finding is how a
run comes to report findings as posted that were never stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mini_app_polis import logger as logger_mod
from mini_app_polis.api import KaianoApiClient as CommonPythonApiClient

from evaluator_cog import __version__ as _EVALUATOR_VERSION

log = logger_mod.get_logger()


@dataclass
class PostResult:
    """What actually happened when findings were handed to the API.

    ``post_findings`` used to compute this, log one line of it, and throw
    it away. On 2026-09-03 that cost two full conformance runs: the
    evaluator computed ~162 findings across 13 repos, every POST failed
    before leaving the process, and the flow still finished Completed and
    pinged Healthchecks green — because a swallowed failure is
    indistinguishable from success to every caller.

    Returning the outcome is what lets the flow tell the difference. The
    load-bearing property is :attr:`total_failure`: attempted work that
    posted nothing is a systemic fault (no route, no credential, no
    service), not N independent unlucky findings, and it must surface as
    a failed flow run rather than a warning nobody reads.
    """

    attempted: int = 0
    posted: int = 0
    duplicates: int = 0
    failed: int = 0
    #: One line per suppressed finding, saying what it matched against.
    #: A count alone cannot be acted on: three duplicates in a run tells
    #: you nothing about which rule stopped being reported, or why.
    duplicate_details: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_failure(self) -> bool:
        """True when findings were offered and none reached the API."""
        return self.attempted > 0 and self.posted == 0 and self.failed > 0

    @property
    def last_error(self) -> str:
        """The most recent failure, or "" — what a log line should name."""
        return self.errors[-1] if self.errors else ""

    def merge(self, other: PostResult) -> None:
        """Fold another result into this one, for a run-scoped tally."""
        self.attempted += other.attempted
        self.posted += other.posted
        self.duplicates += other.duplicates
        self.duplicate_details.extend(other.duplicate_details)
        self.failed += other.failed
        self.errors.extend(other.errors)


def _was_deduplicated(response: Any) -> bool:
    """True when the API recognised this finding rather than storing it.

    PIPE-002. A suppressed write answers 200 with ``deduplicated: true``,
    so a caller that checks only for an exception counts it as delivered —
    and a run then reports findings as posted that were never stored,
    which is the September failure shape reached by a new route.

    An absent or unrecognisable flag means stored. That is the safe
    reading: an API from before the idempotency guard does not send the
    field and did write the row.
    """
    if not isinstance(response, dict):
        return False
    data = response.get("data")
    return isinstance(data, dict) and data.get("deduplicated") is True


def post_findings(
    *,
    findings: list[dict],
    run_id: str,
    repo: str,
    flow_name: str | None,
    source: str,
    standards_version: str,
    direct_finding_text: str | None = None,
) -> PostResult:
    """Post a list of findings to api-kaianolevine-com.

    Never raises — a caller mid-run should not lose the findings it has
    already computed because one POST failed. But it no longer stays
    silent either: the returned :class:`PostResult` says how many were
    offered, accepted, deduplicated and rejected, and the caller is
    expected to act on ``total_failure``.
    """
    result = PostResult()
    err_ct = warn_ct = info_ct = 0

    api_client = CommonPythonApiClient.from_env("evaluator-cog")

    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "INFO").upper()
        if sev == "WARNING":
            sev = "WARN"
        if sev in {"CRITICAL", "ERROR"}:
            err_ct += 1
        elif sev == "WARN":
            warn_ct += 1
        elif sev == "SUCCESS":
            info_ct += 1
        else:
            sev = "INFO"
            info_ct += 1

        finding_text = (f.get("finding") or "").strip()
        if not finding_text:
            log.warning("Skipping finding with empty finding text")
            continue

        violation_id = f.get("violation_id") or None
        # Note: findings may carry `status`, `deferred`, and `downgraded`
        # keys set by the v4.0.0 dispatch machinery in run_all_checks. The
        # api-kaianolevine-com /v1/evaluations endpoint does not accept
        # these fields (extra=forbid), so we do not forward them. The
        # dispatch metadata is preserved on the in-process finding dicts
        # for logging and for the LLM prompt, but not persisted server-side.
        payload = {
            "run_id": run_id,
            "repo": repo,
            "flow_name": flow_name,
            "dimension": f.get("dimension") or "pipeline_consistency",
            "severity": sev,
            "finding": finding_text,
            "suggestion": f.get("suggestion") or None,
            "standards_version": standards_version,
            # Which build wrote this. run_id cannot say: a fleet pass's id
            # is minted by the API before any job reaches an evaluator, and
            # standards_version names the catalog, not the code applying it.
            "evaluator_version": _EVALUATOR_VERSION,
            "source": source,
            "violation_id": violation_id,
        }
        result.attempted += 1
        try:
            response = api_client.post("/v1/evaluations", payload)
        except Exception as e:
            log.warning("pipeline evaluation: failed to POST finding: %s", e)
            result.failed += 1
            result.errors.append(str(e))
            continue

        if _was_deduplicated(response):
            # Offered and declined, which is neither a post nor a failure.
            # The server holds this finding already under this run — a
            # redelivered message, or the release workflow's retry.
            log.info(
                "⏭️ Already stored for run_id=%s: %s",
                run_id,
                finding_text[:60],
            )
            result.duplicates += 1
            result.duplicate_details.append(
                f"{violation_id or 'finding'} for {repo} was already stored "
                f"server-side under run {run_id}: {finding_text[:120]}"
            )
        else:
            result.posted += 1

    log.info(
        "🤖 Evaluation complete: %d errors, %d warnings, %d info findings "
        "(%d offered, %d posted, %d duplicate, %d failed)",
        err_ct,
        warn_ct,
        info_ct,
        result.attempted,
        result.posted,
        result.duplicates,
        result.failed,
    )
    if result.total_failure:
        log.error(
            "🛑 Nothing reached the API: %d findings offered, 0 posted. Last error: %s",
            result.attempted,
            result.last_error,
        )
    return result
