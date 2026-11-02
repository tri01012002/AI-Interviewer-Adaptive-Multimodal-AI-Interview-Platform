"""Interview API routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from services.interview_service import (
    CampaignInterviewNotReadyError,
    InterviewConflictError,
    InterviewEvaluationError,
    InterviewForbiddenError,
    InterviewNotFoundError,
    InterviewProcessingError,
    InterviewService,
    InterviewUnavailableError,
)
from services.report_service import ReportService
from services.support_access_audit import (
    DuplicateAuditRequest,
    MandatoryAuditUnavailable,
    audit_request_id,
    persist_required_audit,
)
from services.tenant_authorization import TenantContext, tenant_context

router = APIRouter()

service = InterviewService()
report_service = ReportService()


class InterviewStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(..., min_length=1)
    position: str = Field(..., min_length=1)
    mode: str = "text"


class CampaignInterviewStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_id: str = Field(..., min_length=1)
    mode: str = "text"


class InterviewAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)


@router.post("/interview/start", tags=["Interview"])
async def start_interview(
    payload: InterviewStartRequest,
    context: TenantContext = Depends(tenant_context),
) -> dict[str, Any]:
    if context.role in {"org_admin", "platform_admin"}:
        raise HTTPException(status_code=404, detail="Candidate not found")
    try:
        return service.start_interview(
            payload.candidate_id,
            payload.position,
            payload.mode,
            context.actor_user_id,
            context.role,
            context.organization_id,
        )
    except InterviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found") from exc


@router.post(
    "/interview/campaigns/{campaign_id}/applications/{application_id}/start",
    tags=["Interview"],
)
async def start_campaign_interview(
    campaign_id: str,
    application_id: str,
    payload: CampaignInterviewStartRequest,
    context: TenantContext = Depends(tenant_context),
) -> dict[str, Any]:
    try:
        return service.start_campaign_interview(
            campaign_id=campaign_id,
            application_id=application_id,
            schedule_id=payload.schedule_id,
            mode=payload.mode,
            owner_user_id=context.actor_user_id,
            owner_role=context.role,
            organization_id=context.organization_id,
        )
    except CampaignInterviewNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except InterviewNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found",
        ) from exc


@router.post("/interview/{interview_id}/answer", tags=["Interview"])
async def submit_answer(
    interview_id: str,
    payload: InterviewAnswerRequest,
    context: TenantContext = Depends(tenant_context),
) -> dict[str, Any]:
    try:
        return service.submit_answer(
            interview_id,
            payload.answer,
            payload.turn_id,
            context.actor_user_id,
            context.role,
            context.organization_id,
        )
    except InterviewEvaluationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Answer evaluation is temporarily unavailable; retry with the same turn_id.",
        ) from exc
    except InterviewForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except (InterviewConflictError, InterviewProcessingError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InterviewUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except InterviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found") from exc


@router.get("/interview/{interview_id}", tags=["Interview"])
async def get_interview(
    interview_id: str,
    context: TenantContext = Depends(tenant_context),
) -> dict[str, Any]:
    state = service.get_interview(
        interview_id,
        context.actor_user_id,
        context.role,
        context.organization_id,
    )
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found")
    return state


@router.get("/interviews", tags=["Interview"])
async def list_interviews(
    context: TenantContext = Depends(tenant_context),
) -> list[dict[str, Any]]:
    return service.list_interviews(
        context.actor_user_id, context.role, context.organization_id
    )


@router.post("/interview/{interview_id}/report", tags=["Interview"])
async def export_report(
    request: Request,
    interview_id: str,
    format: Literal["json", "txt"] = "json",
    context: TenantContext = Depends(tenant_context),
) -> dict[str, Any]:
    if not getattr(request.state, "mandatory_audit_request_id", None):
        try:
            request_id = audit_request_id(
                request,
                context.actor_user_id,
                context.organization_id,
            )
            persist_required_audit(
                request_id=request_id,
                actor_user_id=context.actor_user_id,
                organization_id=context.organization_id,
                requested_organization_id=context.organization_id,
                action="report_export_attempt",
                resource_type="interview",
                resource_id=interview_id,
                support_reason=context.support_reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Idempotency-Key") from exc
        except DuplicateAuditRequest as exc:
            raise HTTPException(
                status_code=409,
                detail="This protected request was already attempted",
            ) from exc
        except MandatoryAuditUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail="Required access audit is temporarily unavailable",
            ) from exc

    state = service.get_interview(
        interview_id,
        context.actor_user_id,
        context.role,
        context.organization_id,
    )
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found")
    report = report_service.build_report(
        state.get("candidate_id", "unknown"),
        interview_id,
        organization_id=context.organization_id,
    )
    report_path = report_service.write_report(report, format)
    return {
        "interview_id": interview_id,
        "report_file": Path(report_path).name,
        "format": format,
        "report": report,
    }
