"""Tenant-scoped candidate intake, durable processing, and human review."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError

from config import settings
from services.candidate_cv_parser import (
    PARSER_VERSION,
    CVDocumentError,
    parse_candidate_cv,
    validate_cv_upload,
)
from services.candidate_cv_storage import CandidateCVStorage
from services.candidate_screening import (
    CandidateScreeningResult,
    IncompatibleRubricError,
    assess_candidate_cv,
)
from services.database import (
    CAMPAIGN_WORKER_CONTROL_ID,
    AccessAuditRecord,
    CandidateCriterionAssessmentRecord,
    CandidateCVDocumentRecord,
    CandidateRecord,
    CandidateScreeningRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignExecutionRecord,
    RecruitmentCampaignRecord,
    RecruitmentCampaignWorkerControlRecord,
    SessionLocal,
)
from services.recruitment_campaign_service import RecruitmentCampaignService
from services.tenant_authorization import TenantContext

logger = logging.getLogger(__name__)
MAX_APPLICATION_ATTEMPTS = 3


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    campaign_id: str
    status: str
    attempt_count: int
    failure_code: str | None
    failure_message: str | None
    review_status: str
    created_at: datetime
    updated_at: datetime
    document: dict[str, Any] | None = None
    parsed_cv: dict[str, Any] | None = None
    screening: dict[str, Any] | None = None


class CriterionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1, max_length=120)
    status: Literal["confirmed", "needs_correction"]
    note: str | None = Field(default=None, max_length=1000)


class ApplicationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criteria: list[CriterionReview] = Field(min_length=1, max_length=100)


class CandidateApplicationService:
    @classmethod
    def submit_cv(
        cls,
        campaign_id: str,
        context: TenantContext,
        filename: str,
        media_type: str | None,
        content: bytes,
    ) -> ApplicationResponse:
        if context.role != "candidate":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the authenticated candidate may submit their CV",
            )
        if len(content) > settings.CV_MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="CV exceeds the maximum upload size",
            )
        existing = cls._authorized_submission_target(campaign_id, context)
        try:
            extension = validate_cv_upload(filename, media_type, content)
        except CVDocumentError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        digest = sha256(content).hexdigest()

        if existing is not None:
            prior = cls._existing_candidate_application(
                campaign_id, context.organization_id, existing.id
            )
            if prior is not None:
                with SessionLocal() as session:
                    document = session.execute(
                        select(CandidateCVDocumentRecord).where(
                            CandidateCVDocumentRecord.application_id == prior.id,
                            CandidateCVDocumentRecord.organization_id
                            == context.organization_id,
                        )
                    ).scalar_one()
                    if document.sha256 == digest:
                        return cls._serialize(session, prior)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An application already exists for this campaign",
                )

        storage_key = CandidateCVStorage.write(content, extension)
        stored = False
        application_id = str(uuid4())
        now = datetime.now(UTC)
        try:
            with SessionLocal.begin() as session:
                campaign, candidate = cls._load_submission_target(
                    session, campaign_id, context
                )
                session.add(
                    RecruitmentApplicationRecord(
                        id=application_id,
                        organization_id=context.organization_id,
                        campaign_id=campaign.id,
                        candidate_id=candidate.id,
                        status="pending",
                        attempt_count=0,
                        max_attempts=MAX_APPLICATION_ATTEMPTS,
                        review_status="pending",
                        created_at=now,
                        updated_at=now,
                    )
                )
                # The models declare no relationship(), so the unit of work does
                # not order these INSERTs; PostgreSQL enforces the CV document's
                # foreign key to the application, SQLite does not.
                session.flush()
                session.add(
                    CandidateCVDocumentRecord(
                        id=str(uuid4()),
                        organization_id=context.organization_id,
                        application_id=application_id,
                        storage_key=storage_key,
                        file_extension=extension,
                        media_type=(
                            "application/pdf"
                            if extension == ".pdf"
                            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                        ),
                        byte_size=len(content),
                        sha256=digest,
                        parse_status="pending",
                        created_at=now,
                    )
                )
                session.add(
                    AccessAuditRecord(
                        actor_user_id=context.actor_user_id,
                        organization_id=context.organization_id,
                        requested_organization_id=context.organization_id,
                        action="candidate_cv_submitted",
                        resource_type="recruitment_application",
                        resource_id=application_id,
                        outcome="allowed",
                        support_reason=context.support_reason,
                    )
                )
                session.flush()
                application = session.get(RecruitmentApplicationRecord, application_id)
                assert application is not None
                response = cls._serialize(session, application)
            stored = True
            logger.info(
                "Candidate CV accepted for processing",
                extra={
                    "application_id": application_id,
                    "campaign_id": campaign_id,
                    "organization_id": context.organization_id,
                },
            )
            return response
        except IntegrityError as exc:
            prior = cls._existing_candidate_application(
                campaign_id, context.organization_id, existing.id
            )
            if prior is not None:
                with SessionLocal() as session:
                    document = session.execute(
                        select(CandidateCVDocumentRecord).where(
                            CandidateCVDocumentRecord.application_id == prior.id,
                            CandidateCVDocumentRecord.organization_id
                            == context.organization_id,
                        )
                    ).scalar_one()
                    if document.sha256 == digest:
                        return cls._serialize(session, prior)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An application already exists for this campaign",
            ) from exc
        finally:
            if not stored:
                CandidateCVStorage.delete(storage_key)

    @staticmethod
    def _authorized_submission_target(
        campaign_id: str, context: TenantContext
    ) -> CandidateRecord | None:
        with SessionLocal() as session:
            campaign, candidate = CandidateApplicationService._load_submission_target(
                session, campaign_id, context
            )
            return candidate

    @staticmethod
    def _load_submission_target(
        session, campaign_id: str, context: TenantContext
    ) -> tuple[RecruitmentCampaignRecord, CandidateRecord]:
        campaign = session.execute(
            select(RecruitmentCampaignRecord).where(
                RecruitmentCampaignRecord.id == campaign_id,
                RecruitmentCampaignRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        if campaign is None:
            raise HTTPException(status_code=404, detail="Campaign not found")
        if campaign.approval_state != "approved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Applications are accepted only for approved campaigns",
            )
        candidate = session.execute(
            select(CandidateRecord).where(
                CandidateRecord.organization_id == context.organization_id,
                CandidateRecord.user_id == context.actor_user_id,
            )
        ).scalar_one_or_none()
        if candidate is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Candidate profile is not linked to this organization",
            )
        return campaign, candidate

    @staticmethod
    def _existing_candidate_application(
        campaign_id: str, organization_id: str, candidate_id: str
    ) -> RecruitmentApplicationRecord | None:
        with SessionLocal() as session:
            return session.execute(
                select(RecruitmentApplicationRecord).where(
                    RecruitmentApplicationRecord.campaign_id == campaign_id,
                    RecruitmentApplicationRecord.organization_id == organization_id,
                    RecruitmentApplicationRecord.candidate_id == candidate_id,
                )
            ).scalar_one_or_none()

    @classmethod
    def discover_eligible(cls, limit: int) -> list[str]:
        if limit < 1:
            return []
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            exhausted = session.execute(
                select(
                    RecruitmentApplicationRecord.id,
                    RecruitmentApplicationRecord.organization_id,
                    RecruitmentCampaignRecord.approval_state,
                )
                .join(
                    RecruitmentCampaignRecord,
                    and_(
                        RecruitmentCampaignRecord.id
                        == RecruitmentApplicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == RecruitmentApplicationRecord.organization_id,
                    ),
                )
                .where(
                    RecruitmentApplicationRecord.status == "running",
                    RecruitmentApplicationRecord.lease_expires_at <= now,
                    RecruitmentApplicationRecord.attempt_count
                    >= RecruitmentApplicationRecord.max_attempts,
                )
                .order_by(RecruitmentApplicationRecord.updated_at)
                .limit(limit)
            ).all()
            for application_id, organization_id, approval_state in exhausted:
                failure_code = (
                    "campaign_approval_revoked"
                    if approval_state != "approved"
                    else "processing_attempts_exhausted"
                )
                failure_message = (
                    "Campaign approval is no longer active; CV processing stopped."
                    if approval_state != "approved"
                    else "The processing lease expired after the maximum attempts."
                )
                result = session.execute(
                    update(RecruitmentApplicationRecord)
                    .where(
                        RecruitmentApplicationRecord.id == application_id,
                        RecruitmentApplicationRecord.organization_id
                        == organization_id,
                        RecruitmentApplicationRecord.status == "running",
                        RecruitmentApplicationRecord.lease_expires_at <= now,
                        RecruitmentApplicationRecord.attempt_count
                        >= RecruitmentApplicationRecord.max_attempts,
                    )
                    .values(
                        status="needs_attention",
                        failure_code=failure_code,
                        failure_message=failure_message,
                        lease_token=None,
                        lease_expires_at=None,
                        available_at=None,
                        updated_at=now,
                    )
                )
                if result.rowcount == 1:
                    cls._add_processing_audit(
                        session,
                        application_id,
                        organization_id,
                        "candidate_cv_processing_needs_attention",
                    )
        due = and_(
            RecruitmentApplicationRecord.status == "retryable_failure",
            RecruitmentApplicationRecord.available_at <= now,
        )
        expired = and_(
            RecruitmentApplicationRecord.status == "running",
            RecruitmentApplicationRecord.lease_expires_at <= now,
        )
        with SessionLocal() as session:
            application_ids = list(
                session.execute(
                    select(RecruitmentApplicationRecord.id)
                    .join(
                        RecruitmentCampaignRecord,
                        and_(
                            RecruitmentCampaignRecord.id
                            == RecruitmentApplicationRecord.campaign_id,
                            RecruitmentCampaignRecord.organization_id
                            == RecruitmentApplicationRecord.organization_id,
                        ),
                    )
                    .where(
                        or_(
                            RecruitmentCampaignRecord.approval_state == "approved",
                            RecruitmentApplicationRecord.status.in_(
                                ("pending", "retryable_failure", "running")
                            ),
                        ),
                        ~exists().where(
                            RecruitmentCampaignExecutionRecord.campaign_id
                            == RecruitmentCampaignRecord.id,
                            RecruitmentCampaignExecutionRecord.organization_id
                            == RecruitmentCampaignRecord.organization_id,
                            RecruitmentCampaignExecutionRecord.status == "completed",
                        ),
                        or_(
                            RecruitmentApplicationRecord.status == "pending",
                            due,
                            expired,
                        ),
                        RecruitmentApplicationRecord.attempt_count
                        < RecruitmentApplicationRecord.max_attempts,
                    )
                    .order_by(RecruitmentApplicationRecord.created_at)
                    .limit(limit)
                ).scalars()
            )
            return [
                application_id
                for application_id in application_ids
                if not cls._campaign_target_achieved(session, application_id)
            ]

    @staticmethod
    def _campaign_target_achieved(session, application_id: str) -> bool:
        campaign = session.execute(
            select(RecruitmentCampaignRecord)
            .join(
                RecruitmentApplicationRecord,
                and_(
                    RecruitmentApplicationRecord.campaign_id
                    == RecruitmentCampaignRecord.id,
                    RecruitmentApplicationRecord.organization_id
                    == RecruitmentCampaignRecord.organization_id,
                ),
            )
            .where(RecruitmentApplicationRecord.id == application_id)
        ).scalar_one_or_none()
        return campaign is not None and RecruitmentCampaignService.target_is_achieved(
            session, campaign
        )

    @classmethod
    def process_dispatched(cls, application_id: str) -> ApplicationResponse | None:
        token = str(uuid4())
        with SessionLocal() as session:
            target = session.execute(
                select(
                    RecruitmentApplicationRecord.organization_id,
                    RecruitmentApplicationRecord.campaign_id,
                    RecruitmentCampaignRecord.approval_state,
                )
                .join(
                    RecruitmentCampaignRecord,
                    and_(
                        RecruitmentCampaignRecord.id
                        == RecruitmentApplicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == RecruitmentApplicationRecord.organization_id,
                    ),
                )
                .where(RecruitmentApplicationRecord.id == application_id)
            ).one_or_none()
        if target is None:
            return None
        if target.approval_state != "approved":
            return cls._mark_approval_revoked(
                application_id, target.organization_id
            )

        with SessionLocal.begin() as session:
            control = session.get(
                RecruitmentCampaignWorkerControlRecord, CAMPAIGN_WORKER_CONTROL_ID
            )
            if control is not None and control.paused:
                return None
            campaign = session.execute(
                select(RecruitmentCampaignRecord).where(
                    RecruitmentCampaignRecord.id == target.campaign_id,
                    RecruitmentCampaignRecord.organization_id
                    == target.organization_id,
                )
            ).scalar_one_or_none()
            if (
                campaign is not None
                and campaign.approval_state == "approved"
                and RecruitmentCampaignService.target_is_achieved(session, campaign)
            ):
                return None
            claim_time = datetime.now(UTC)
            claim = session.execute(
                update(RecruitmentApplicationRecord)
                .where(
                    RecruitmentApplicationRecord.id == application_id,
                    RecruitmentApplicationRecord.organization_id
                    == target.organization_id,
                    RecruitmentApplicationRecord.attempt_count
                    < RecruitmentApplicationRecord.max_attempts,
                    or_(
                        RecruitmentApplicationRecord.status == "pending",
                        and_(
                            RecruitmentApplicationRecord.status == "retryable_failure",
                            RecruitmentApplicationRecord.available_at <= claim_time,
                        ),
                        and_(
                            RecruitmentApplicationRecord.status == "running",
                            RecruitmentApplicationRecord.lease_expires_at <= claim_time,
                        ),
                    ),
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == RecruitmentApplicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == RecruitmentApplicationRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state == "approved",
                    ),
                    ~exists().where(
                        RecruitmentCampaignExecutionRecord.campaign_id
                        == RecruitmentApplicationRecord.campaign_id,
                        RecruitmentCampaignExecutionRecord.organization_id
                        == RecruitmentApplicationRecord.organization_id,
                        RecruitmentCampaignExecutionRecord.status == "completed",
                    ),
                    ~exists().where(
                        RecruitmentCampaignWorkerControlRecord.id
                        == CAMPAIGN_WORKER_CONTROL_ID,
                        RecruitmentCampaignWorkerControlRecord.paused.is_(True),
                    ),
                )
                .values(
                    status="running",
                    attempt_count=RecruitmentApplicationRecord.attempt_count + 1,
                    lease_token=token,
                    lease_expires_at=claim_time
                    + timedelta(seconds=settings.CAMPAIGN_WORKER_LEASE_SECONDS),
                    available_at=None,
                    failure_code=None,
                    failure_message=None,
                    updated_at=claim_time,
                )
            )
            if claim.rowcount != 1:
                return None

        with SessionLocal() as session:
            application = session.execute(
                select(RecruitmentApplicationRecord).where(
                    RecruitmentApplicationRecord.id == application_id,
                    RecruitmentApplicationRecord.organization_id
                    == target.organization_id,
                    RecruitmentApplicationRecord.lease_token == token,
                )
            ).scalar_one_or_none()
            document = session.execute(
                select(CandidateCVDocumentRecord).where(
                    CandidateCVDocumentRecord.application_id == application_id,
                    CandidateCVDocumentRecord.organization_id == target.organization_id,
                )
            ).scalar_one_or_none()
            campaign = session.execute(
                select(RecruitmentCampaignRecord).where(
                    RecruitmentCampaignRecord.id == target.campaign_id,
                    RecruitmentCampaignRecord.organization_id == target.organization_id,
                )
            ).scalar_one_or_none()
            if application is None or document is None or campaign is None:
                return None
            storage_key = document.storage_key
            extension = document.file_extension
            rubric = json.loads(campaign.competency_rubric_json)
            attempt = application.attempt_count

        try:
            content = CandidateCVStorage.read(storage_key)
            extracted_text, parsed = parse_candidate_cv(content, extension)
            screening = assess_candidate_cv(rubric, extracted_text)
        except CVDocumentError as exc:
            return cls._finish_failure(
                application_id, target.organization_id, token, exc.code, str(exc), permanent=True
            )
        except IncompatibleRubricError as exc:
            return cls._finish_attention(
                application_id, target.organization_id, token,
                "incompatible_approved_rubric", str(exc),
                extracted_text=extracted_text,
                parsed_output=parsed.model_dump(mode="json"),
            )
        except OSError:
            retry_at = cls._retry_time(attempt)
            return cls._finish_failure(
                application_id, target.organization_id, token,
                "cv_storage_unavailable",
                "Private CV storage was unavailable while processing the application.",
                permanent=retry_at is None,
                available_at=retry_at,
            )
        return cls._persist_screening(
            application_id,
            target.organization_id,
            token,
            extracted_text,
            parsed.model_dump(mode="json"),
            screening,
        )

    @staticmethod
    def _retry_time(attempt: int) -> datetime | None:
        if attempt >= MAX_APPLICATION_ATTEMPTS:
            return None
        delay = min(
            settings.CAMPAIGN_WORKER_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
            settings.CAMPAIGN_WORKER_RETRY_MAX_SECONDS,
        )
        return datetime.now(UTC) + timedelta(seconds=delay)

    @classmethod
    def _finish_failure(
        cls,
        application_id: str,
        organization_id: str,
        token: str,
        code: str,
        message: str,
        *,
        permanent: bool,
        available_at: datetime | None = None,
    ) -> ApplicationResponse | None:
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            row = session.execute(
                update(RecruitmentApplicationRecord)
                .where(
                    RecruitmentApplicationRecord.id == application_id,
                    RecruitmentApplicationRecord.organization_id == organization_id,
                    RecruitmentApplicationRecord.status == "running",
                    RecruitmentApplicationRecord.lease_token == token,
                    RecruitmentApplicationRecord.lease_expires_at > now,
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == RecruitmentApplicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == RecruitmentApplicationRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state == "approved",
                    ),
                )
                .values(
                    status="failed" if permanent else "retryable_failure",
                    failure_code=code,
                    failure_message=message[:1000],
                    lease_token=None,
                    lease_expires_at=None,
                    available_at=available_at,
                    updated_at=now,
                )
            )
            if row.rowcount != 1:
                return None
            application = session.get(RecruitmentApplicationRecord, application_id)
            assert application is not None
            if permanent:
                document = session.execute(
                    select(CandidateCVDocumentRecord).where(
                        CandidateCVDocumentRecord.application_id == application_id,
                        CandidateCVDocumentRecord.organization_id == organization_id,
                    )
                ).scalar_one()
                document.parse_status = "failed"
                document.parse_error_code = code
            return cls._serialize(session, application)

    @classmethod
    def _finish_attention(
        cls,
        application_id: str,
        organization_id: str,
        token: str,
        code: str,
        message: str,
        *,
        extracted_text: str,
        parsed_output: dict[str, Any],
    ) -> ApplicationResponse | None:
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            result = session.execute(
                update(RecruitmentApplicationRecord)
                .where(
                    RecruitmentApplicationRecord.id == application_id,
                    RecruitmentApplicationRecord.organization_id == organization_id,
                    RecruitmentApplicationRecord.status == "running",
                    RecruitmentApplicationRecord.lease_token == token,
                    RecruitmentApplicationRecord.lease_expires_at > now,
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == RecruitmentApplicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == RecruitmentApplicationRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state == "approved",
                    ),
                )
                .values(
                    status="needs_attention",
                    failure_code=code,
                    failure_message=message[:1000],
                    lease_token=None,
                    lease_expires_at=None,
                    available_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return None
            document = session.execute(
                select(CandidateCVDocumentRecord).where(
                    CandidateCVDocumentRecord.application_id == application_id,
                    CandidateCVDocumentRecord.organization_id == organization_id,
                )
            ).scalar_one()
            document.parse_status = "parsed"
            document.parser_version = PARSER_VERSION
            document.parsed_output_json = json.dumps(
                parsed_output, ensure_ascii=False
            )
            document.extracted_text = extracted_text
            document.parse_error_code = None
            document.parsed_at = now
            cls._add_processing_audit(
                session,
                application_id,
                organization_id,
                "candidate_cv_screening_needs_attention",
            )
            application = session.get(RecruitmentApplicationRecord, application_id)
            assert application is not None
            return cls._serialize(session, application)

    @classmethod
    def _persist_screening(
        cls,
        application_id: str,
        organization_id: str,
        token: str,
        extracted_text: str,
        parsed_output: dict[str, Any],
        screening: CandidateScreeningResult,
    ) -> ApplicationResponse | None:
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            result = session.execute(
                update(RecruitmentApplicationRecord)
                .where(
                    RecruitmentApplicationRecord.id == application_id,
                    RecruitmentApplicationRecord.organization_id == organization_id,
                    RecruitmentApplicationRecord.status == "running",
                    RecruitmentApplicationRecord.lease_token == token,
                    RecruitmentApplicationRecord.lease_expires_at > now,
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == RecruitmentApplicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == RecruitmentApplicationRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state == "approved",
                    ),
                )
                .values(
                    status="needs_review",
                    review_status="pending",
                    lease_token=None,
                    lease_expires_at=None,
                    failure_code=None,
                    failure_message=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                cls._mark_revoked_in_session(session, application_id, organization_id, token)
                return None
            application = session.get(RecruitmentApplicationRecord, application_id)
            document = session.execute(
                select(CandidateCVDocumentRecord).where(
                    CandidateCVDocumentRecord.application_id == application_id,
                    CandidateCVDocumentRecord.organization_id == organization_id,
                )
            ).scalar_one()
            document.parse_status = "parsed"
            document.parser_version = PARSER_VERSION
            document.parsed_output_json = json.dumps(parsed_output, ensure_ascii=False)
            document.extracted_text = extracted_text
            document.parse_error_code = None
            document.parsed_at = now

            existing = session.execute(
                select(CandidateScreeningRecord).where(
                    CandidateScreeningRecord.application_id == application_id,
                    CandidateScreeningRecord.organization_id == organization_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                result_record = CandidateScreeningRecord(
                    id=str(uuid4()),
                    organization_id=organization_id,
                    application_id=application_id,
                    rubric_version=screening.rubric_version,
                    rubric_snapshot_json=json.dumps(
                        screening.rubric_snapshot, ensure_ascii=False, sort_keys=True
                    ),
                    result_source=screening.result_source,
                    review_required=True,
                    assessed_at=now,
                )
                session.add(result_record)
                session.flush()
                session.add_all(
                    CandidateCriterionAssessmentRecord(
                        id=str(uuid4()),
                        screening_id=result_record.id,
                        criterion_id=assessment.criterion_id,
                        criterion_title=assessment.criterion_title,
                        score=assessment.score,
                        evidence_json=json.dumps(
                            [item.model_dump(mode="json") for item in assessment.evidence],
                            ensure_ascii=False,
                        ),
                        explanation=assessment.explanation,
                        uncertainty=assessment.uncertainty,
                        review_required=True,
                    )
                    for assessment in screening.assessments
                )
            session.flush()
            session.add(
                AccessAuditRecord(
                    actor_user_id=None,
                    organization_id=organization_id,
                    requested_organization_id=organization_id,
                    action="candidate_cv_screened",
                    resource_type="recruitment_application",
                    resource_id=application_id,
                    outcome="allowed",
                )
            )
            assert application is not None
            response = cls._serialize(session, application)
        logger.info(
            "Candidate CV processing completed for human review",
            extra={
                "application_id": application_id,
                "organization_id": organization_id,
                "status": "needs_review",
            },
        )
        return response

    @classmethod
    def _mark_approval_revoked(
        cls, application_id: str, organization_id: str
    ) -> ApplicationResponse | None:
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            result = session.execute(
                update(RecruitmentApplicationRecord)
                .where(
                    RecruitmentApplicationRecord.id == application_id,
                    RecruitmentApplicationRecord.organization_id == organization_id,
                    RecruitmentApplicationRecord.status.in_(
                        ("pending", "retryable_failure", "running")
                    ),
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == RecruitmentApplicationRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == RecruitmentApplicationRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state != "approved",
                    ),
                )
                .values(
                    status="needs_attention",
                    failure_code="campaign_approval_revoked",
                    failure_message="Campaign approval is no longer active; CV processing stopped.",
                    lease_token=None,
                    lease_expires_at=None,
                    available_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return None
            cls._add_processing_audit(
                session, application_id, organization_id, "candidate_cv_processing_stopped"
            )
            application = session.get(RecruitmentApplicationRecord, application_id)
            assert application is not None
            return cls._serialize(session, application)

    @staticmethod
    def _mark_revoked_in_session(
        session, application_id: str, organization_id: str, token: str
    ) -> None:
        now = datetime.now(UTC)
        result = session.execute(
            update(RecruitmentApplicationRecord)
            .where(
                RecruitmentApplicationRecord.id == application_id,
                RecruitmentApplicationRecord.organization_id == organization_id,
                RecruitmentApplicationRecord.status == "running",
                RecruitmentApplicationRecord.lease_token == token,
                exists().where(
                    RecruitmentCampaignRecord.id
                    == RecruitmentApplicationRecord.campaign_id,
                    RecruitmentCampaignRecord.organization_id
                    == RecruitmentApplicationRecord.organization_id,
                    RecruitmentCampaignRecord.approval_state != "approved",
                ),
            )
            .values(
                status="needs_attention",
                failure_code="campaign_approval_revoked",
                failure_message="Campaign approval is no longer active; CV processing stopped.",
                lease_token=None,
                lease_expires_at=None,
                available_at=None,
                updated_at=now,
            )
        )
        if result.rowcount == 1:
            CandidateApplicationService._add_processing_audit(
                session, application_id, organization_id,
                "candidate_cv_processing_stopped",
            )

    @staticmethod
    def _add_processing_audit(
        session, application_id: str, organization_id: str, action: str
    ) -> None:
        session.add(
            AccessAuditRecord(
                actor_user_id=None,
                organization_id=organization_id,
                requested_organization_id=organization_id,
                action=action,
                resource_type="recruitment_application",
                resource_id=application_id,
                outcome="allowed",
            )
        )

    @classmethod
    def get_application(
        cls, application_id: str, context: TenantContext
    ) -> ApplicationResponse:
        if context.role not in {"candidate", "recruiter", "org_admin", "platform_admin"}:
            raise HTTPException(status_code=403, detail="Application access denied")
        with SessionLocal() as session:
            application = session.execute(
                select(RecruitmentApplicationRecord).where(
                    RecruitmentApplicationRecord.id == application_id,
                    RecruitmentApplicationRecord.organization_id
                    == context.organization_id,
                )
            ).scalar_one_or_none()
            if application is None:
                raise HTTPException(status_code=404, detail="Application not found")
            campaign = session.execute(
                select(RecruitmentCampaignRecord).where(
                    RecruitmentCampaignRecord.id == application.campaign_id,
                    RecruitmentCampaignRecord.organization_id
                    == context.organization_id,
                )
            ).scalar_one_or_none()
            if campaign is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if context.role == "candidate":
                candidate = session.execute(
                    select(CandidateRecord.id).where(
                        CandidateRecord.id == application.candidate_id,
                        CandidateRecord.organization_id == context.organization_id,
                        CandidateRecord.user_id == context.actor_user_id,
                    )
                ).scalar_one_or_none()
                if candidate is None:
                    raise HTTPException(status_code=404, detail="Application not found")
            else:
                RecruitmentCampaignService._assert_authorized(campaign, context)
            return cls._serialize(session, application)

    @classmethod
    def review_application(
        cls,
        application_id: str,
        context: TenantContext,
        payload: ApplicationReviewRequest,
    ) -> ApplicationResponse:
        if context.role not in {"recruiter", "org_admin", "platform_admin"}:
            raise HTTPException(status_code=403, detail="Application review access denied")
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            application = session.execute(
                select(RecruitmentApplicationRecord).where(
                    RecruitmentApplicationRecord.id == application_id,
                    RecruitmentApplicationRecord.organization_id
                    == context.organization_id,
                )
            ).scalar_one_or_none()
            if application is None:
                raise HTTPException(status_code=404, detail="Application not found")
            campaign = session.execute(
                select(RecruitmentCampaignRecord).where(
                    RecruitmentCampaignRecord.id == application.campaign_id,
                    RecruitmentCampaignRecord.organization_id
                    == context.organization_id,
                )
            ).scalar_one_or_none()
            if campaign is None:
                raise HTTPException(status_code=404, detail="Application not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            screening = session.execute(
                select(CandidateScreeningRecord).where(
                    CandidateScreeningRecord.application_id == application.id,
                    CandidateScreeningRecord.organization_id == context.organization_id,
                )
            ).scalar_one_or_none()
            if screening is None:
                raise HTTPException(status_code=409, detail="Application has no screening result to review")
            assessments = {
                item.criterion_id: item
                for item in session.execute(
                    select(CandidateCriterionAssessmentRecord).where(
                        CandidateCriterionAssessmentRecord.screening_id == screening.id
                    )
                ).scalars()
            }
            for review in payload.criteria:
                assessment = assessments.get(review.criterion_id)
                if assessment is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Unknown criterion: {review.criterion_id}",
                    )
                assessment.review_status = review.status
                assessment.review_note = review.note
                assessment.reviewed_by_user_id = context.actor_user_id
                assessment.reviewed_at = now
            application.review_status = (
                "reviewed"
                if all(
                    assessment.review_status != "pending"
                    for assessment in assessments.values()
                )
                else "pending"
            )
            application.reviewed_by_user_id = (
                context.actor_user_id if application.review_status == "reviewed" else None
            )
            application.reviewed_at = (
                now if application.review_status == "reviewed" else None
            )
            application.updated_at = now
            session.add(
                AccessAuditRecord(
                    actor_user_id=context.actor_user_id,
                    organization_id=context.organization_id,
                    requested_organization_id=context.organization_id,
                    action="candidate_screening_reviewed",
                    resource_type="recruitment_application",
                    resource_id=application.id,
                    outcome="allowed",
                    support_reason=context.support_reason,
                )
            )
            return cls._serialize(session, application)

    @staticmethod
    def _serialize(session, application: RecruitmentApplicationRecord) -> ApplicationResponse:
        document = session.execute(
            select(CandidateCVDocumentRecord).where(
                CandidateCVDocumentRecord.application_id == application.id,
                CandidateCVDocumentRecord.organization_id == application.organization_id,
            )
        ).scalar_one_or_none()
        parsed_cv = (
            None
            if document is None or document.parsed_output_json is None
            else json.loads(document.parsed_output_json)
        )
        screening = session.execute(
            select(CandidateScreeningRecord).where(
                CandidateScreeningRecord.application_id == application.id,
                CandidateScreeningRecord.organization_id == application.organization_id,
            )
        ).scalar_one_or_none()
        screening_json: dict[str, Any] | None = None
        if screening is not None:
            assessments = session.execute(
                select(CandidateCriterionAssessmentRecord)
                .where(CandidateCriterionAssessmentRecord.screening_id == screening.id)
                .order_by(CandidateCriterionAssessmentRecord.criterion_id)
            ).scalars()
            screening_json = {
                "rubric_version": screening.rubric_version,
                "rubric_snapshot": json.loads(screening.rubric_snapshot_json),
                "result_source": screening.result_source,
                "synthetic": screening.result_source == "mock",
                "review_required": screening.review_required,
                "assessed_at": screening.assessed_at.isoformat(),
                "assessments": [
                    {
                        "criterion_id": item.criterion_id,
                        "criterion_title": item.criterion_title,
                        "score": item.score,
                        "evidence": json.loads(item.evidence_json),
                        "explanation": item.explanation,
                        "uncertainty": item.uncertainty,
                        "review_required": item.review_required,
                        "review_status": item.review_status,
                        "review_note": item.review_note,
                        "reviewed_by_user_id": item.reviewed_by_user_id,
                        "reviewed_at": (
                            None if item.reviewed_at is None else item.reviewed_at.isoformat()
                        ),
                    }
                    for item in assessments
                ],
            }
        return ApplicationResponse(
            id=application.id,
            campaign_id=application.campaign_id,
            status=application.status,
            attempt_count=application.attempt_count,
            failure_code=application.failure_code,
            failure_message=application.failure_message,
            review_status=application.review_status,
            created_at=application.created_at,
            updated_at=application.updated_at,
            document=(
                None
                if document is None
                else {
                    "id": document.id,
                    "file_extension": document.file_extension,
                    "media_type": document.media_type,
                    "byte_size": document.byte_size,
                    "sha256": document.sha256,
                    "parse_status": document.parse_status,
                    "parser_version": document.parser_version,
                    "parse_error_code": document.parse_error_code,
                    "parsed_at": (
                        None if document.parsed_at is None else document.parsed_at.isoformat()
                    ),
                }
            ),
            parsed_cv=parsed_cv,
            screening=screening_json,
        )
