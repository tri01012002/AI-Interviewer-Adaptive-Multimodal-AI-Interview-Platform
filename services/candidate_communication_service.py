"""Durable, tenant-scoped candidate communications with offline-only delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from string import Template
from typing import Any, Literal, Mapping, Protocol
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError

from config import settings
from services.database import (
    CAMPAIGN_WORKER_CONTROL_ID,
    AccessAuditRecord,
    CandidateCommunicationEventRecord,
    CandidateCommunicationRecord,
    CandidateRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignExecutionRecord,
    RecruitmentCampaignRecord,
    RecruitmentCampaignWorkerControlRecord,
    SessionLocal,
)
from services.email_provider import (
    EmailDeliveryRequest,
    EmailDeliveryResult,
    EmailProvider,
    create_email_provider,
)
from services.recruitment_campaign_service import RecruitmentCampaignService
from services.tenant_authorization import TenantContext

logger = logging.getLogger(__name__)

MAX_COMMUNICATION_ATTEMPTS = 3
MAX_BODY_CHARS = 20_000
ALLOWED_PLACEHOLDERS = {"candidate_name", "campaign_reference"}
SAFE_PROVIDER_ERROR_CLASSES = {
    "acceptance_unknown",
    "authentication_rejected",
    "campaign_approval_revoked",
    "connection_failure",
    "delivery_outcome_unknown",
    "delivery_preflight_rejected",
    "permanent_smtp_rejection",
    "provider_failure",
    "provider_outcome_unknown",
    "provider_rejected",
    "provider_temporary_failure",
    "recipient_refused",
    "reconciled_not_accepted",
    "smtp_protocol_failure",
    "temporary_smtp_rejection",
    "temporary_unavailable",
    "transport_outcome_unknown",
}
EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,63}$"
)

BUILTIN_TEMPLATES: dict[str, tuple[str, str]] = {
    "interview_invitation": (
        "Interview invitation - ${campaign_reference}",
        "Hello ${candidate_name},\n\nWe would like to invite you to interview "
        "for ${campaign_reference}. Please contact the recruitment team to "
        "coordinate a suitable time.\n",
    ),
    "additional_information_request": (
        "Additional information requested - ${campaign_reference}",
        "Hello ${candidate_name},\n\nWe would appreciate additional information "
        "about your application for ${campaign_reference}. Please reply to the "
        "recruitment team with the requested details.\n",
    ),
    "status_update": (
        "Application update - ${campaign_reference}",
        "Hello ${candidate_name},\n\nThere is an update regarding your "
        "application for ${campaign_reference}. The recruitment team will "
        "contact you with further information.\n",
    ),
    "rejection_notification": (
        "Application status - ${campaign_reference}",
        "Hello ${candidate_name},\n\nThe recruitment team has an update about "
        "your application for ${campaign_reference}. Please contact the team "
        "if you have questions.\n",
    ),
}


class CommunicationDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_type: Literal[
        "interview_invitation",
        "additional_information_request",
        "status_update",
        "rejection_notification",
    ]
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    subject_template: str | None = Field(default=None, min_length=1, max_length=1024)
    body_template: str | None = Field(default=None, min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def templates_are_paired(self) -> CommunicationDraftRequest:
        if (self.subject_template is None) != (self.body_template is None):
            raise ValueError("subject_template and body_template must be supplied together")
        return self


class CommunicationEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str
    outcome: str
    attempt_number: int | None
    provider_name: str | None
    details: dict[str, Any]
    created_at: datetime


class CommunicationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    campaign_id: str
    application_id: str
    idempotency_key: str
    template_type: str
    template_version: str
    recipient_email: str
    subject: str
    body: str
    status: str
    created_by_user_id: str
    approved_by_user_id: str | None
    approved_at: datetime | None
    attempt_count: int
    max_attempts: int
    provider_name: str | None
    delivery_status: str | None
    provider_reference: str | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None
    events: list[CommunicationEventResponse]


class CommunicationReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accepted", "not_accepted", "unresolved"]
    provider_message_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )


@dataclass(frozen=True)
class CommunicationMessage:
    communication_id: str
    idempotency_key: str
    recipient: str
    subject: str
    body: str


@dataclass(frozen=True)
class CommunicationDeliveryResult:
    """Compatibility result accepted from existing in-process test/custom adapters."""

    outcome: Literal[
        "simulated",
        "accepted",
        "definitively_rejected",
        "retryable_not_accepted",
        "unknown",
        "retryable_failure",
        "ambiguous",
    ]
    provider_name: str
    provider_reference: str | None = None
    failure_code: str | None = None
    idempotency_supported: bool = False
    safe_metadata: dict[str, str | int | bool] | None = None


class CommunicationProvider(Protocol):
    """Legacy injectable interface; retryable means the message was not accepted."""

    def deliver(self, message: CommunicationMessage) -> CommunicationDeliveryResult: ...


class SimulatedCommunicationProvider:
    """Deterministic local adapter; it never sends or queues a real message."""

    def deliver(self, message: CommunicationMessage) -> CommunicationDeliveryResult:
        reference = "sim-" + hashlib.sha256(message.communication_id.encode()).hexdigest()[:24]
        return CommunicationDeliveryResult(
            outcome="simulated",
            provider_name="offline_simulation",
            provider_reference=reference,
        )


class CommunicationDeliveryFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class CandidateCommunicationService:
    @classmethod
    def create_draft(
        cls,
        application_id: str,
        context: TenantContext,
        payload: CommunicationDraftRequest,
    ) -> CommunicationResponse:
        cls._require_mutation_role(context)
        with SessionLocal() as session:
            application, campaign, candidate = cls._load_target(
                session, application_id, context
            )
            RecruitmentCampaignService._assert_authorized(campaign, context)
            if campaign.approval_state != "approved":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Communications require an approved campaign",
                )
            if application.status != "needs_review":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Application must finish screening before communication drafting",
                )
            recipient = cls._validate_recipient(candidate.email)
            values = {
                "candidate_name": cls._safe_single_line(candidate.name, 200),
                "campaign_reference": f"campaign-{campaign.id[:8]}",
            }
            default_subject, default_body = BUILTIN_TEMPLATES[payload.template_type]
            subject_template = payload.subject_template or default_subject
            body_template = payload.body_template or default_body
            subject = cls._render(subject_template, values, subject=True)
            body = cls._render(body_template, values, subject=False)
            template_version = (
                "custom-v1" if payload.subject_template is not None else "builtin-v1"
            )
            snapshot = {
                "subject_template": subject_template,
                "body_template": body_template,
                "variables": sorted(ALLOWED_PLACEHOLDERS),
            }
            request_hash = hashlib.sha256(
                json.dumps(
                    {
                        "application_id": application.id,
                        "template_type": payload.template_type,
                        "template_version": template_version,
                        "template_snapshot": snapshot,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            existing = session.execute(
                select(CandidateCommunicationRecord).where(
                    CandidateCommunicationRecord.organization_id
                    == context.organization_id,
                    CandidateCommunicationRecord.idempotency_key
                    == payload.idempotency_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency key was already used for a different communication",
                    )
                return cls._serialize(session, existing)
            campaign_id = campaign.id

        now = datetime.now(UTC)
        communication_id = str(uuid4())
        try:
            with SessionLocal.begin() as session:
                current_application, current_campaign, current_candidate = cls._load_target(
                    session, application_id, context
                )
                RecruitmentCampaignService._assert_authorized(current_campaign, context)
                if (
                    current_campaign.approval_state != "approved"
                    or current_application.status != "needs_review"
                    or cls._validate_recipient(current_candidate.email) != recipient
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Application or campaign state changed while creating the draft",
                    )
                communication = CandidateCommunicationRecord(
                    id=communication_id,
                    organization_id=context.organization_id,
                    campaign_id=campaign_id,
                    application_id=application_id,
                    idempotency_key=payload.idempotency_key,
                    request_hash=request_hash,
                    template_type=payload.template_type,
                    template_version=template_version,
                    template_snapshot_json=json.dumps(
                        snapshot, sort_keys=True, separators=(",", ":")
                    ),
                    recipient_email=recipient,
                    subject=subject,
                    body=body,
                    status="draft",
                    created_by_user_id=context.actor_user_id,
                    attempt_count=0,
                    max_attempts=MAX_COMMUNICATION_ATTEMPTS,
                    created_at=now,
                    updated_at=now,
                )
                session.add(communication)
                # Insert the parent row before its event row: without
                # relationship() the unit of work does not order them, and
                # PostgreSQL enforces the event's foreign key.
                session.flush()
                cls._record_transition(
                    session,
                    communication,
                    context,
                    "communication_draft_created",
                    "created",
                    None,
                    "draft",
                )
                session.flush()
                cls._audit(
                    session,
                    communication,
                    context,
                    "candidate_communication_draft_created",
                )
                return cls._serialize(session, communication)
        except IntegrityError as exc:
            with SessionLocal() as session:
                existing = session.execute(
                    select(CandidateCommunicationRecord).where(
                        CandidateCommunicationRecord.organization_id
                        == context.organization_id,
                        CandidateCommunicationRecord.idempotency_key
                        == payload.idempotency_key,
                    )
                ).scalar_one_or_none()
                if existing is not None and existing.request_hash == request_hash:
                    return cls._serialize(session, existing)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Communication could not be created with this idempotency key",
            ) from exc

    @classmethod
    def submit_for_approval(
        cls, communication_id: str, context: TenantContext
    ) -> CommunicationResponse:
        return cls._transition_as_campaign_user(
            communication_id,
            context,
            expected="draft",
            target="awaiting_approval",
            action="candidate_communication_submitted",
            event_type="submitted_for_approval",
        )

    @classmethod
    def approve(
        cls, communication_id: str, context: TenantContext
    ) -> CommunicationResponse:
        if context.role not in {"org_admin", "platform_admin"}:
            raise HTTPException(status_code=403, detail="Only organization administrators can approve communications")
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            communication, campaign = cls._load_communication(
                session, communication_id, context
            )
            RecruitmentCampaignService._assert_authorized(campaign, context)
            cls._expect_status(communication, "awaiting_approval")
            if campaign.approval_state != "approved":
                raise HTTPException(status_code=409, detail="Campaign approval is no longer active")
            changed = session.execute(
                update(CandidateCommunicationRecord)
                .where(
                    CandidateCommunicationRecord.id == communication_id,
                    CandidateCommunicationRecord.organization_id
                    == context.organization_id,
                    CandidateCommunicationRecord.status == "awaiting_approval",
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == CandidateCommunicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == CandidateCommunicationRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state == "approved",
                        ~exists().where(
                            RecruitmentCampaignExecutionRecord.campaign_id
                            == RecruitmentCampaignRecord.id,
                            RecruitmentCampaignExecutionRecord.organization_id
                            == RecruitmentCampaignRecord.organization_id,
                            RecruitmentCampaignExecutionRecord.status == "completed",
                        ),
                    ),
                )
                .values(
                    status="approved",
                    approved_by_user_id=context.actor_user_id,
                    approved_at=now,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise HTTPException(status_code=409, detail="Communication approval state changed")
            session.refresh(communication)
            cls._record_transition(
                session, communication, context, "communication_approved",
                "allowed", "awaiting_approval", "approved",
            )
            cls._audit(session, communication, context, "candidate_communication_approved")
            return cls._serialize(session, communication)

    @classmethod
    def cancel(
        cls, communication_id: str, context: TenantContext
    ) -> CommunicationResponse:
        return cls._transition_as_campaign_user(
            communication_id,
            context,
            expected={
                "draft", "awaiting_approval", "approved", "retryable_failure",
                "needs_attention",
            },
            target="cancelled",
            action="candidate_communication_cancelled",
            event_type="cancelled",
            delivery_not_unknown=True,
        )

    @classmethod
    def retry(
        cls, communication_id: str, context: TenantContext
    ) -> CommunicationResponse:
        cls._require_mutation_role(context)
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            communication, campaign = cls._load_communication(
                session, communication_id, context
            )
            RecruitmentCampaignService._assert_authorized(campaign, context)
            cls._expect_status(communication, "retryable_failure")
            if communication.attempt_count >= communication.max_attempts:
                raise HTTPException(status_code=409, detail="Communication retry limit was reached")
            available_at = communication.available_at
            if available_at is not None and available_at.tzinfo is None:
                available_at = available_at.replace(tzinfo=UTC)
            if available_at is not None and available_at > now:
                raise HTTPException(status_code=409, detail="Communication retry backoff has not elapsed")
            if campaign.approval_state != "approved":
                raise HTTPException(status_code=409, detail="Campaign approval is no longer active")
            changed = session.execute(
                update(CandidateCommunicationRecord)
                .where(
                    CandidateCommunicationRecord.id == communication_id,
                    CandidateCommunicationRecord.organization_id
                    == context.organization_id,
                    CandidateCommunicationRecord.status == "retryable_failure",
                    CandidateCommunicationRecord.attempt_count
                    < CandidateCommunicationRecord.max_attempts,
                    or_(
                        CandidateCommunicationRecord.available_at.is_(None),
                        CandidateCommunicationRecord.available_at <= now,
                    ),
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == CandidateCommunicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == CandidateCommunicationRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state == "approved",
                    ),
                    ~exists().where(
                        RecruitmentCampaignExecutionRecord.campaign_id
                        == CandidateCommunicationRecord.campaign_id,
                        RecruitmentCampaignExecutionRecord.organization_id
                        == CandidateCommunicationRecord.organization_id,
                        RecruitmentCampaignExecutionRecord.status == "completed",
                    ),
                )
                .values(
                    status="approved",
                    available_at=None,
                    failure_code=None,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise HTTPException(status_code=409, detail="Communication retry state changed")
            session.refresh(communication)
            cls._record_transition(
                session, communication, context, "candidate_communication_retry_requested",
                "allowed", "retryable_failure", "approved",
            )
            cls._audit(
                session, communication, context, "candidate_communication_retry_requested"
            )
            return cls._serialize(session, communication)

    @classmethod
    def reconcile(
        cls,
        communication_id: str,
        context: TenantContext,
        payload: CommunicationReconciliationRequest,
    ) -> CommunicationResponse:
        if context.role not in {"org_admin", "platform_admin"}:
            raise HTTPException(
                status_code=403,
                detail="Only organization administrators can reconcile delivery outcomes",
            )
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            communication, campaign = cls._load_communication(
                session, communication_id, context
            )
            RecruitmentCampaignService._assert_authorized(campaign, context)
            cls._expect_status(communication, "needs_attention")
            if communication.delivery_status != "unknown":
                raise HTTPException(
                    status_code=409,
                    detail="Only unknown delivery outcomes can be reconciled",
                )

            prior_status = communication.status
            if payload.decision == "accepted":
                target_status = "sent"
                values = {
                    "status": target_status,
                    "delivery_status": "sent",
                    "sent_at": now,
                    "provider_reference": payload.provider_message_id,
                    "available_at": None,
                    "failure_code": None,
                    "updated_at": now,
                }
            elif payload.decision == "not_accepted":
                if campaign.approval_state != "approved":
                    target_status = "cancelled"
                elif communication.attempt_count < communication.max_attempts:
                    target_status = "retryable_failure"
                else:
                    target_status = "needs_attention"
                values = {
                    "status": target_status,
                    "delivery_status": None,
                    "sent_at": None,
                    "provider_reference": None,
                    "failure_code": "reconciled_not_accepted",
                    "available_at": (
                        now if target_status == "retryable_failure" else None
                    ),
                    "updated_at": now,
                }
            else:
                target_status = prior_status
                values = {"updated_at": now}
            changed = session.execute(
                update(CandidateCommunicationRecord)
                .where(
                    CandidateCommunicationRecord.id == communication_id,
                    CandidateCommunicationRecord.organization_id
                    == context.organization_id,
                    CandidateCommunicationRecord.status == "needs_attention",
                    CandidateCommunicationRecord.delivery_status == "unknown",
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                raise HTTPException(status_code=409, detail="Communication outcome changed")
            session.refresh(communication)
            cls._record_transition(
                session,
                communication,
                context,
                "communication_delivery_reconciled",
                payload.decision,
                prior_status,
                target_status,
                attempt_number=communication.attempt_count,
                provider_name=communication.provider_name,
                details={
                    "provider_message_id_recorded": (
                        payload.provider_message_id is not None
                        if payload.decision == "accepted"
                        else False
                    )
                },
            )
            cls._audit(
                session,
                communication,
                context,
                "candidate_communication_delivery_reconciled",
            )
            return cls._serialize(session, communication)

    @classmethod
    def get(
        cls, communication_id: str, context: TenantContext
    ) -> CommunicationResponse:
        with SessionLocal() as session:
            communication, campaign = cls._load_communication(
                session, communication_id, context
            )
            if context.role == "candidate":
                candidate = session.execute(
                    select(CandidateRecord.id).where(
                        CandidateRecord.id
                        == select(RecruitmentApplicationRecord.candidate_id)
                        .where(RecruitmentApplicationRecord.id == communication.application_id)
                        .scalar_subquery(),
                        CandidateRecord.organization_id == context.organization_id,
                        CandidateRecord.user_id == context.actor_user_id,
                    )
                ).scalar_one_or_none()
                if candidate is None:
                    raise HTTPException(status_code=404, detail="Communication not found")
            else:
                RecruitmentCampaignService._assert_authorized(campaign, context)
            return cls._serialize(session, communication)

    @classmethod
    def discover_eligible(cls, limit: int) -> list[str]:
        if limit < 1:
            return []
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            expired = session.execute(
                select(CandidateCommunicationRecord).where(
                    CandidateCommunicationRecord.status == "sending",
                    CandidateCommunicationRecord.lease_expires_at <= now,
                ).limit(limit)
            ).scalars().all()
            for communication in expired:
                old_status = communication.status
                # Conditional so that concurrent sweepers record the expiry
                # once: the loser re-reads the committed row and matches 0.
                changed = session.execute(
                    update(CandidateCommunicationRecord)
                    .execution_options(synchronize_session=False)
                    .where(
                        CandidateCommunicationRecord.id == communication.id,
                        CandidateCommunicationRecord.status == "sending",
                        CandidateCommunicationRecord.lease_token
                        == communication.lease_token,
                        CandidateCommunicationRecord.lease_expires_at <= now,
                    )
                    .values(
                        status="needs_attention",
                        delivery_status="unknown",
                        failure_code="delivery_outcome_unknown",
                        lease_token=None,
                        lease_expires_at=None,
                        available_at=None,
                        updated_at=now,
                    )
                )
                if changed.rowcount != 1:
                    continue
                session.refresh(communication)
                cls._record_transition(
                    session,
                    communication,
                    None,
                    "communication_lease_expired",
                    "needs_attention",
                    old_status,
                    communication.status,
                    attempt_number=communication.attempt_count,
                    provider_name=communication.provider_name,
                )
                cls._audit_system(
                    session, communication, "candidate_communication_lease_expired"
                )

        retry_due = and_(
            CandidateCommunicationRecord.status == "retryable_failure",
            CandidateCommunicationRecord.available_at <= now,
        )
        with SessionLocal() as session:
            communication_ids = list(
                session.execute(
                    select(CandidateCommunicationRecord.id)
                    .join(
                        RecruitmentCampaignRecord,
                        and_(
                            RecruitmentCampaignRecord.id
                            == CandidateCommunicationRecord.campaign_id,
                            RecruitmentCampaignRecord.organization_id
                            == CandidateCommunicationRecord.organization_id,
                        ),
                    )
                    .where(
                        RecruitmentCampaignRecord.approval_state == "approved",
                        ~exists().where(
                            RecruitmentCampaignExecutionRecord.campaign_id
                            == RecruitmentCampaignRecord.id,
                            RecruitmentCampaignExecutionRecord.organization_id
                            == RecruitmentCampaignRecord.organization_id,
                            RecruitmentCampaignExecutionRecord.status == "completed",
                        ),
                        or_(
                            CandidateCommunicationRecord.status == "approved",
                            retry_due,
                        ),
                    )
                    .order_by(CandidateCommunicationRecord.created_at)
                    .limit(limit)
                ).scalars()
            )
            return [
                communication_id
                for communication_id in communication_ids
                if not cls._campaign_target_achieved(session, communication_id)
            ]

    @staticmethod
    def _campaign_target_achieved(session, communication_id: str) -> bool:
        campaign = session.execute(
            select(RecruitmentCampaignRecord)
            .join(
                CandidateCommunicationRecord,
                and_(
                    CandidateCommunicationRecord.campaign_id
                    == RecruitmentCampaignRecord.id,
                    CandidateCommunicationRecord.organization_id
                    == RecruitmentCampaignRecord.organization_id,
                ),
            )
            .where(CandidateCommunicationRecord.id == communication_id)
        ).scalar_one_or_none()
        return campaign is not None and RecruitmentCampaignService.target_is_achieved(
            session, campaign
        )

    @classmethod
    def process_dispatched(
        cls,
        communication_id: str,
        provider: CommunicationProvider | EmailProvider | None = None,
    ) -> CommunicationResponse | None:
        active_provider = create_email_provider() if provider is None else provider
        token = str(uuid4())
        now = datetime.now(UTC)
        retry_due = and_(
            CandidateCommunicationRecord.status == "retryable_failure",
            CandidateCommunicationRecord.available_at <= now,
        )
        with SessionLocal.begin() as session:
            prior_status = session.execute(
                select(CandidateCommunicationRecord.status).where(
                    CandidateCommunicationRecord.id == communication_id
                )
            ).scalar_one_or_none()
            claim = session.execute(
                update(CandidateCommunicationRecord)
                .where(
                    CandidateCommunicationRecord.id == communication_id,
                    CandidateCommunicationRecord.attempt_count
                    < CandidateCommunicationRecord.max_attempts,
                    CandidateCommunicationRecord.approved_by_user_id.is_not(None),
                    CandidateCommunicationRecord.approved_at.is_not(None),
                    or_(
                        CandidateCommunicationRecord.status == "approved",
                        retry_due,
                    ),
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == CandidateCommunicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == CandidateCommunicationRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state == "approved",
                    ),
                    ~exists().where(
                        RecruitmentCampaignExecutionRecord.campaign_id
                        == CandidateCommunicationRecord.campaign_id,
                        RecruitmentCampaignExecutionRecord.organization_id
                        == CandidateCommunicationRecord.organization_id,
                        RecruitmentCampaignExecutionRecord.status == "completed",
                    ),
                    ~exists().where(
                        RecruitmentCampaignWorkerControlRecord.id
                        == CAMPAIGN_WORKER_CONTROL_ID,
                        RecruitmentCampaignWorkerControlRecord.paused.is_(True),
                    ),
                )
                .values(
                    status="sending",
                    attempt_count=CandidateCommunicationRecord.attempt_count + 1,
                    lease_token=token,
                    lease_expires_at=now
                    + timedelta(seconds=settings.CAMPAIGN_WORKER_LEASE_SECONDS),
                    available_at=None,
                    failure_code=None,
                    updated_at=now,
                )
            )
            if claim.rowcount != 1:
                return None
            communication = session.get(CandidateCommunicationRecord, communication_id)
            if communication is None:
                raise RuntimeError("Claimed communication record disappeared")
            attempt = communication.attempt_count
            cls._record_transition(
                session,
                communication,
                None,
                "communication_delivery_attempt_started",
                "started",
                prior_status,
                "sending",
                attempt_number=attempt,
                provider_name=cls._provider_label(active_provider),
            )
            cls._audit_system(
                session, communication, "candidate_communication_delivery_attempt_started"
            )

        with SessionLocal() as session:
            communication = session.execute(
                select(CandidateCommunicationRecord).where(
                    CandidateCommunicationRecord.id == communication_id,
                    CandidateCommunicationRecord.lease_token == token,
                    CandidateCommunicationRecord.status == "sending",
                    CandidateCommunicationRecord.lease_expires_at > datetime.now(UTC),
                )
            ).scalar_one_or_none()
            if communication is None:
                return None
            preflight_valid = (
                communication.approved_by_user_id is not None
                and communication.approved_at is not None
                and cls._campaign_still_approved(session, communication)
                and cls._delivery_scope_valid(session, communication)
            )
            request = EmailDeliveryRequest(
                communication_id=communication.id,
                idempotency_key=communication.id,
                recipient=communication.recipient_email,
                subject=communication.subject,
                body=communication.body,
            )
            attempt = communication.attempt_count

        if not preflight_valid:
            return cls._mark_pre_delivery_revoked(communication_id, token)
        try:
            raw_result = active_provider.deliver(request)
        except CommunicationDeliveryFailure as exc:
            return cls._finish_provider_failure(
                communication_id,
                token,
                attempt,
                cls._safe_failure_code(exc.code),
                retryable=exc.retryable,
                outcome=(
                    "retryable_not_accepted" if exc.retryable else "unknown"
                ),
            )
        except Exception:
            return cls._finish_provider_failure(
                communication_id,
                token,
                attempt,
                "provider_outcome_unknown",
                retryable=False,
                outcome="unknown",
            )
        result = cls._normalize_provider_result(raw_result)
        if result.outcome == "simulated":
            return cls._finish_simulated(
                communication_id, token, attempt, result
            )
        if result.outcome == "accepted":
            return cls._finish_accepted(communication_id, token, attempt, result)
        if result.outcome == "retryable_not_accepted":
            return cls._finish_provider_failure(
                communication_id,
                token,
                attempt,
                cls._safe_failure_code(result.error_class or "provider_temporary_failure"),
                retryable=True,
                outcome="retryable_not_accepted",
                provider_name=cls._safe_provider_name(result.provider_name),
                safe_metadata=result.safe_metadata,
            )
        definitive_rejection = result.outcome == "definitively_rejected"
        return cls._finish_provider_failure(
            communication_id,
            token,
            attempt,
            cls._safe_failure_code(
                result.error_class
                or (
                    "provider_rejected"
                    if definitive_rejection
                    else "provider_outcome_unknown"
                )
            ),
            retryable=False,
            outcome=(
                "definitively_rejected" if definitive_rejection else "unknown"
            ),
            provider_name=cls._safe_provider_name(result.provider_name),
            safe_metadata=result.safe_metadata,
        )

    @classmethod
    def _mark_pre_delivery_revoked(
        cls, communication_id: str, token: str
    ) -> CommunicationResponse | None:
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            communication = cls._locked_communication(
                session, communication_id, token, now
            )
            if communication is None:
                return None
            communication.status = "cancelled"
            communication.delivery_status = None
            communication.failure_code = "delivery_preflight_rejected"
            communication.available_at = None
            communication.lease_token = None
            communication.lease_expires_at = None
            communication.updated_at = now
            cls._record_transition(
                session,
                communication,
                None,
                "communication_delivery_preflight_rejected",
                "not_attempted",
                "sending",
                "cancelled",
                attempt_number=communication.attempt_count,
            )
            cls._audit_system(
                session,
                communication,
                "candidate_communication_delivery_preflight_rejected",
            )
            return cls._serialize(session, communication)

    @classmethod
    def _finish_simulated(
        cls,
        communication_id: str,
        token: str,
        attempt: int,
        result: EmailDeliveryResult,
    ) -> CommunicationResponse | None:
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            communication = cls._locked_communication(session, communication_id, token, now)
            if communication is None:
                return None
            communication.status = "simulated"
            communication.provider_name = cls._safe_provider_name(result.provider_name)
            communication.delivery_status = "simulated"
            communication.provider_reference = cls._safe_provider_reference(
                result.provider_message_id
            )
            communication.sent_at = None
            communication.lease_token = None
            communication.lease_expires_at = None
            communication.updated_at = now
            cls._record_transition(
                session,
                communication,
                None,
                "communication_delivery_simulated",
                "simulated",
                "sending",
                "simulated",
                attempt_number=attempt,
                provider_name=communication.provider_name,
                details=cls._safe_provider_metadata(result.safe_metadata),
            )
            cls._audit_system(
                session, communication, "candidate_communication_delivery_simulated"
            )
            logger.info(
                "Candidate communication simulated",
                extra={
                    "communication_id": communication.id,
                    "campaign_id": communication.campaign_id,
                    "application_id": communication.application_id,
                    "attempt": attempt,
                    "provider": communication.provider_name,
                },
            )
            return cls._serialize(session, communication)

    @classmethod
    def _finish_accepted(
        cls,
        communication_id: str,
        token: str,
        attempt: int,
        result: EmailDeliveryResult,
    ) -> CommunicationResponse | None:
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            communication = cls._locked_communication(session, communication_id, token, now)
            if communication is None:
                return None
            campaign_approved = cls._campaign_still_approved(session, communication)
            communication.status = "sent"
            communication.delivery_status = "sent"
            communication.provider_name = cls._safe_provider_name(result.provider_name)
            communication.provider_reference = cls._safe_provider_reference(
                result.provider_message_id
            )
            communication.failure_code = None
            communication.sent_at = now
            communication.lease_token = None
            communication.lease_expires_at = None
            communication.available_at = None
            communication.updated_at = now
            cls._record_transition(
                session,
                communication,
                None,
                "communication_delivery_attempt_completed",
                "accepted",
                "sending",
                "sent",
                attempt_number=attempt,
                provider_name=communication.provider_name,
                details={
                    **cls._safe_provider_metadata(result.safe_metadata),
                    "campaign_approval_active_at_completion": campaign_approved,
                },
            )
            cls._audit_system(
                session,
                communication,
                (
                    "candidate_communication_provider_accepted"
                    if campaign_approved
                    else "candidate_communication_provider_accepted_after_campaign_revocation"
                ),
            )
            logger.info(
                "Candidate communication accepted by email provider",
                extra={
                    "communication_id": communication.id,
                    "campaign_id": communication.campaign_id,
                    "attempt": attempt,
                    "provider": communication.provider_name,
                },
            )
            return cls._serialize(session, communication)

    @classmethod
    def _finish_provider_failure(
        cls,
        communication_id: str,
        token: str,
        attempt: int,
        code: str,
        *,
        retryable: bool,
        outcome: Literal[
            "definitively_rejected", "retryable_not_accepted", "unknown"
        ],
        provider_name: str | None = None,
        safe_metadata: Mapping[str, str | int | bool] | None = None,
    ) -> CommunicationResponse | None:
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            communication = cls._locked_communication(session, communication_id, token, now)
            if communication is None:
                return None
            campaign_approved = cls._campaign_still_approved(session, communication)
            can_retry = (
                retryable
                and attempt < communication.max_attempts
                and campaign_approved
            )
            retry_at = None
            if outcome == "unknown":
                communication.status = "needs_attention"
                communication.available_at = None
                communication.delivery_status = "unknown"
            elif not campaign_approved:
                communication.status = "cancelled"
                communication.available_at = None
                communication.delivery_status = None
                code = "campaign_approval_revoked"
            elif can_retry:
                backoff = min(
                    settings.CAMPAIGN_WORKER_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    settings.CAMPAIGN_WORKER_RETRY_MAX_SECONDS,
                )
                communication.status = "retryable_failure"
                retry_at = now + timedelta(seconds=backoff)
                communication.available_at = retry_at
                communication.delivery_status = None
            else:
                communication.status = "needs_attention"
                communication.available_at = None
                communication.delivery_status = None
            communication.provider_name = provider_name
            communication.failure_code = code[:64]
            communication.lease_token = None
            communication.lease_expires_at = None
            communication.updated_at = now
            cls._record_transition(
                session,
                communication,
                None,
                "communication_delivery_attempt_completed",
                outcome,
                "sending",
                communication.status,
                attempt_number=attempt,
                provider_name=provider_name,
                details={
                    **cls._safe_provider_metadata(safe_metadata),
                    "campaign_approval_active_at_completion": campaign_approved,
                    **(
                        {"retry_scheduled_at": retry_at.isoformat()}
                        if retry_at is not None
                        else {}
                    ),
                },
            )
            cls._audit_system(
                session,
                communication,
                (
                    "candidate_communication_delivery_failed"
                    if campaign_approved
                    else "candidate_communication_campaign_approval_revoked"
                ),
            )
            return cls._serialize(session, communication)

    @staticmethod
    def _locked_communication(session, communication_id: str, token: str, now):
        return session.execute(
            select(CandidateCommunicationRecord).where(
                CandidateCommunicationRecord.id == communication_id,
                CandidateCommunicationRecord.status == "sending",
                CandidateCommunicationRecord.lease_token == token,
                CandidateCommunicationRecord.lease_expires_at > now,
            )
        ).scalar_one_or_none()

    @staticmethod
    def _campaign_still_approved(session, communication) -> bool:
        return (
            session.execute(
                select(RecruitmentCampaignRecord.id).where(
                    RecruitmentCampaignRecord.id == communication.campaign_id,
                    RecruitmentCampaignRecord.organization_id
                    == communication.organization_id,
                    RecruitmentCampaignRecord.approval_state == "approved",
                )
            ).scalar_one_or_none()
            is not None
        )

    @staticmethod
    def _delivery_scope_valid(session, communication) -> bool:
        return (
            session.execute(
                select(RecruitmentApplicationRecord.id)
                .join(
                    CandidateRecord,
                    and_(
                        CandidateRecord.id
                        == RecruitmentApplicationRecord.candidate_id,
                        CandidateRecord.organization_id
                        == RecruitmentApplicationRecord.organization_id,
                    ),
                )
                .where(
                    RecruitmentApplicationRecord.id == communication.application_id,
                    RecruitmentApplicationRecord.organization_id
                    == communication.organization_id,
                    RecruitmentApplicationRecord.campaign_id == communication.campaign_id,
                    CandidateRecord.email == communication.recipient_email,
                )
            ).scalar_one_or_none()
            is not None
        )

    @classmethod
    def _normalize_provider_result(cls, result: object) -> EmailDeliveryResult:
        outcome = getattr(result, "outcome", "unknown")
        outcome_aliases = {
            "retryable_failure": "retryable_not_accepted",
            "ambiguous": "unknown",
        }
        normalized_outcome = (
            outcome_aliases.get(outcome, outcome)
            if isinstance(outcome, str)
            else "unknown"
        )
        if normalized_outcome not in {
            "simulated",
            "accepted",
            "definitively_rejected",
            "retryable_not_accepted",
            "unknown",
        }:
            normalized_outcome = "unknown"
        return EmailDeliveryResult(
            outcome=normalized_outcome,
            provider_name=cls._safe_provider_name(
                getattr(result, "provider_name", "unknown_provider")
            ),
            provider_message_id=cls._safe_provider_reference(
                getattr(result, "provider_message_id", None)
                or getattr(result, "provider_reference", None)
            ),
            error_class=cls._safe_failure_code(
                getattr(result, "error_class", None)
                or getattr(result, "failure_code", None)
                or "provider_failure"
            ),
            idempotency_supported=(
                getattr(result, "idempotency_supported", False) is True
            ),
            safe_metadata={
                **cls._safe_provider_metadata(
                    getattr(result, "safe_metadata", None)
                ),
                "idempotency_supported": (
                    getattr(result, "idempotency_supported", False) is True
                ),
            },
        )

    @staticmethod
    def _safe_provider_metadata(
        metadata: Mapping[str, object] | None,
    ) -> dict[str, str | int | bool]:
        allowed_keys = {
            "idempotency_supported",
            "idempotency_scope",
            "idempotency_retention_seconds",
            "smtp_acceptance",
            "smtp_status_class",
            "refused_recipient_count",
            "campaign_approval_active_at_completion",
        }
        if not isinstance(metadata, Mapping):
            return {}
        result: dict[str, str | int | bool] = {}
        for key, value in metadata.items():
            if key not in allowed_keys or isinstance(value, (dict, list, tuple)):
                continue
            if isinstance(value, bool) or (
                isinstance(value, int) and 0 <= value <= 10**9
            ):
                result[key] = value
            elif (
                isinstance(value, str)
                and len(value) <= 128
                and re.fullmatch(r"[A-Za-z0-9._:-]*", value)
            ):
                result[key] = value
        return result

    @staticmethod
    def _provider_label(provider: object) -> str:
        value = getattr(provider, "provider_name", None)
        if not isinstance(value, str):
            value = (
                "offline_simulation"
                if isinstance(provider, SimulatedCommunicationProvider)
                else "injected_provider"
            )
        return CandidateCommunicationService._safe_provider_name(value)

    @classmethod
    def _transition_as_campaign_user(
        cls,
        communication_id: str,
        context: TenantContext,
        *,
        expected: str | set[str],
        target: str,
        action: str,
        event_type: str,
        delivery_not_unknown: bool = False,
    ) -> CommunicationResponse:
        cls._require_mutation_role(context)
        with SessionLocal.begin() as session:
            communication, campaign = cls._load_communication(
                session, communication_id, context
            )
            RecruitmentCampaignService._assert_authorized(campaign, context)
            cls._expect_status(communication, expected)
            if target == "awaiting_approval" and campaign.approval_state != "approved":
                raise HTTPException(status_code=409, detail="Campaign approval is no longer active")
            prior = communication.status
            allowed = {expected} if isinstance(expected, str) else expected
            conditions = [
                CandidateCommunicationRecord.id == communication_id,
                CandidateCommunicationRecord.organization_id == context.organization_id,
                CandidateCommunicationRecord.status.in_(allowed),
            ]
            if delivery_not_unknown:
                conditions.append(
                    or_(
                        CandidateCommunicationRecord.delivery_status.is_(None),
                        CandidateCommunicationRecord.delivery_status != "unknown",
                    )
                )
            if target == "awaiting_approval":
                conditions.append(
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == CandidateCommunicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == CandidateCommunicationRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state == "approved",
                    )
                )
            now = datetime.now(UTC)
            values: dict[str, Any] = {"status": target, "updated_at": now}
            if target == "cancelled":
                values["available_at"] = None
            changed = session.execute(
                update(CandidateCommunicationRecord)
                .where(*conditions)
                .values(**values)
            )
            if changed.rowcount != 1:
                raise HTTPException(status_code=409, detail="Communication state changed")
            session.refresh(communication)
            cls._record_transition(
                session, communication, context, event_type, "allowed", prior, target
            )
            cls._audit(session, communication, context, action)
            return cls._serialize(session, communication)

    @staticmethod
    def _require_mutation_role(context: TenantContext) -> None:
        if context.role not in {"recruiter", "org_admin", "platform_admin"}:
            raise HTTPException(status_code=403, detail="Communication access denied")

    @staticmethod
    def _expect_status(
        communication: CandidateCommunicationRecord, expected: str | set[str]
    ) -> None:
        allowed = {expected} if isinstance(expected, str) else expected
        if communication.status not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"Communication cannot transition from {communication.status}",
            )

    @staticmethod
    def _load_target(session, application_id: str, context: TenantContext):
        application = session.execute(
            select(RecruitmentApplicationRecord).where(
                RecruitmentApplicationRecord.id == application_id,
                RecruitmentApplicationRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        if application is None:
            raise HTTPException(status_code=404, detail="Application not found")
        campaign = session.execute(
            select(RecruitmentCampaignRecord).where(
                RecruitmentCampaignRecord.id == application.campaign_id,
                RecruitmentCampaignRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        candidate = session.execute(
            select(CandidateRecord).where(
                CandidateRecord.id == application.candidate_id,
                CandidateRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        if campaign is None or candidate is None:
            raise HTTPException(status_code=404, detail="Application not found")
        return application, campaign, candidate

    @staticmethod
    def _load_communication(session, communication_id: str, context: TenantContext):
        communication = session.execute(
            select(CandidateCommunicationRecord).where(
                CandidateCommunicationRecord.id == communication_id,
                CandidateCommunicationRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        if communication is None:
            raise HTTPException(status_code=404, detail="Communication not found")
        campaign = session.execute(
            select(RecruitmentCampaignRecord).where(
                RecruitmentCampaignRecord.id == communication.campaign_id,
                RecruitmentCampaignRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        if campaign is None:
            raise HTTPException(status_code=404, detail="Communication not found")
        return communication, campaign

    @classmethod
    def _serialize(
        cls, session, communication: CandidateCommunicationRecord
    ) -> CommunicationResponse:
        events = session.execute(
            select(CandidateCommunicationEventRecord)
            .where(
                CandidateCommunicationEventRecord.communication_id == communication.id,
                CandidateCommunicationEventRecord.organization_id
                == communication.organization_id,
            )
            .order_by(CandidateCommunicationEventRecord.created_at)
        ).scalars()
        return CommunicationResponse(
            id=communication.id,
            campaign_id=communication.campaign_id,
            application_id=communication.application_id,
            idempotency_key=communication.idempotency_key,
            template_type=communication.template_type,
            template_version=communication.template_version,
            recipient_email=communication.recipient_email,
            subject=communication.subject,
            body=communication.body,
            status=communication.status,
            created_by_user_id=communication.created_by_user_id,
            approved_by_user_id=communication.approved_by_user_id,
            approved_at=communication.approved_at,
            attempt_count=communication.attempt_count,
            max_attempts=communication.max_attempts,
            provider_name=communication.provider_name,
            delivery_status=communication.delivery_status,
            provider_reference=communication.provider_reference,
            failure_code=communication.failure_code,
            created_at=communication.created_at,
            updated_at=communication.updated_at,
            sent_at=communication.sent_at,
            events=[
                CommunicationEventResponse(
                    event_type=event.event_type,
                    outcome=event.outcome,
                    attempt_number=event.attempt_number,
                    provider_name=event.provider_name,
                    details=json.loads(event.details_json),
                    created_at=event.created_at,
                )
                for event in events
            ],
        )

    @staticmethod
    def _render(template: str, values: dict[str, str], *, subject: bool) -> str:
        try:
            parsed = Template(template)
            placeholders = {
                match.group("named") or match.group("braced")
                for match in parsed.pattern.finditer(template)
                if match.group("named") or match.group("braced")
            }
            if placeholders - ALLOWED_PLACEHOLDERS:
                raise ValueError("Unknown template placeholder")
            rendered = parsed.substitute(values).strip()
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="Template contains an invalid placeholder"
            ) from exc
        if not rendered or "\x00" in rendered:
            raise HTTPException(status_code=422, detail="Rendered message is invalid")
        if subject:
            if len(rendered) > 255 or "\r" in rendered or "\n" in rendered:
                raise HTTPException(status_code=422, detail="Rendered subject is invalid")
        elif len(rendered) > MAX_BODY_CHARS:
            raise HTTPException(status_code=422, detail="Rendered message body is too long")
        return rendered

    @staticmethod
    def _validate_recipient(address: str) -> str:
        normalized = address.strip()
        if (
            len(normalized) > 320
            or not EMAIL_PATTERN.fullmatch(normalized)
            or ".." in normalized
        ):
            raise HTTPException(status_code=422, detail="Candidate email address is invalid")
        return normalized

    @staticmethod
    def _safe_single_line(value: str, limit: int) -> str:
        return " ".join(value.split())[:limit]

    @staticmethod
    def _safe_failure_code(value: str) -> str:
        return (
            value
            if isinstance(value, str)
            and value in SAFE_PROVIDER_ERROR_CLASSES
            else "provider_failure"
        )

    @staticmethod
    def _safe_provider_name(value: str) -> str:
        return (
            value
            if isinstance(value, str)
            and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value)
            else "unknown_provider"
        )

    @staticmethod
    def _safe_provider_reference(value: object) -> str | None:
        return (
            value
            if isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value)
            else None
        )

    @staticmethod
    def _record_transition(
        session,
        communication: CandidateCommunicationRecord,
        context: TenantContext | None,
        event_type: str,
        outcome: str,
        prior_status: str | None,
        new_status: str | None,
        *,
        attempt_number: int | None = None,
        provider_name: str | None = None,
        details: dict[str, str | int | bool] | None = None,
    ) -> None:
        details = {
            "from_status": prior_status,
            "to_status": new_status,
            **(details or {}),
        }
        session.add(
            CandidateCommunicationEventRecord(
                organization_id=communication.organization_id,
                communication_id=communication.id,
                actor_user_id=(
                    None
                    if context is None or context.is_automated
                    else context.actor_user_id
                ),
                event_type=event_type,
                outcome=outcome,
                attempt_number=attempt_number,
                provider_name=provider_name,
                details_json=json.dumps(details, sort_keys=True),
                created_at=datetime.now(UTC),
            )
        )

    @staticmethod
    def _audit(
        session,
        communication: CandidateCommunicationRecord,
        context: TenantContext,
        action: str,
    ) -> None:
        session.add(
            AccessAuditRecord(
                actor_user_id=None if context.is_automated else context.actor_user_id,
                organization_id=communication.organization_id,
                requested_organization_id=communication.organization_id,
                action=action,
                resource_type="candidate_communication",
                resource_id=communication.id,
                outcome="allowed",
                support_reason=context.support_reason,
            )
        )

    @staticmethod
    def _audit_system(
        session, communication: CandidateCommunicationRecord, action: str
    ) -> None:
        session.add(
            AccessAuditRecord(
                actor_user_id=None,
                organization_id=communication.organization_id,
                requested_organization_id=communication.organization_id,
                action=action,
                resource_type="candidate_communication",
                resource_id=communication.id,
                outcome="allowed",
            )
        )
