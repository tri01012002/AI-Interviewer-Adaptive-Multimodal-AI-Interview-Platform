"""Interview API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from services.interview_service import InterviewForbiddenError, InterviewNotFoundError, InterviewService
from services.auth_service import current_user
from services.report_service import ReportService

router = APIRouter()

service = InterviewService()
report_service = ReportService()


class InterviewStartRequest(BaseModel):
    candidate_id: str = Field(..., min_length=1)
    position: str = Field(..., min_length=1)
    mode: str = "text"


class InterviewAnswerRequest(BaseModel):
    turn_id: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)


@router.post("/interview/start", tags=["Interview"], dependencies=[Depends(current_user)])
async def start_interview(payload: InterviewStartRequest, user=Depends(current_user)) -> dict[str, Any]:
    return service.start_interview(payload.candidate_id, payload.position, payload.mode, user["id"])


@router.post("/interview/{interview_id}/answer", tags=["Interview"], dependencies=[Depends(current_user)])
async def submit_answer(interview_id: str, payload: InterviewAnswerRequest, user=Depends(current_user)) -> dict[str, Any]:
    try:
        return service.submit_answer(interview_id, payload.answer, payload.turn_id, user["id"], user["role"])
    except InterviewForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InterviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found") from exc


@router.get("/interview/{interview_id}", tags=["Interview"], dependencies=[Depends(current_user)])
async def get_interview(interview_id: str, user=Depends(current_user)) -> dict[str, Any]:
    state = service.get_interview(interview_id, user["id"], user["role"])
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found")
    return state


@router.get("/interviews", tags=["Interview"], dependencies=[Depends(current_user)])
async def list_interviews(user=Depends(current_user)) -> list[dict[str, Any]]:
    return service.list_interviews(user["id"], user["role"])


@router.post("/interview/{interview_id}/report", tags=["Interview"], dependencies=[Depends(current_user)])
async def export_report(interview_id: str, format: str = "json", user=Depends(current_user)) -> dict[str, Any]:
    state = service.get_interview(interview_id, user["id"], user["role"])
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found")
    report_path = report_service.export_report(state.get("candidate_id", "unknown"), interview_id, state, format)
    return {"interview_id": interview_id, "report_path": report_path, "format": format}
