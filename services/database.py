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
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from config import settings

CAMPAIGN_WORKER_CONTROL_ID = "recruitment_campaigns"


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


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    requirements_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="draft", server_default="draft"
    )
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


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


class RecruitmentCampaignRecord(Base):
    __tablename__ = "recruitment_campaigns"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, ForeignKey("organizations.id"), nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    director_hiring_brief: Mapped[str] = mapped_column(Text, nullable=False)
    structured_requirements_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    job_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    competency_rubric_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    application_form_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    objective_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    objective_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    plan_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    approved_objective_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    approved_plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    objective_update_idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    objective_update_request_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    approval_state: Mapped[str] = mapped_column(String, nullable=False, default="draft", server_default="draft")
    workflow_state: Mapped[str] = mapped_column(String, nullable=False, default="brief_created", server_default="brief_created")
    approval_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_decided_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True
    )
    approval_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), nullable=False)

    __table_args__ = (
        CheckConstraint("approval_state IN ('draft', 'pending_review', 'approved', 'rejected')", name="ck_recruitment_campaigns_approval_state"),
        CheckConstraint("workflow_state IN ('brief_created', 'requirements_defined', 'job_description_ready', 'rubric_ready', 'application_form_ready', 'pending_approval', 'approved', 'rejected')", name="ck_recruitment_campaigns_workflow_state"),
        UniqueConstraint("organization_id", "id", name="uq_recruitment_campaigns_org_id"),
        Index("ix_recruitment_campaigns_org_owner", "organization_id", "owner_user_id"),
        Index("ix_recruitment_campaigns_workflow_state", "organization_id", "workflow_state", "approval_state"),
    )


class RecruitmentCampaignApprovalRecord(Base):
    __tablename__ = "recruitment_campaign_approvals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    campaign_id: Mapped[str] = mapped_column(String, nullable=False)
    objective_version: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_recruitment_campaign_approvals_decision",
        ),
        UniqueConstraint("organization_id", "id", name="uq_recruitment_campaign_approvals_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["recruitment_campaigns.organization_id", "recruitment_campaigns.id"],
            name="fk_recruitment_campaign_approvals_campaign",
        ),
        Index(
            "ix_recruitment_campaign_approvals_campaign_version",
            "organization_id",
            "campaign_id",
            "objective_version",
            "plan_version",
        ),
    )


class RecruitmentCampaignHiringOutcomeRecord(Base):
    __tablename__ = "recruitment_campaign_hiring_outcomes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    campaign_id: Mapped[str] = mapped_column(String, nullable=False)
    application_id: Mapped[str] = mapped_column(String, nullable=False)
    objective_version: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    criteria_satisfied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('hired', 'rejected', 'withdrawn')",
            name="ck_recruitment_campaign_hiring_outcomes_outcome",
        ),
        UniqueConstraint(
            "organization_id", "campaign_id", "application_id",
            name="uq_recruitment_campaign_hiring_outcome_application",
        ),
        UniqueConstraint(
            "organization_id", "idempotency_key",
            name="uq_recruitment_campaign_hiring_outcome_idempotency",
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id", "application_id"],
            [
                "recruitment_applications.organization_id",
                "recruitment_applications.campaign_id",
                "recruitment_applications.id",
            ],
            name="fk_recruitment_campaign_hiring_outcomes_application",
        ),
        Index(
            "ix_recruitment_campaign_hiring_outcomes_campaign",
            "organization_id",
            "campaign_id",
            "outcome",
            "decided_at",
        ),
    )


class RecruitmentCampaignExecutionRecord(Base):
    __tablename__ = "recruitment_campaign_executions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(
        String, ForeignKey("recruitment_campaigns.id"), nullable=False
    )
    organization_id: Mapped[str] = mapped_column(
        String, ForeignKey("organizations.id"), nullable=False, index=True
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'waiting_for_approval', 'completed', "
            "'awaiting_progress', 'retryable_failure', 'needs_attention')",
            name="ck_recruitment_campaign_executions_status",
        ),
        UniqueConstraint(
            "campaign_id", name="uq_recruitment_campaign_execution_campaign"
        ),
        Index(
            "ix_recruitment_campaign_executions_org_status",
            "organization_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_recruitment_campaign_executions_dispatch",
            "status",
            "available_at",
            "lease_expires_at",
        ),
    )


class RecruitmentCampaignWorkerControlRecord(Base):
    __tablename__ = "recruitment_campaign_worker_control"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class RecruitmentCampaignExecutionStepRecord(Base):
    __tablename__ = "recruitment_campaign_execution_steps"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    execution_id: Mapped[str] = mapped_column(
        String, ForeignKey("recruitment_campaign_executions.id"), nullable=False
    )
    step_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_json: Mapped[str] = mapped_column(Text, nullable=False)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_source: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'retryable_failure', "
            "'needs_attention')",
            name="ck_recruitment_campaign_execution_steps_status",
        ),
        UniqueConstraint(
            "execution_id",
            "step_key",
            name="uq_recruitment_campaign_execution_step",
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


class RecruitmentApplicationRecord(Base):
    __tablename__ = "recruitment_applications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(String, nullable=False)
    candidate_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    reviewed_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'retryable_failure', "
            "'needs_review', 'needs_attention', 'failed')",
            name="ck_recruitment_applications_status",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'reviewed')",
            name="ck_recruitment_applications_review_status",
        ),
        UniqueConstraint(
            "organization_id", "id", name="uq_recruitment_applications_org_id"
        ),
        UniqueConstraint(
            "organization_id",
            "campaign_id",
            "candidate_id",
            name="uq_recruitment_applications_campaign_candidate",
        ),
        UniqueConstraint(
            "organization_id",
            "campaign_id",
            "id",
            name="uq_recruitment_applications_org_campaign_id",
        ),
        UniqueConstraint(
            "organization_id",
            "campaign_id",
            "id",
            "candidate_id",
            name="uq_recruitment_applications_org_campaign_id_candidate",
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["recruitment_campaigns.organization_id", "recruitment_campaigns.id"],
            name="fk_recruitment_applications_org_campaign",
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_recruitment_applications_org_candidate",
        ),
        Index(
            "ix_recruitment_applications_dispatch",
            "status",
            "available_at",
            "lease_expires_at",
        ),
    )


class CandidateCVDocumentRecord(Base):
    __tablename__ = "candidate_cv_documents"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    application_id: Mapped[str] = mapped_column(String, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    file_extension: Mapped[str] = mapped_column(String, nullable=False)
    media_type: Mapped[str] = mapped_column(String, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parse_status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    parser_version: Mapped[str | None] = mapped_column(String, nullable=True)
    parsed_output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    parsed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "parse_status IN ('pending', 'parsed', 'failed')",
            name="ck_candidate_cv_documents_parse_status",
        ),
        UniqueConstraint(
            "organization_id", "application_id",
            name="uq_candidate_cv_documents_org_application",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id"],
            ["recruitment_applications.organization_id", "recruitment_applications.id"],
            name="fk_candidate_cv_documents_org_application",
        ),
    )


class CandidateScreeningRecord(Base):
    __tablename__ = "candidate_screenings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    application_id: Mapped[str] = mapped_column(String, nullable=False)
    rubric_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rubric_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_source: Mapped[str] = mapped_column(
        String, nullable=False, default="deterministic", server_default="deterministic"
    )
    review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "application_id",
            name="uq_candidate_screenings_org_application",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id"],
            ["recruitment_applications.organization_id", "recruitment_applications.id"],
            name="fk_candidate_screenings_org_application",
        ),
    )


class CandidateCriterionAssessmentRecord(Base):
    __tablename__ = "candidate_criterion_assessments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    screening_id: Mapped[str] = mapped_column(
        String, ForeignKey("candidate_screenings.id"), nullable=False
    )
    criterion_id: Mapped[str] = mapped_column(String, nullable=False)
    criterion_title: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    uncertainty: Mapped[str] = mapped_column(Text, nullable=False)
    review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    review_status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "review_status IN ('pending', 'confirmed', 'needs_correction')",
            name="ck_candidate_criterion_assessments_review_status",
        ),
        UniqueConstraint(
            "screening_id", "criterion_id",
            name="uq_candidate_criterion_assessments_screening_criterion",
        ),
    )


class CandidateCommunicationRecord(Base):
    __tablename__ = "candidate_communications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    campaign_id: Mapped[str] = mapped_column(String, nullable=False)
    application_id: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    template_type: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    template_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    approved_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "template_type IN ('interview_invitation', 'additional_information_request', "
            "'status_update', 'rejection_notification')",
            name="ck_candidate_communications_template_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'awaiting_approval', 'approved', 'sending', 'sent', "
            "'retryable_failure', 'needs_attention', 'cancelled', 'simulated')",
            name="ck_candidate_communications_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name="ck_candidate_communications_attempts",
        ),
        CheckConstraint(
            "delivery_status IS NULL OR delivery_status IN ('simulated', 'sent', 'unknown')",
            name="ck_candidate_communications_delivery_status",
        ),
        CheckConstraint(
            "length(subject) <= 255 AND length(body) <= 20000",
            name="ck_candidate_communications_content_length",
        ),
        CheckConstraint(
            "status NOT IN ('approved', 'sending', 'sent', 'simulated', "
            "'retryable_failure', 'needs_attention') OR "
            "(approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_candidate_communications_approval_required",
        ),
        UniqueConstraint(
            "organization_id", "id", name="uq_candidate_communications_org_id"
        ),
        UniqueConstraint(
            "organization_id", "idempotency_key",
            name="uq_candidate_communications_idempotency",
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["recruitment_campaigns.organization_id", "recruitment_campaigns.id"],
            name="fk_candidate_communications_org_campaign",
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id", "application_id"],
            [
                "recruitment_applications.organization_id",
                "recruitment_applications.campaign_id",
                "recruitment_applications.id",
            ],
            name="fk_candidate_communications_org_campaign_application",
        ),
        Index(
            "ix_candidate_communications_dispatch",
            "status",
            "available_at",
            "lease_expires_at",
        ),
        Index(
            "ix_candidate_communications_org_status",
            "organization_id",
            "status",
            "updated_at",
        ),
    )


class CandidateCommunicationEventRecord(Base):
    __tablename__ = "candidate_communication_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    communication_id: Mapped[str] = mapped_column(String, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "communication_id"],
            [
                "candidate_communications.organization_id",
                "candidate_communications.id",
            ],
            name="fk_candidate_communication_events_org_communication",
        ),
        Index(
            "ix_candidate_communication_events_communication",
            "organization_id",
            "communication_id",
            "created_at",
        ),
    )


class CandidateAvailabilityRequestRecord(Base):
    __tablename__ = "candidate_availability_requests"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    campaign_id: Mapped[str] = mapped_column(String, nullable=False)
    application_id: Mapped[str] = mapped_column(String, nullable=False)
    candidate_id: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interview_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_windows_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_response")
    created_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_response', 'responded', 'reviewed', 'expired', 'cancelled')",
            name="ck_candidate_availability_requests_status",
        ),
        CheckConstraint(
            "interview_duration_minutes BETWEEN 15 AND 240",
            name="ck_candidate_availability_requests_duration",
        ),
        UniqueConstraint(
            "organization_id", "id", name="uq_candidate_availability_requests_org_id"
        ),
        UniqueConstraint(
            "organization_id", "application_id",
            name="uq_candidate_availability_requests_application",
        ),
        UniqueConstraint(
            "organization_id", "idempotency_key",
            name="uq_candidate_availability_requests_idempotency",
        ),
        UniqueConstraint(
            "organization_id", "id", "application_id", "candidate_id",
            name="uq_candidate_availability_requests_response_parent",
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id", "application_id", "candidate_id"],
            [
                "recruitment_applications.organization_id",
                "recruitment_applications.campaign_id",
                "recruitment_applications.id",
                "recruitment_applications.candidate_id",
            ],
            name="fk_candidate_availability_requests_application",
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_candidate_availability_requests_candidate",
        ),
        Index(
            "ix_candidate_availability_requests_expiration",
            "status",
            "response_deadline",
        ),
        Index(
            "ix_candidate_availability_requests_campaign",
            "organization_id",
            "campaign_id",
            "created_at",
        ),
    )


class CandidateAvailabilityResponseRecord(Base):
    __tablename__ = "candidate_availability_responses"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    application_id: Mapped[str] = mapped_column(String, nullable=False)
    candidate_id: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    response_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    windows_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reviewed_window_indices_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reviewed_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "review_status IN ('pending', 'reviewed')",
            name="ck_candidate_availability_responses_review_status",
        ),
        CheckConstraint("version > 0", name="ck_candidate_availability_responses_version"),
        UniqueConstraint(
            "organization_id", "request_id",
            name="uq_candidate_availability_responses_request",
        ),
        UniqueConstraint(
            "organization_id", "request_id", "idempotency_key",
            name="uq_candidate_availability_responses_idempotency",
        ),
        ForeignKeyConstraint(
            ["organization_id", "request_id", "application_id", "candidate_id"],
            [
                "candidate_availability_requests.organization_id",
                "candidate_availability_requests.id",
                "candidate_availability_requests.application_id",
                "candidate_availability_requests.candidate_id",
            ],
            name="fk_candidate_availability_responses_request",
        ),
        Index(
            "ix_candidate_availability_responses_application",
            "organization_id",
            "application_id",
        ),
    )


class CandidateAvailabilitySubmissionRecord(Base):
    __tablename__ = "candidate_availability_submissions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    response_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "request_id"],
            [
                "candidate_availability_requests.organization_id",
                "candidate_availability_requests.id",
            ],
            name="fk_candidate_availability_submissions_request",
        ),
        UniqueConstraint(
            "organization_id", "request_id", "idempotency_key",
            name="uq_candidate_availability_submissions_idempotency",
        ),
        Index(
            "ix_candidate_availability_submissions_request",
            "organization_id",
            "request_id",
            "submitted_at",
        ),
    )


class CandidateAvailabilityEventRecord(Base):
    __tablename__ = "candidate_availability_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "request_id"],
            [
                "candidate_availability_requests.organization_id",
                "candidate_availability_requests.id",
            ],
            name="fk_candidate_availability_events_request",
        ),
        Index(
            "ix_candidate_availability_events_request",
            "organization_id",
            "request_id",
            "created_at",
        ),
    )


class InterviewScheduleRecord(Base):
    __tablename__ = "interview_schedules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    campaign_id: Mapped[str] = mapped_column(String, nullable=False)
    application_id: Mapped[str] = mapped_column(String, nullable=False)
    candidate_id: Mapped[str] = mapped_column(String, nullable=False)
    availability_request_id: Mapped[str] = mapped_column(String, nullable=False)
    availability_response_version: Mapped[int] = mapped_column(Integer, nullable=False)
    availability_window_index: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    interviewer_user_id: Mapped[str] = mapped_column(String, nullable=False)
    exclusive_resources_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    rescheduled_from_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reservation_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancellation_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    confirmed_by_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'pending_confirmation', 'confirmed_internal', "
            "'reschedule_pending', 'cancelled', 'expired', 'needs_attention')",
            name="ck_interview_schedules_status",
        ),
        CheckConstraint("duration_minutes BETWEEN 15 AND 240", name="ck_interview_schedules_duration"),
        CheckConstraint("end_at > start_at", name="ck_interview_schedules_interval"),
        CheckConstraint("version > 0", name="ck_interview_schedules_version"),
        UniqueConstraint("organization_id", "id", name="uq_interview_schedules_org_id"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_interview_schedules_idempotency"
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id", "application_id", "candidate_id"],
            [
                "recruitment_applications.organization_id",
                "recruitment_applications.campaign_id",
                "recruitment_applications.id",
                "recruitment_applications.candidate_id",
            ],
            name="fk_interview_schedules_application",
        ),
        ForeignKeyConstraint(
            ["organization_id", "availability_request_id", "application_id", "candidate_id"],
            [
                "candidate_availability_requests.organization_id",
                "candidate_availability_requests.id",
                "candidate_availability_requests.application_id",
                "candidate_availability_requests.candidate_id",
            ],
            name="fk_interview_schedules_availability",
        ),
        ForeignKeyConstraint(
            ["organization_id", "interviewer_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_interview_schedules_interviewer_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "rescheduled_from_id"],
            ["interview_schedules.organization_id", "interview_schedules.id"],
            name="fk_interview_schedules_rescheduled_from",
        ),
        Index(
            "uq_interview_schedules_confirmed_application",
            "organization_id",
            "application_id",
            unique=True,
            sqlite_where=text("status = 'confirmed_internal'"),
            postgresql_where=text("status = 'confirmed_internal'"),
        ),
        Index(
            "uq_interview_schedules_pending_replacement",
            "organization_id",
            "rescheduled_from_id",
            unique=True,
            sqlite_where=text("status = 'reschedule_pending'"),
            postgresql_where=text("status = 'reschedule_pending'"),
        ),
        Index(
            "ix_interview_schedules_expiration",
            "status",
            "expires_at",
        ),
        Index(
            "ix_interview_schedules_campaign",
            "organization_id",
            "campaign_id",
            "created_at",
        ),
    )


class InterviewScheduleReservationRecord(Base):
    __tablename__ = "interview_schedule_reservations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    schedule_id: Mapped[str] = mapped_column(String, nullable=False)
    reservation_token: Mapped[str] = mapped_column(String(36), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(128), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "resource_type IN ('candidate', 'interviewer', 'exclusive')",
            name="ck_interview_schedule_reservations_resource_type",
        ),
        CheckConstraint("end_at > start_at", name="ck_interview_schedule_reservations_interval"),
        UniqueConstraint(
            "organization_id",
            "schedule_id",
            "reservation_token",
            "resource_type",
            "resource_key",
            name="uq_interview_schedule_reservations_resource",
        ),
        ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["interview_schedules.organization_id", "interview_schedules.id"],
            name="fk_interview_schedule_reservations_schedule",
        ),
        Index(
            "ix_interview_schedule_reservations_overlap",
            "organization_id",
            "resource_type",
            "resource_key",
            "start_at",
            "end_at",
        ),
        Index(
            "ix_interview_schedule_reservations_schedule",
            "organization_id",
            "schedule_id",
            "reservation_token",
        ),
    )


class InterviewScheduleEventRecord(Base):
    __tablename__ = "interview_schedule_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    schedule_id: Mapped[str] = mapped_column(String, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["interview_schedules.organization_id", "interview_schedules.id"],
            name="fk_interview_schedule_events_schedule",
        ),
        Index(
            "ix_interview_schedule_events_schedule",
            "organization_id",
            "schedule_id",
            "created_at",
        ),
    )


class InterviewScheduleMutexRecord(Base):
    __tablename__ = "interview_schedule_mutex"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


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
    campaign_id: Mapped[str | None] = mapped_column(String, nullable=True)
    application_id: Mapped[str | None] = mapped_column(String, nullable=True)
    schedule_id: Mapped[str | None] = mapped_column(String, nullable=True)
    objective_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rubric_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="text")
    current_question: Mapped[str] = mapped_column(String, nullable=False)
    state_json: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_interviews_org_id"),
        UniqueConstraint(
            "organization_id",
            "campaign_id",
            "application_id",
            name="uq_interviews_campaign_application",
        ),
        CheckConstraint(
            "campaign_id IS NULL OR (application_id IS NOT NULL AND schedule_id IS NOT NULL "
            "AND objective_version IS NOT NULL AND plan_version IS NOT NULL "
            "AND objective_version > 0 AND plan_version > 0 "
            "AND rubric_snapshot_json IS NOT NULL)",
            name="ck_interviews_campaign_link_complete",
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_interviews_org_candidate",
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_id", "application_id", "candidate_id"],
            [
                "recruitment_applications.organization_id",
                "recruitment_applications.campaign_id",
                "recruitment_applications.id",
                "recruitment_applications.candidate_id",
            ],
            name="fk_interviews_campaign_application",
        ),
        ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["interview_schedules.organization_id", "interview_schedules.id"],
            name="fk_interviews_schedule",
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
    with SessionLocal.begin() as session:
        if engine.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            raise RuntimeError(
                f"Unsupported database dialect for interview scheduling: {engine.dialect.name}"
            )
        session.execute(
            insert(InterviewScheduleMutexRecord)
            .values(id="global", generation=0)
            .on_conflict_do_nothing(index_elements=["id"])
        )


def get_db_session() -> Session:
    return SessionLocal()
