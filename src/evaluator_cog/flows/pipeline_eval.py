from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from mini_app_polis import logger as logger_mod
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database import get_db_session
from ..models import PipelineEvaluation as DbEval
from ..schemas import (
    Envelope,
    PipelineEvaluationItem,
    PrefectWebhookPayload,
    success_envelope,
)

router = APIRouter()
log = logger_mod.get_logger()


def _severity_for_state(state_type: str | None) -> str:
    state = (state_type or "").upper()
    if state == "CRASHED":
        return "ERROR"
    if state == "FAILED":
        return "WARN"
    return "INFO"


@router.post(
    "/prefect-webhook",
    response_model=Envelope[PipelineEvaluationItem],
    summary="Prefect flow webhook",
    description="Receive Prefect flow state payloads and create evaluation findings.",
)
async def prefect_webhook(
    payload: PrefectWebhookPayload = Body(..., embed=False),
    session: AsyncSession = Depends(get_db_session),
) -> Envelope[PipelineEvaluationItem]:
    settings = get_settings()

    flow_name = payload.flow_name or "unknown"
    state_name = payload.state_name or "unknown"
    state_type = payload.state_type or "unknown"
    finding = f"Flow {flow_name} entered {state_name} state"
    severity = _severity_for_state(state_type)
    standards_version = getattr(settings, "STANDARDS_VERSION", "6.0")

    if (
        payload.flow_name is None
        or payload.state_name is None
        or payload.state_type is None
    ):
        log.warning(
            (
                "Prefect webhook payload missing fields; using safe fallbacks. "
                "flow_name=%s state_name=%s state_type=%s"
            ),
            payload.flow_name,
            payload.state_name,
            payload.state_type,
        )

    row = DbEval(
        owner_id=settings.KAIANO_API_OWNER_ID,
        run_id=payload.flow_run_id,
        repo="deejay-set-processor-dev",
        dimension="pipeline_consistency",
        severity=severity,
        finding=finding,
        suggestion=None,
        standards_version=standards_version,
        source="prefect_webhook",
        flow_name=payload.flow_name,
        details=None,
    )
    session.add(row)
    await session.flush()
    await session.commit()
    await session.refresh(row)

    data = PipelineEvaluationItem(
        id=row.id,
        run_id=row.run_id,
        repo=row.repo,
        dimension=row.dimension,
        severity=row.severity,
        finding=row.finding or "",
        suggestion=row.suggestion,
        standards_version=row.standards_version,
        source=row.source,
        flow_name=row.flow_name,
        evaluated_at=row.evaluated_at,
    )
    return success_envelope(data, count=1, version=settings.API_VERSION)
