"""Persistence repositories for interview operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import (
    CandidateRecord,
    InterviewAssessmentRecord,
    InterviewCompetencyStateRecord,
    InterviewEvidenceRecord,
    InterviewQuestionRecord,
    InterviewRecord,
    InterviewTurnRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
)
from services.turn_state import validate_turn_transition


class InterviewRepository:
    """Persistence operations for interviews; transaction ownership stays with the caller."""

    def create(
        self,
        session: Session,
        state: dict[str, Any],
        owner_user_id: str | None,
        organization_id: str,
    ) -> InterviewRecord:
        interview = InterviewRecord(
            id=state["interview_id"],
            owner_user_id=owner_user_id,
            candidate_id=state["candidate_id"],
            organization_id=organization_id,
            campaign_id=state.get("campaign_id"),
            application_id=state.get("application_id"),
            schedule_id=state.get("schedule_id"),
            objective_version=state.get("objective_version"),
            plan_version=state.get("plan_version"),
            rubric_snapshot_json=(
                None
                if state.get("rubric_snapshot") is None
                else json.dumps(state["rubric_snapshot"], separators=(",", ":"))
            ),
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
            competency=state.get("current_competency"),
            difficulty=state.get("difficulty"),
            question_text=interview.current_question,
            status="sent",
            generated_at=now,
            sent_at=now,
        ))
        session.flush()
        return interview

    def get_authorized(
        self,
        session: Session,
        interview_id: str,
        user_id: str,
        role: str,
        organization_id: str,
    ) -> InterviewRecord | None:
        query = (
            select(InterviewRecord)
            .join(
                OrganizationRecord,
                OrganizationRecord.id == InterviewRecord.organization_id,
            )
            .where(
                InterviewRecord.id == interview_id,
                InterviewRecord.organization_id == organization_id,
                OrganizationRecord.status == "active",
            )
        )
        if role == "platform_admin":
            return session.execute(query).scalar_one_or_none()
        if role == "recruiter":
            query = (
                query.join(CandidateRecord, CandidateRecord.id == InterviewRecord.candidate_id)
                .join(
                    OrganizationMembershipRecord,
                    (OrganizationMembershipRecord.organization_id
                     == InterviewRecord.organization_id)
                    & (OrganizationMembershipRecord.user_id == user_id),
                )
                .where(
                    CandidateRecord.organization_id == organization_id,
                    InterviewRecord.owner_user_id == user_id,
                    CandidateRecord.owner_user_id == user_id,
                    OrganizationMembershipRecord.role == "recruiter",
                    OrganizationMembershipRecord.status == "active",
                )
            )
        elif role == "candidate":
            query = (
                query.join(CandidateRecord, CandidateRecord.id == InterviewRecord.candidate_id)
                .join(
                    OrganizationMembershipRecord,
                    (OrganizationMembershipRecord.organization_id
                     == InterviewRecord.organization_id)
                    & (OrganizationMembershipRecord.user_id == user_id),
                )
                .where(
                    CandidateRecord.organization_id == organization_id,
                    CandidateRecord.user_id == user_id,
                    OrganizationMembershipRecord.role == "candidate",
                    OrganizationMembershipRecord.status == "active",
                )
            )
        else:
            return None
        return session.execute(query).scalar_one_or_none()

    def list_authorized(
        self, session: Session, user_id: str, role: str, organization_id: str
    ) -> list[InterviewRecord]:
        query = (
            select(InterviewRecord)
            .join(
                OrganizationRecord,
                OrganizationRecord.id == InterviewRecord.organization_id,
            )
            .where(InterviewRecord.organization_id == organization_id)
            .where(OrganizationRecord.status == "active")
            .order_by(InterviewRecord.created_at.desc())
        )
        if role == "platform_admin":
            pass
        elif role == "recruiter":
            query = (
                query.join(CandidateRecord, CandidateRecord.id == InterviewRecord.candidate_id)
                .join(
                    OrganizationMembershipRecord,
                    (OrganizationMembershipRecord.organization_id
                     == InterviewRecord.organization_id)
                    & (OrganizationMembershipRecord.user_id == user_id),
                )
                .where(
                    CandidateRecord.organization_id == organization_id,
                    InterviewRecord.owner_user_id == user_id,
                    CandidateRecord.owner_user_id == user_id,
                    OrganizationMembershipRecord.role == "recruiter",
                    OrganizationMembershipRecord.status == "active",
                )
            )
        elif role == "candidate":
            query = (
                query.join(CandidateRecord, CandidateRecord.id == InterviewRecord.candidate_id)
                .join(
                    OrganizationMembershipRecord,
                    (OrganizationMembershipRecord.organization_id
                     == InterviewRecord.organization_id)
                    & (OrganizationMembershipRecord.user_id == user_id),
                )
                .where(
                    CandidateRecord.organization_id == organization_id,
                    CandidateRecord.user_id == user_id,
                    OrganizationMembershipRecord.role == "candidate",
                    OrganizationMembershipRecord.status == "active",
                )
            )
        else:
            return []
        return list(session.execute(query).scalars().all())

    @staticmethod
    def can_manage_candidate(
        session: Session,
        candidate_id: str,
        user_id: str,
        role: str,
        organization_id: str,
    ) -> bool:
        query = (
            select(CandidateRecord)
            .join(
                OrganizationRecord,
                OrganizationRecord.id == CandidateRecord.organization_id,
            )
            .where(
                CandidateRecord.id == candidate_id,
                CandidateRecord.organization_id == organization_id,
                OrganizationRecord.status == "active",
            )
        )
        if role in {"recruiter", "candidate"}:
            query = query.join(
                OrganizationMembershipRecord,
                (OrganizationMembershipRecord.organization_id
                 == CandidateRecord.organization_id)
                & (OrganizationMembershipRecord.user_id == user_id),
            ).where(
                OrganizationMembershipRecord.status == "active",
                OrganizationMembershipRecord.role == role,
            )
        candidate = session.execute(query).scalar_one_or_none()
        if candidate is None:
            return False
        if role == "platform_admin":
            return True
        if role == "recruiter":
            return candidate.owner_user_id == user_id
        if role == "candidate":
            return candidate.user_id == user_id
        return False

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
            competency=state.get("current_competency"),
            difficulty=state.get("difficulty"),
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
                ownership=item.ownership,
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
        updated_at = turn.updated_at or turn.started_at or datetime.now(UTC)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if updated_at > datetime.now(UTC) - timedelta(seconds=lease_seconds):
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
