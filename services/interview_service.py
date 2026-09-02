"""Application service for interview lifecycle operations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agents.interview_agent.graph import InterviewAgentCore
from evaluation.service import EvaluationService, Rubric
from rag.service import RAGQuestionService
from services.database import SessionLocal
from services.repositories import EvaluationRepository, InterviewRepository, QuestionRepository, TurnRepository
from services.turn_state import TurnStatus
from config import settings
from integrations.llm_providers.provider import provider_from_settings


class InterviewNotFoundError(LookupError):
    """Raised when an interview does not exist."""


class InterviewForbiddenError(PermissionError):
    """Raised when a user is authenticated but cannot perform an operation."""


class InterviewService:
    """Coordinates interview decisions and persistence within explicit transactions."""

    def __init__(
        self,
        agent: InterviewAgentCore | None = None,
        evaluator: EvaluationService | None = None,
        rag: RAGQuestionService | None = None,
        interview_repository: InterviewRepository | None = None,
        turn_repository: TurnRepository | None = None,
        evaluation_repository: EvaluationRepository | None = None,
    ) -> None:
        self.rag = rag or RAGQuestionService()
        self.agent = agent or InterviewAgentCore(
            llm_provider=provider_from_settings(settings),
            context_retriever=self._retrieve_graph_context,
        )
        self.evaluator = evaluator or EvaluationService()
        self.interviews = interview_repository or InterviewRepository()
        self.turns = turn_repository or TurnRepository()
        self.evaluations = evaluation_repository or EvaluationRepository()
        self.questions = QuestionRepository()

    def start_interview(
        self, candidate_id: str, position: str, mode: str = "text", owner_user_id: str | None = None
    ) -> dict[str, Any]:
        state = self.agent.start_interview(candidate_id=candidate_id, position=position)
        interview_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        state.update({"interview_id": interview_id, "mode": mode, "created_at": now, "updated_at": now})
        with SessionLocal.begin() as session:
            self.interviews.create(session, state, owner_user_id)
        return state

    def get_interview(self, interview_id: str, user_id: str | None = None, role: str = "candidate") -> dict[str, Any] | None:
        with SessionLocal() as session:
            interview = self.interviews.get_authorized(session, interview_id, user_id or "", role)
            return None if interview is None else self._state(interview)

    def list_interviews(self, user_id: str | None = None, role: str = "candidate") -> list[dict[str, Any]]:
        with SessionLocal() as session:
            return [self._state(interview) for interview in self.interviews.list_authorized(session, user_id or "", role)]

    def submit_answer(
        self, interview_id: str, answer: str, turn_id: str, user_id: str | None = None, role: str = "candidate"
    ) -> dict[str, Any]:
        if role == "recruiter":
            raise InterviewForbiddenError("Recruiters have read-only interview access")
        try:
            with SessionLocal.begin() as session:
                interview = self.interviews.get_authorized(session, interview_id, user_id or "", role)
                if interview is None:
                    raise InterviewNotFoundError(interview_id)
                existing_turn = self.turns.get_by_client_id(session, interview_id, turn_id)
                if existing_turn is not None:
                    if existing_turn.status == TurnStatus.FAILED_RETRYABLE:
                        self.turns.transition(existing_turn, TurnStatus.PROCESSING)
                        turn = existing_turn
                    elif self.turns.reclaim_if_stale(existing_turn, settings.TURN_PROCESSING_LEASE_SECONDS):
                        turn = existing_turn
                    else:
                        return self._state(interview)
                else:
                    turn = self.turns.create_processing(session, interview_id, turn_id, answer)
        except InterviewNotFoundError:
            raise

        except IntegrityError:
            with SessionLocal() as session:
                interview = self.interviews.get_authorized(session, interview_id, user_id or "", role)
                if interview is None:
                    raise InterviewNotFoundError(interview_id)
                return self._state(interview)

        try:
            with SessionLocal.begin() as session:
                interview = self.interviews.get_authorized(session, interview_id, user_id or "", role)
                if interview is None:
                    raise InterviewNotFoundError(interview_id)
                persisted_turn = self.turns.get_by_client_id(session, interview_id, turn_id)
                if persisted_turn is None:
                    raise InterviewNotFoundError(interview_id)
                state = self._state(interview)
                if self.evaluations.has_assessment(session, interview_id, persisted_turn.id):
                    if persisted_turn.status == TurnStatus.PROCESSING:
                        self.turns.transition(persisted_turn, TurnStatus.COMPLETED)
                    return state
                updated_state = self.agent.handle_answer(state, answer, turn_id)
                updated_state["follow_up_questions"] = self.rag.retrieve_relevant_questions(
                    updated_state.get("position", ""), updated_state.get("skills", {})
                )
                evaluation = self.evaluator.evaluate_answer(
                    answer, updated_state.get("skills", {}), updated_state.get("position", "")
                )
                competencies = list(updated_state.get("skills", {})) or ["general"]
                questions = self.questions.list_for_interview(session, interview_id)
                question_id = questions[-1].id if questions else None
                for competency in competencies:
                    result = self.evaluator.evaluator.evaluate(
                        state.get("current_question", ""),
                        answer,
                        competency,
                        Rubric(competency=competency),
                    )
                    self.evaluations.save_result(session, interview_id, persisted_turn, question_id, result)
                updated_state["evaluation"] = evaluation
                updated_state["overall_score"] = evaluation["overall_score"]
                updated_state["current_question"] = self.rag.generate_follow_up_question(
                    updated_state.get("position", ""), answer, updated_state.get("skills", {})
                )
                updated_state["updated_at"] = datetime.now(timezone.utc).isoformat()
                self.interviews.save_state(session, interview, updated_state, persisted_turn)
                return updated_state
        except Exception:
            with SessionLocal.begin() as session:
                failed_turn = self.turns.get_by_client_id(session, interview_id, turn_id)
                if failed_turn is not None and failed_turn.status == TurnStatus.PROCESSING:
                    self.turns.transition(failed_turn, TurnStatus.FAILED_RETRYABLE)
            raise

    def _retrieve_graph_context(self, state: dict[str, Any]) -> str:
        result = self.rag.retrieve_context(
            str(state.get("position", "")),
            state.get("current_competency"),
            state.get("identified_gaps", []),
        )
        return self.rag.format_context(result)

    @staticmethod
    def _state(interview: Any) -> dict[str, Any]:
        state = json.loads(interview.state_json)
        state.update({
            "interview_id": interview.id,
            "candidate_id": interview.candidate_id,
            "position": interview.position,
            "mode": interview.mode,
            "current_question": interview.current_question,
        })
        return state