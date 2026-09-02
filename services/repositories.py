"""Persistence repositories for interview operations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import (
    InterviewAssessmentRecord,
    InterviewCompetencyStateRecord,
    InterviewEvidenceRecord,
    InterviewQuestionRecord,
    InterviewRecord,
    InterviewTurnRecord,
)
from services.turn_state import validate_turn_transition


class InterviewRepository:
    """Persistence operations for interviews; transaction ownership stays with the caller."""

    def create(self, session: Session, state: dict[str, Any], owner_user_id: str | None = None) -> InterviewRecord:
        interview = InterviewRecord(
            id=state["interview_id"],
            owner_user_id=owner_user_id,
            candidate_id=state["candidate_id"],
            position=state["position"],
            mode=state.get("mode", "text"),
            current_question=state["current_question"],
            state_json=json.dumps(state),
        )
        session.add(interview)
        session.flush()
        now = datetime.fromisoformat(state["created_at"])
        session.add(InterviewQuestionRecord(
            interview_id=interview.id,
            sequence_number=1,
            question_text=interview.current_question,
            status="sent",
            generated_at=now,
            sent_at=now,
        ))
        session.flush()
        return interview

    def get(self, session: Session, interview_id: str) -> InterviewRecord | None:
        return session.get(InterviewRecord, interview_id)

    def get_authorized(
        self, session: Session, interview_id: str, user_id: str, role: str
    ) -> InterviewRecord | None:
        query = select(InterviewRecord).where(InterviewRecord.id == interview_id)
        if role not in {"admin", "recruiter"}:
            query = query.where(InterviewRecord.owner_user_id == user_id)
        return session.execute(query).scalar_one_or_none()

    def list_authorized(self, session: Session, user_id: str, role: str) -> list[InterviewRecord]:
        query = select(InterviewRecord).order_by(InterviewRecord.created_at.desc())
        if role not in {"admin", "recruiter"}:
            query = query.where(InterviewRecord.owner_user_id == user_id)
        return list(session.execute(query).scalars().all())

    def list(self, session: Session) -> list[InterviewRecord]:
        return list(session.execute(
            select(InterviewRecord).order_by(InterviewRecord.created_at.desc())
        ).scalars().all())

    def save_state(
        self,
        session: Session,
        interview: InterviewRecord,
        state: dict[str, Any],
        turn: InterviewTurnRecord | None = None,
    ) -> None:
        now = datetime.fromisoformat(state["updated_at"])
        previous_question = interview.current_question
        interview.current_question = state["current_question"]
        interview.state_json = json.dumps(state)
        interview.updated_at = now

        if state["current_question"] == previous_question:
            if turn is not None:
                validate_turn_transition(turn.status, "completed")
                turn.status = "completed"
                turn.completed_at = now
            return

        question_count = session.scalar(
            select(func.count(InterviewQuestionRecord.id)).where(
                InterviewQuestionRecord.interview_id == interview.id
            )
        ) or 0
        question = InterviewQuestionRecord(
            interview_id=interview.id,
            sequence_number=question_count + 1,
            question_text=state["current_question"],
            status="sent",
            generated_at=now,
            sent_at=now,
        )
        session.add(question)
        session.flush()

        if turn is None:
            turn_count = session.scalar(
                select(func.count(InterviewTurnRecord.id)).where(
                    InterviewTurnRecord.interview_id == interview.id
                )
            ) or 0
            history = state.get("history", [])
            answer = history[-1].get("answer") if history else None
            turn = InterviewTurnRecord(
                interview_id=interview.id,
                turn_id=str(uuid4()),
                sequence_number=turn_count + 1,
                status="received",
                candidate_answer=answer,
                started_at=now,
            )
            session.add(turn)
        turn.question_id = question.id
        validate_turn_transition(turn.status, "completed")
        turn.status = "completed"
        turn.updated_at = now
        turn.completed_at = now


class QuestionRepository:
    """Question persistence kept separate from interview orchestration."""

    def list_for_interview(self, session: Session, interview_id: str) -> list[InterviewQuestionRecord]:
        return list(session.execute(
            select(InterviewQuestionRecord)
            .where(InterviewQuestionRecord.interview_id == interview_id)
            .order_by(InterviewQuestionRecord.sequence_number)
        ).scalars().all())


class EvaluationRepository:
    """Durable evidence and assessment writes owned by the caller's transaction."""

    def has_assessment(self, session: Session, interview_id: str, turn_record_id: str) -> bool:
        return session.scalar(
            select(func.count(InterviewAssessmentRecord.id)).where(
                InterviewAssessmentRecord.interview_id == interview_id,
                InterviewAssessmentRecord.turn_id == turn_record_id,
            )
        ) > 0

    def save_result(
        self,
        session: Session,
        interview_id: str,
        turn: InterviewTurnRecord,
        question_id: str | None,
        result: Any,
    ) -> None:
        assessment = result.assessment
        evidence_ids: list[str] = []
        for item in result.evidence:
            evidence = InterviewEvidenceRecord(
                interview_id=interview_id,
                turn_id=turn.id,
                question_id=question_id,
                competency=item.competency,
                evidence_text=item.text,
                evidence_type=item.evidence_type,
                strength=item.strength.value,
                specificity=item.specificity,
                relevance=item.relevance,
                evaluator_type=assessment.evaluator_type,
                evaluator_version=assessment.evaluator_version,
            )
            session.add(evidence)
            session.flush()
            evidence_ids.append(evidence.id)

        assessment_record = InterviewAssessmentRecord(
            interview_id=interview_id,
            turn_id=turn.id,
            competency=assessment.competency,
            score=assessment.score,
            status=assessment.status.value,
            evidence_strength=assessment.evidence_strength.value,
            confidence=assessment.confidence,
            strengths_json=json.dumps(assessment.strengths),
            gaps_json=json.dumps(assessment.gaps),
            rationale=assessment.rationale,
            evaluator_type=assessment.evaluator_type,
            evaluator_version=assessment.evaluator_version,
        )
        session.add(assessment_record)
        competency = session.execute(
            select(InterviewCompetencyStateRecord).where(
                InterviewCompetencyStateRecord.interview_id == interview_id,
                InterviewCompetencyStateRecord.competency == assessment.competency,
            )
        ).scalar_one_or_none()
        if competency is None:
            competency = InterviewCompetencyStateRecord(
                interview_id=interview_id,
                competency=assessment.competency,
            )
            session.add(competency)
        competency.evidence_count = (competency.evidence_count or 0) + len(evidence_ids)
        competency.confidence = assessment.confidence
        competency.strength = assessment.status.value


class TurnRepository:
    """Turn persistence queries for application services and recovery workflows."""

    def list_for_interview(self, session: Session, interview_id: str) -> list[InterviewTurnRecord]:
        return list(session.execute(
            select(InterviewTurnRecord)
            .where(InterviewTurnRecord.interview_id == interview_id)
            .order_by(InterviewTurnRecord.sequence_number)
        ).scalars().all())

    def get_by_client_id(self, session: Session, interview_id: str, turn_id: str) -> InterviewTurnRecord | None:
        return session.execute(
            select(InterviewTurnRecord).where(
                InterviewTurnRecord.interview_id == interview_id,
                InterviewTurnRecord.turn_id == turn_id,
            )
        ).scalar_one_or_none()

    def transition(self, turn: InterviewTurnRecord, target_status: str) -> None:
        from services.turn_state import validate_turn_transition

        validate_turn_transition(turn.status, target_status)
        turn.status = target_status
        turn.updated_at = datetime.now()

    def reclaim_if_stale(self, turn: InterviewTurnRecord, lease_seconds: int) -> bool:
        if turn.status != "processing":
            return False
        updated_at = turn.updated_at or turn.started_at or datetime.now(timezone.utc)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if updated_at > datetime.now(timezone.utc) - timedelta(seconds=lease_seconds):
            return False
        self.transition(turn, "failed_retryable")
        self.transition(turn, "processing")
        return True

    def create_processing(
        self, session: Session, interview_id: str, turn_id: str, answer: str
    ) -> InterviewTurnRecord:
        sequence_number = (session.scalar(
            select(func.count(InterviewTurnRecord.id)).where(
                InterviewTurnRecord.interview_id == interview_id
            )
        ) or 0) + 1
        turn = InterviewTurnRecord(
            interview_id=interview_id,
            turn_id=turn_id,
            sequence_number=sequence_number,
            status="received",
            candidate_answer=answer,
            started_at=datetime.now(),
        )
        session.add(turn)
        session.flush()
        self.transition(turn, "processing")
        return turn