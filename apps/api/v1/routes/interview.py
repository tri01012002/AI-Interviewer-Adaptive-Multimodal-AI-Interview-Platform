"""Interview API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from agents.interview_agent.graph import InterviewAgentCore
from evaluation.service import EvaluationService
from rag.service import RAGQuestionService
from services.interview_store import InterviewStore
from services.report_service import ReportService

router = APIRouter()

agent = InterviewAgentCore()
store = InterviewStore()
evaluator = EvaluationService()
rag = RAGQuestionService()
report_service = ReportService()


class InterviewStartRequest(BaseModel):
    candidate_id: str = Field(..., min_length=1)
    position: str = Field(..., min_length=1)
    mode: str = "text"


class InterviewAnswerRequest(BaseModel):
    answer: str = Field(..., min_length=1)


@router.post("/interview/start", tags=["Interview"])
async def start_interview(payload: InterviewStartRequest) -> dict[str, Any]:
    state = agent.start_interview(candidate_id=payload.candidate_id, position=payload.position)
    interview_id = store.create(payload.candidate_id, payload.position, payload.mode, state)
    state["interview_id"] = interview_id
    state["mode"] = payload.mode
    store.save(interview_id, state)
    return state


@router.post("/interview/{interview_id}/answer", tags=["Interview"])
async def submit_answer(interview_id: str, payload: InterviewAnswerRequest) -> dict[str, Any]:
    current_state = store.get(interview_id)
    if current_state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found")

    updated_state = agent.handle_answer(current_state, payload.answer)
    updated_state["follow_up_questions"] = rag.retrieve_relevant_questions(
        updated_state.get("position", ""), updated_state.get("skills", {})
    )
    evaluation = evaluator.evaluate_answer(payload.answer, updated_state.get("skills", {}), updated_state.get("position", ""))
    updated_state["evaluation"] = evaluation
    updated_state["overall_score"] = evaluation["overall_score"]
    updated_state["current_question"] = rag.generate_follow_up_question(
        updated_state.get("position", ""), payload.answer, updated_state.get("skills", {})
    )
    store.save(interview_id, updated_state)
    return updated_state


@router.get("/interview/{interview_id}", tags=["Interview"])
async def get_interview(interview_id: str) -> dict[str, Any]:
    state = store.get(interview_id)
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found")
    return state


@router.get("/interviews", tags=["Interview"])
async def list_interviews() -> list[dict[str, Any]]:
    return store.list()


@router.post("/interview/{interview_id}/report", tags=["Interview"])
async def export_report(interview_id: str, format: str = "json") -> dict[str, Any]:
    state = store.get(interview_id)
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found")
    report_path = report_service.export_report(state.get("candidate_id", "unknown"), interview_id, state, format)
    return {"interview_id": interview_id, "report_path": report_path, "format": format}
