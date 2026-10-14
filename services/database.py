"""PostgreSQL-compatible database schema definitions for the interview platform."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from config import settings


def _resolve_database_url() -> str:
    database_url = getattr(settings, "DATABASE_URL", "")
    if database_url and database_url.startswith("postgresql"):
        return database_url
    default_sqlite_url = "sqlite:///./storage/ai_interviewer.db"
    if (
        database_url
        and database_url.startswith("sqlite:")
        and database_url != default_sqlite_url
    ):
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
    role: Mapped[str] = mapped_column(
        String, default="candidate", server_default="candidate"
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class OrganizationRecord(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended')", name="ck_organizations_status"),
    )


class OrganizationMembershipRecord(Base):
    __tablename__ = "organization_memberships"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, ForeignKey("organizations.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    granted_by_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_organization_memberships_org_user"),
        CheckConstraint(
            "role IN ('org_admin', 'recruiter', 'candidate')",
            name="ck_organization_memberships_role",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_organization_memberships_status",
        ),
        Index(
            "ix_organization_memberships_org_status_role",
            "organization_id",
            "status",
            "role",
        ),
    )


class CandidateInvitationRecord(Base):
    __tablename__ = "candidate_invitations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    candidate_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("candidates.id"), nullable=True, index=True
    )
    recruiter_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, index=True
    )
    organization_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("organizations.id"), nullable=True, index=True
    )
    candidate_name: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        Index(
            "uq_candidate_invitations_one_pending_per_candidate",
            "candidate_id",
            unique=True,
            sqlite_where=(consumed_at.is_(None) & revoked_at.is_(None)),
            postgresql_where=(consumed_at.is_(None) & revoked_at.is_(None)),
        ),
        Index(
            "ix_candidate_invitations_recruiter_created",
            "recruiter_user_id",
            "created_at",
        ),
        Index(
            "uq_candidate_invitations_one_pending_per_org_email",
            "organization_id",
            "email",
            unique=True,
            sqlite_where=(
                organization_id.is_not(None)
                & consumed_at.is_(None)
                & revoked_at.is_(None)
            ),
            postgresql_where=(
                organization_id.is_not(None)
                & consumed_at.is_(None)
                & revoked_at.is_(None)
            ),
        ),
        ForeignKeyConstraint(
            ["organization_id", "recruiter_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_candidate_invitations_org_recruiter_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_candidate_invitations_org_candidate",
        ),
    )


class CandidateRecord(Base):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    organization_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("organizations.id"), nullable=True, index=True
    )
    owner_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    resume_url: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_candidates_org_email"),
        UniqueConstraint("organization_id", "id", name="uq_candidates_org_id"),
        UniqueConstraint("organization_id", "user_id", name="uq_candidates_org_user"),
        ForeignKeyConstraint(
            ["organization_id", "owner_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_candidates_org_owner_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_candidates_org_user_membership",
        ),
    )


class AccessAuditRecord(Base):
    __tablename__ = "access_audit"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    actor_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    organization_id: Mapped[str | None] = mapped_column(String, ForeignKey("organizations.id"), nullable=True, index=True)
    requested_organization_id: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False, default="allowed")
    support_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    __table_args__ = (
        Index("uq_access_audit_request_id", "request_id", unique=True),
    )


class InterviewRecord(Base):
    __tablename__ = "interviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    candidate_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    organization_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("organizations.id"), nullable=True, index=True
    )
    position: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="text")
    current_question: Mapped[str] = mapped_column(String, nullable=False)
    state_json: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_interviews_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_interviews_org_candidate",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_interviews_org_owner_membership",
        ),
    )


class InterviewTurnRecord(Base):
    """Represents a single turn in an interview (question + answer pair)"""
    __tablename__ = "interview_turns"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    interview_id: Mapped[str] = mapped_column(String, ForeignKey("interviews.id"), nullable=False, index=True)
    turn_id: Mapped[str] = mapped_column(String, nullable=False)  # Client-stable ID
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="received")
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
    question_id: Mapped[str | None] = mapped_column(String, ForeignKey("interview_questions.id"), nullable=True)
    competency: Mapped[str] = mapped_column(String, nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String, nullable=False, default="candidate_statement")
    strength: Mapped[str | None] = mapped_column(String, nullable=True)  # strong, medium, weak
    specificity: Mapped[str | None] = mapped_column(String, nullable=True)  # high, medium, low
    ownership: Mapped[str | None] = mapped_column(String, nullable=True)  # explicit, implicit, none
    relevance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evaluator_type: Mapped[str] = mapped_column(String, nullable=False, default="deterministic")
    evaluator_version: Mapped[str] = mapped_column(String, nullable=False, default="phase-6-v1")
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


class InterviewAssessmentRecord(Base):
    """Immutable-per-turn competency assessment produced from persisted evidence."""
    __tablename__ = "interview_assessments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    interview_id: Mapped[str] = mapped_column(String, ForeignKey("interviews.id"), nullable=False, index=True)
    turn_id: Mapped[str] = mapped_column(String, ForeignKey("interview_turns.id"), nullable=False, index=True)
    competency: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="not_assessed")
    evidence_strength: Mapped[str] = mapped_column(String, nullable=False, default="weak")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    strengths_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    gaps_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evaluator_type: Mapped[str] = mapped_column(String, nullable=False, default="deterministic")
    evaluator_version: Mapped[str] = mapped_column(String, nullable=False, default="phase-6-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_interview_assessments_interview_turn_competency", "interview_id", "turn_id", "competency", unique=True),
    )


class RAGDocumentRecord(Base):
    """Durable, versioned text chunks and optional semantic embeddings."""

    __tablename__ = "rag_documents"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(String, nullable=False)
    chunk_id: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str] = mapped_column(String, nullable=False)
    competency_id: Mapped[str | None] = mapped_column(String, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    organization_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scope: Mapped[str] = mapped_column(
        String, nullable=False, default="unclassified", server_default="unclassified"
    )
    scope_approval_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    language: Mapped[str] = mapped_column(String, nullable=False, default="en")
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    embedding_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    __table_args__ = (
        CheckConstraint(
            "scope IN ('organization', 'global', 'unclassified')",
            name="ck_rag_documents_scope",
        ),
        CheckConstraint(
            "(scope = 'organization' AND organization_id IS NOT NULL "
            "AND trim(organization_id) <> '') OR "
            "(scope = 'global' AND organization_id IS NULL) OR "
            "scope = 'unclassified'",
            name="ck_rag_documents_scope_organization",
        ),
        CheckConstraint(
            "(scope = 'global' AND scope_approval_reference IS NOT NULL "
            "AND trim(scope_approval_reference) <> '') OR "
            "(scope IN ('organization', 'unclassified') "
            "AND scope_approval_reference IS NULL)",
            name="ck_rag_documents_scope_approval",
        ),
        Index(
            "uq_rag_document_chunk_org_scope_version",
            "organization_id",
            "document_id",
            "chunk_id",
            "version",
            unique=True,
            postgresql_where=(scope == "organization"),
            sqlite_where=(scope == "organization"),
        ),
        Index(
            "uq_rag_document_chunk_nontenant_scope_version",
            "scope",
            "document_id",
            "chunk_id",
            "version",
            unique=True,
            postgresql_where=(scope.in_(("global", "unclassified"))),
            sqlite_where=(scope.in_(("global", "unclassified"))),
        ),
        Index(
            "ix_rag_document_scope_filters",
            "active",
            "scope",
            "organization_id",
            "job_id",
            "competency_id",
            "difficulty",
        ),
        Index("ix_rag_document_filters", "active", "job_id", "competency_id", "difficulty"),
        Index("ix_rag_document_embedding_model", "embedding_model", "embedding_dimension"),
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
