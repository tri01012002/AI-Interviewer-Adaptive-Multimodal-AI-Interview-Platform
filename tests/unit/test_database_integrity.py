from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from services.database import (
    Base,
    InterviewCompetencyStateRecord,
    InterviewEvidenceRecord,
    InterviewQuestionRecord,
    InterviewRecord,
    InterviewTurnRecord,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()
    engine.dispose()


def interview(session):
    record = InterviewRecord(
        id="interview-1",
        candidate_id="candidate-1",
        position="AI Engineer",
        mode="text",
        current_question="Introduce yourself",
        state_json="{}",
    )
    session.add(record)
    session.commit()
    return record


def question(session):
    record = InterviewQuestionRecord(
        id="question-1",
        interview_id="interview-1",
        sequence_number=1,
        question_text="Introduce yourself",
        status="sent",
    )
    session.add(record)
    session.commit()
    return record


def test_turn_id_and_sequence_are_unique_per_interview(session):
    interview(session)
    question(session)
    timestamp = datetime.now(timezone.utc)
    session.add(InterviewTurnRecord(
        id="turn-1",
        interview_id="interview-1",
        turn_id="client-turn-1",
        sequence_number=1,
        question_id="question-1",
        status="completed",
        created_at=timestamp,
        updated_at=timestamp,
    ))
    session.commit()

    session.add(InterviewTurnRecord(
        id="turn-2",
        interview_id="interview-1",
        turn_id="client-turn-1",
        sequence_number=2,
        question_id="question-1",
        status="completed",
    ))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(InterviewTurnRecord(
        id="turn-3",
        interview_id="interview-1",
        turn_id="client-turn-2",
        sequence_number=1,
        question_id="question-1",
        status="completed",
    ))
    with pytest.raises(IntegrityError):
        session.commit()


def test_invalid_foreign_keys_are_rejected(session):
    session.add(InterviewTurnRecord(
        id="turn-invalid",
        interview_id="missing-interview",
        turn_id="client-turn-1",
        sequence_number=1,
        status="received",
    ))
    with pytest.raises(IntegrityError):
        session.commit()


def test_evidence_requires_existing_interview_and_turn(session):
    interview(session)
    session.add(InterviewEvidenceRecord(
        id="evidence-1",
        interview_id="interview-1",
        turn_id="missing-turn",
        competency="python",
        evidence_text="Used Python",
    ))
    with pytest.raises(IntegrityError):
        session.commit()


def test_competency_is_unique_per_interview(session):
    interview(session)
    session.add(InterviewCompetencyStateRecord(
        id="competency-1",
        interview_id="interview-1",
        competency="python",
    ))
    session.commit()
    session.add(InterviewCompetencyStateRecord(
        id="competency-2",
        interview_id="interview-1",
        competency="python",
    ))
    with pytest.raises(IntegrityError):
        session.commit()


def test_question_sequence_is_unique_per_interview(session):
    interview(session)
    question(session)
    session.add(InterviewQuestionRecord(
        id="question-2",
        interview_id="interview-1",
        sequence_number=1,
        question_text="Another question",
        status="pending",
    ))
    with pytest.raises(IntegrityError):
        session.commit()