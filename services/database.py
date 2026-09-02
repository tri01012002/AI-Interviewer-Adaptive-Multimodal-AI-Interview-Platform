"""PostgreSQL-compatible database schema definitions for the interview platform."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from config import settings

from sqlalchemy import Float, ForeignKey, Index, Integer, Text

def _resolve_database_url() -> str:
    database_url = getattr(settings, "DATABASE_URL", "")
    if database_url and database_url.startswith("postgresql"):
        return database_url

    storage_dir = Path(__file__).resolve().parent.parent / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    db_path = storage_dir / "ai_interviewer.db"
    return f"sqlite:///{db_path.resolve()}"


class Base(DeclarativeBase):
    pass


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class CandidateRecord(Base):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    resume_url: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class InterviewRecord(Base):
    __tablename__ = "interviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    candidate_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    position: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="text")
    current_question: Mapped[str] = mapped_column(String, nullable=False)
    state_json: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class InterviewTurnRecord(Base):
    """Represents a single turn in an interview (question + answer pair)"""
    __tablename__ = "interview_turns"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    interview_id: Mapped[str] = mapped_column(String, ForeignKey("interviews.id"), nullable=False, index=True)
    turn_id: Mapped[str] = mapped_column(String, nullable=False)  # Client-stable ID
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="received")  # received, processing, completed, error
    question_id: Mapped[str | None] = mapped_column(String, ForeignKey("interview_questions.id"), nullable=True)
    candidate_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    __table_args__ = (
        Index("ix_interview_turns_interview_id_turn_id", "interview_id", "turn_id", unique=True),
        Index("ix_interview_turns_interview_id_sequence", "interview_id", "sequence_number", unique=True),
        Index("ix_interview_turns_interview_id_status", "interview_id", "status"),
    )


class InterviewQuestionRecord(Base):
    """Represents a question asked during an interview"""
    __tablename__ = "interview_questions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    interview_id: Mapped[str] = mapped_column(String, ForeignKey("interviews.id"), nullable=False, index=True)
    turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    competency: Mapped[str | None] = mapped_column(String, nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String, nullable=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[str] = mapped_column(String, nullable=False, default="free_form")  # free_form, multiple_choice
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")  # pending, sent, answered, cancelled
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    __table_args__ = (
        Index("ix_interview_questions_interview_id_sequence", "interview_id", "sequence_number", unique=True),
        Index("ix_interview_questions_interview_id_status", "interview_id", "status"),
    )


class InterviewEvidenceRecord(Base):
    """Evidence extracted from candidate answers"""
    __tablename__ = "interview_evidence"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    interview_id: Mapped[str] = mapped_column(String, ForeignKey("interviews.id"), nullable=False, index=True)
    turn_id: Mapped[str] = mapped_column(String, ForeignKey("interview_turns.id"), nullable=False, index=True)
    competency: Mapped[str] = mapped_column(String, nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    strength: Mapped[str | None] = mapped_column(String, nullable=True)  # strong, medium, weak
    specificity: Mapped[str | None] = mapped_column(String, nullable=True)  # high, medium, low
    ownership: Mapped[str | None] = mapped_column(String, nullable=True)  # explicit, implicit, none
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    __table_args__ = (
        Index("ix_interview_evidence_interview_id_competency", "interview_id", "competency"),
    )


class InterviewCompetencyStateRecord(Base):
    """Tracks competency assessment state for an interview"""
    __tablename__ = "interview_competency_state"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    interview_id: Mapped[str] = mapped_column(String, ForeignKey("interviews.id"), nullable=False, index=True)
    competency: Mapped[str] = mapped_column(String, nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)  # 0.0 to 1.0
    strength: Mapped[str] = mapped_column(String, nullable=False, default="unknown")  # strong, medium, weak, unknown
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    __table_args__ = (
        Index("ix_interview_competency_state_interview_id_competency", "interview_id", "competency", unique=True),
        Index("ix_interview_competency_state_interview_id_confidence", "interview_id", "confidence"),
    )

engine = create_engine(_resolve_database_url(), future=True, echo=settings.SQLALCHEMY_ECHO)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    """Create tables for explicitly controlled local/test bootstrap only.

    Application startup uses Alembic migrations instead of this helper.
    """
    Base.metadata.create_all(bind=engine)




def get_db_session() -> Session:
    return SessionLocal()
