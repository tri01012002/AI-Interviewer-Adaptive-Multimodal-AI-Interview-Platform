"""Temporary SQLAlchemy-backed interview persistence compatibility layer.

This preserves the existing API used by the interview routes until STEP 2
introduces formal repositories. The legacy state_json column is retained as a
compatibility snapshot; normalized interview records are written alongside it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from services.database import (
    InterviewQuestionRecord,
    InterviewRecord,
    InterviewTurnRecord,
    SessionLocal,
)


class InterviewStore:
    """Persist interviews through SQLAlchemy while preserving the old API."""

    def create(self, candidate_id: str, position: str, mode: str, state: dict[str, Any]) -> str:
        interview_id = str(uuid4())
        now = datetime.now(timezone.utc)
        state = dict(state)
        state.update({
            "interview_id": interview_id,
            "candidate_id": candidate_id,
            "position": position,
            "mode": mode,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        })
        with SessionLocal.begin() as session:
            interview = InterviewRecord(
                id=interview_id,
                candidate_id=candidate_id,
                position=position,
                mode=mode,
                current_question=state["current_question"],
                state_json=json.dumps(state),
            )
            session.add(interview)
            session.flush()
            session.add(InterviewQuestionRecord(
                interview_id=interview_id,
                sequence_number=1,
                question_text=state["current_question"],
                status="sent",
                generated_at=now,
                sent_at=now,
            ))
        return interview_id

    def get(self, interview_id: str) -> dict[str, Any] | None:
        with SessionLocal() as session:
            interview = session.get(InterviewRecord, interview_id)
            return None if interview is None else self._state(interview)

    def save(self, interview_id: str, state: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        state = dict(state)
        state["interview_id"] = interview_id
        state["updated_at"] = now.isoformat()
        with SessionLocal.begin() as session:
            interview = session.get(InterviewRecord, interview_id)
            if interview is None:
                raise KeyError(f"Interview not found: {interview_id}")

            previous_question = interview.current_question
            interview.current_question = state["current_question"]
            interview.state_json = json.dumps(state)
            interview.updated_at = now

            if state["current_question"] != previous_question:
                questions = session.execute(
                    select(InterviewQuestionRecord).where(
                        InterviewQuestionRecord.interview_id == interview_id
                    )
                ).scalars().all()
                next_sequence = len(questions) + 1
                question = InterviewQuestionRecord(
                    interview_id=interview_id,
                    sequence_number=next_sequence,
                    question_text=state["current_question"],
                    status="sent",
                    generated_at=now,
                    sent_at=now,
                )
                session.add(question)
                session.flush()
                history = state.get("history", [])
                answer = history[-1].get("answer") if history else None
                session.add(InterviewTurnRecord(
                    interview_id=interview_id,
                    turn_id=str(uuid4()),
                    sequence_number=next_sequence - 1,
                    status="completed",
                    question_id=question.id,
                    candidate_answer=answer,
                    started_at=now,
                    completed_at=now,
                ))
        return state

    def delete(self, interview_id: str) -> None:
        with SessionLocal.begin() as session:
            interview = session.get(InterviewRecord, interview_id)
            if interview is not None:
                session.delete(interview)

    def list(self) -> list[dict[str, Any]]:
        with SessionLocal() as session:
            interviews = session.execute(
                select(InterviewRecord).order_by(InterviewRecord.created_at.desc())
            ).scalars().all()
            return [self._state(interview) for interview in interviews]

    @staticmethod
    def _state(interview: InterviewRecord) -> dict[str, Any]:
        state = json.loads(interview.state_json)
        state.update({
            "interview_id": interview.id,
            "candidate_id": interview.candidate_id,
            "position": interview.position,
            "mode": interview.mode,
            "current_question": interview.current_question,
        })
        return state
