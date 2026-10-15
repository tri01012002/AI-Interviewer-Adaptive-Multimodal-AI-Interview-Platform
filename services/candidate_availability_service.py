"""Durable, tenant-scoped collection of candidate availability preferences."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from services.database import (
    AccessAuditRecord,
    CandidateAvailabilityEventRecord,
    CandidateAvailabilityRequestRecord,
    CandidateAvailabilityResponseRecord,
    CandidateAvailabilitySubmissionRecord,
    CandidateRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignRecord,
    SessionLocal,
)
from services.recruitment_campaign_service import RecruitmentCampaignService
from services.tenant_authorization import TenantContext

MAX_WINDOWS = 10
MIN_REQUEST_LIFETIME = timedelta(minutes=5)
MAX_REQUEST_LIFETIME = timedelta(days=30)
MAX_SLOT_HORIZON = timedelta(days=90)
MIN_DURATION_MINUTES = 15
MAX_DURATION_MINUTES = 240


class AvailabilityWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime


class AvailabilityRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    response_deadline: datetime
    interview_duration_minutes: int = Field(ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)
    timezone_name: str = Field(min_length=1, max_length=64)
    proposed_windows: list[AvailabilityWindow] = Field(min_length=1, max_length=MAX_WINDOWS)


class AvailabilityResponseSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    timezone_name: str = Field(min_length=1, max_length=64)
    windows: list[AvailabilityWindow] = Field(min_length=1, max_length=MAX_WINDOWS)


class AvailabilityReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_window_indices: list[int] = Field(default_factory=list, max_length=MAX_WINDOWS)


class AvailabilityRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    campaign_id: str
    application_id: str
    candidate_id: str
    response_deadline: datetime
    interview_duration_minutes: int
    timezone_name: str
    proposed_windows: list[dict[str, str]]
    status: str
    created_by_user_id: str
    reviewed_by_user_id: str | None
    created_at: datetime
    updated_at: datetime
    response: dict[str, Any] | None


class CandidateAvailabilityService:
    @staticmethod
    def _now(now: datetime | None = None) -> datetime:
        value = now or datetime.now(UTC)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Internal clock must return an aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _zone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="timezone_name must be a valid IANA timezone"
            ) from exc

    @classmethod
    def _normalize_windows(
        cls,
        windows: list[AvailabilityWindow],
        timezone_name: str,
        duration_minutes: int,
        *,
        now: datetime,
    ) -> list[dict[str, str]]:
        zone = cls._zone(timezone_name)
        normalized: list[tuple[datetime, datetime, dict[str, str]]] = []
        duration = timedelta(minutes=duration_minutes)
        for window in windows:
            normalized.append(
                cls._normalize_window(
                    window,
                    zone,
                    duration,
                    now=now,
                )
            )
        normalized.sort(key=lambda item: item[0])
        for previous, current in zip(normalized, normalized[1:], strict=False):
            if current[0] < previous[1]:
                raise HTTPException(status_code=422, detail="Availability windows must not overlap")
        return [item[2] for item in normalized]

    @staticmethod
    def _normalize_window(
        window: AvailabilityWindow,
        zone: ZoneInfo,
        duration: timedelta,
        *,
        now: datetime,
    ) -> tuple[datetime, datetime, dict[str, str]]:
        start, end = window.start, window.end
        if any(value.tzinfo is None or value.utcoffset() is None for value in (start, end)):
            raise HTTPException(
                status_code=422,
                detail="Window timestamps must include an explicit UTC offset",
            )
        for value in (start, end):
            local = value.astimezone(zone)
            if local.replace(tzinfo=None) != value.replace(tzinfo=None):
                raise HTTPException(
                    status_code=422,
                    detail="Timestamp offset does not match timezone_name",
                )
        start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
        if end_utc <= start_utc:
            raise HTTPException(status_code=422, detail="Window end must be after its start")
        if end_utc - start_utc != duration:
            raise HTTPException(
                status_code=422,
                detail="Each availability window must match interview_duration_minutes",
            )
        if start_utc <= now or end_utc > now + MAX_SLOT_HORIZON:
            raise HTTPException(
                status_code=422, detail="Availability window is expired or too far in the future"
            )
        return (
            start_utc,
            end_utc,
            {"start": start_utc.isoformat(), "end": end_utc.isoformat()},
        )

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def create_request(
        cls,
        campaign_id: str,
        context: TenantContext,
        payload: AvailabilityRequestCreate,
        *,
        now: datetime | None = None,
    ) -> AvailabilityRequestResponse:
        cls._require_recruiter(context)
        instant = cls._now(now)
        if (
            payload.response_deadline.tzinfo is None
            or payload.response_deadline.utcoffset() is None
        ):
            raise HTTPException(
                status_code=422, detail="response_deadline must include an explicit UTC offset"
            )
        deadline = payload.response_deadline.astimezone(UTC)
        if not MIN_REQUEST_LIFETIME <= deadline - instant <= MAX_REQUEST_LIFETIME:
            raise HTTPException(
                status_code=422, detail="Response deadline must be 5 minutes to 30 days away"
            )
        windows = cls._normalize_windows(
            payload.proposed_windows,
            payload.timezone_name,
            payload.interview_duration_minutes,
            now=instant,
        )
        digest = cls._digest(
            {
                "campaign_id": campaign_id,
                "application_id": payload.application_id,
                "deadline": deadline.isoformat(),
                "duration": payload.interview_duration_minutes,
                "timezone": payload.timezone_name,
                "windows": windows,
            }
        )
        try:
            with SessionLocal.begin() as session:
                application, campaign, candidate = cls._load_target(
                    session, campaign_id, payload.application_id, context
                )
                RecruitmentCampaignService._assert_authorized(campaign, context)
                cls._assert_eligible(application, campaign)
                prior = session.execute(
                    select(CandidateAvailabilityRequestRecord).where(
                        CandidateAvailabilityRequestRecord.organization_id
                        == context.organization_id,
                        CandidateAvailabilityRequestRecord.idempotency_key
                        == payload.idempotency_key,
                    )
                ).scalar_one_or_none()
                if prior is not None:
                    if prior.request_hash != digest:
                        raise HTTPException(
                            status_code=409,
                            detail="Idempotency key was used for a different request",
                        )
                    return cls._serialize(session, prior)
                record = CandidateAvailabilityRequestRecord(
                    id=str(uuid4()),
                    organization_id=context.organization_id,
                    campaign_id=campaign_id,
                    application_id=application.id,
                    candidate_id=candidate.id,
                    idempotency_key=payload.idempotency_key,
                    request_hash=digest,
                    response_deadline=deadline,
                    interview_duration_minutes=payload.interview_duration_minutes,
                    timezone_name=payload.timezone_name,
                    proposed_windows_json=json.dumps(windows, separators=(",", ":")),
                    status="pending_response",
                    created_by_user_id=context.actor_user_id,
                    created_at=instant,
                    updated_at=instant,
                )
                session.add(record)
                session.flush()
                cls._event(
                    session, record, context.actor_user_id, "request_created", "created", instant
                )
                cls._audit(session, record, context, "candidate_availability_request_created")
                session.flush()
                return cls._serialize(session, record)
        except IntegrityError as exc:
            with SessionLocal() as session:
                prior = session.execute(
                    select(CandidateAvailabilityRequestRecord).where(
                        CandidateAvailabilityRequestRecord.organization_id
                        == context.organization_id,
                        CandidateAvailabilityRequestRecord.idempotency_key
                        == payload.idempotency_key,
                    )
                ).scalar_one_or_none()
                if prior is not None and prior.request_hash == digest:
                    return cls._serialize(session, prior)
            raise HTTPException(
                status_code=409,
                detail="An availability request already exists for this application",
            ) from exc

    @classmethod
    def get_request(
        cls, request_id: str, context: TenantContext, *, candidate_only: bool = False
    ) -> AvailabilityRequestResponse:
        with SessionLocal() as session:
            record = cls._load_request(session, request_id, context.organization_id)
            if candidate_only:
                cls._assert_candidate_owner(session, record, context)
            else:
                cls._assert_recruiter_campaign(session, record, context)
            return cls._serialize(session, record)

    @classmethod
    def list_for_campaign(
        cls, campaign_id: str, context: TenantContext
    ) -> list[AvailabilityRequestResponse]:
        with SessionLocal() as session:
            campaign = cls._load_campaign(session, campaign_id, context.organization_id)
            RecruitmentCampaignService._assert_authorized(campaign, context)
            rows = session.execute(
                select(CandidateAvailabilityRequestRecord)
                .where(
                    CandidateAvailabilityRequestRecord.organization_id == context.organization_id,
                    CandidateAvailabilityRequestRecord.campaign_id == campaign_id,
                )
                .order_by(CandidateAvailabilityRequestRecord.created_at.desc())
            ).scalars()
            return [cls._serialize(session, record) for record in rows]

    @classmethod
    def submit_response(
        cls,
        request_id: str,
        context: TenantContext,
        payload: AvailabilityResponseSubmit,
        *,
        now: datetime | None = None,
    ) -> AvailabilityRequestResponse:
        try:
            return cls._submit_response(request_id, context, payload, now=now)
        except IntegrityError as exc:
            with SessionLocal() as session:
                record = cls._load_request(session, request_id, context.organization_id)
                prior_submission = session.execute(
                    select(CandidateAvailabilitySubmissionRecord).where(
                        CandidateAvailabilitySubmissionRecord.organization_id
                        == context.organization_id,
                        CandidateAvailabilitySubmissionRecord.request_id == request_id,
                        CandidateAvailabilitySubmissionRecord.idempotency_key
                        == payload.idempotency_key,
                    )
                ).scalar_one_or_none()
                if prior_submission is not None:
                    instant = cls._now(now)
                    windows = cls._normalize_windows(
                        payload.windows,
                        payload.timezone_name,
                        record.interview_duration_minutes,
                        now=instant,
                    )
                    digest = cls._digest({"timezone": payload.timezone_name, "windows": windows})
                    if prior_submission.response_hash == digest:
                        return cls._serialize(session, record)
            raise HTTPException(
                status_code=409,
                detail="Availability response changed concurrently",
            ) from exc

    @classmethod
    def _submit_response(
        cls,
        request_id: str,
        context: TenantContext,
        payload: AvailabilityResponseSubmit,
        *,
        now: datetime | None = None,
    ) -> AvailabilityRequestResponse:
        instant = cls._now(now)
        if context.role != "candidate":
            raise HTTPException(
                status_code=403, detail="Only the application owner may submit availability"
            )
        with SessionLocal.begin() as session:
            claimed = session.execute(
                update(CandidateAvailabilityRequestRecord)
                .execution_options(synchronize_session=False)
                .where(
                    CandidateAvailabilityRequestRecord.id == request_id,
                    CandidateAvailabilityRequestRecord.organization_id == context.organization_id,
                    CandidateAvailabilityRequestRecord.status.in_(
                        ("pending_response", "responded")
                    ),
                    CandidateAvailabilityRequestRecord.response_deadline > instant,
                )
                .values(updated_at=instant)
            )
            record = cls._load_request(session, request_id, context.organization_id)
            cls._assert_candidate_owner(session, record, context)
            cls._assert_request_open(record, instant)
            if claimed.rowcount != 1:
                raise HTTPException(
                    status_code=409, detail="Availability request is no longer open"
                )
            application, campaign, _ = cls._load_target(
                session, record.campaign_id, record.application_id, context
            )
            if campaign.approval_state != "approved" or application.status != "needs_review":
                raise HTTPException(
                    status_code=409, detail="Application or campaign is no longer eligible"
                )
            windows = cls._normalize_windows(
                payload.windows,
                payload.timezone_name,
                record.interview_duration_minutes,
                now=instant,
            )
            digest = cls._digest({"timezone": payload.timezone_name, "windows": windows})
            prior_submission = session.execute(
                select(CandidateAvailabilitySubmissionRecord).where(
                    CandidateAvailabilitySubmissionRecord.organization_id
                    == context.organization_id,
                    CandidateAvailabilitySubmissionRecord.request_id == request_id,
                    CandidateAvailabilitySubmissionRecord.idempotency_key
                    == payload.idempotency_key,
                )
            ).scalar_one_or_none()
            if prior_submission is not None:
                if prior_submission.response_hash != digest:
                    raise HTTPException(
                        status_code=409, detail="Idempotency key was used for a different response"
                    )
                return cls._serialize(session, record)
            existing = session.execute(
                select(CandidateAvailabilityResponseRecord).where(
                    CandidateAvailabilityResponseRecord.organization_id == context.organization_id,
                    CandidateAvailabilityResponseRecord.request_id == request_id,
                )
            ).scalar_one_or_none()
            if existing is not None and existing.idempotency_key == payload.idempotency_key:
                if existing.response_hash != digest:
                    raise HTTPException(
                        status_code=409, detail="Idempotency key was used for a different response"
                    )
                return cls._serialize(session, record)
            return cls._persist_response(
                session,
                record,
                context,
                payload,
                windows,
                digest,
                instant,
                existing,
            )

    @classmethod
    def _persist_response(
        cls,
        session,
        record: CandidateAvailabilityRequestRecord,
        context: TenantContext,
        payload: AvailabilityResponseSubmit,
        windows: list[dict[str, str]],
        digest: str,
        instant: datetime,
        existing: CandidateAvailabilityResponseRecord | None,
    ) -> AvailabilityRequestResponse:
        if record.status not in {"pending_response", "responded"}:
            raise HTTPException(
                status_code=409, detail="Availability request cannot accept responses"
            )
        if existing is None:
            existing = CandidateAvailabilityResponseRecord(
                id=str(uuid4()),
                organization_id=context.organization_id,
                request_id=record.id,
                application_id=record.application_id,
                candidate_id=record.candidate_id,
                idempotency_key=payload.idempotency_key,
                response_hash=digest,
                timezone_name=payload.timezone_name,
                windows_json=json.dumps(windows, separators=(",", ":")),
                version=1,
                review_status="pending",
                created_at=instant,
                updated_at=instant,
            )
            session.add(existing)
        else:
            cls._update_response(session, existing, payload, windows, digest, instant)
        changed_request = session.execute(
            update(CandidateAvailabilityRequestRecord)
            .execution_options(synchronize_session=False)
            .where(
                CandidateAvailabilityRequestRecord.id == record.id,
                CandidateAvailabilityRequestRecord.organization_id == context.organization_id,
                CandidateAvailabilityRequestRecord.status.in_(("pending_response", "responded")),
                CandidateAvailabilityRequestRecord.response_deadline > instant,
            )
            .values(status="responded", updated_at=instant)
        )
        if changed_request.rowcount != 1:
            raise HTTPException(status_code=409, detail="Availability request is no longer open")
        record.status = "responded"
        session.add(
            CandidateAvailabilitySubmissionRecord(
                id=str(uuid4()),
                organization_id=context.organization_id,
                request_id=record.id,
                idempotency_key=payload.idempotency_key,
                response_hash=digest,
                submitted_at=instant,
            )
        )
        cls._event(
            session,
            record,
            context.actor_user_id,
            "candidate_response_submitted",
            "accepted",
            instant,
        )
        cls._audit(session, record, context, "candidate_availability_response_submitted")
        session.flush()
        return cls._serialize(session, record)

    @staticmethod
    def _update_response(
        session,
        existing: CandidateAvailabilityResponseRecord,
        payload: AvailabilityResponseSubmit,
        windows: list[dict[str, str]],
        digest: str,
        instant: datetime,
    ) -> None:
        if existing.review_status == "reviewed":
            raise HTTPException(status_code=409, detail="Reviewed availability cannot be changed")
        changed = session.execute(
            update(CandidateAvailabilityResponseRecord)
            .where(
                CandidateAvailabilityResponseRecord.id == existing.id,
                CandidateAvailabilityResponseRecord.version == existing.version,
                CandidateAvailabilityResponseRecord.review_status == "pending",
            )
            .values(
                idempotency_key=payload.idempotency_key,
                response_hash=digest,
                timezone_name=payload.timezone_name,
                windows_json=json.dumps(windows, separators=(",", ":")),
                version=existing.version + 1,
                updated_at=instant,
            )
        )
        if changed.rowcount != 1:
            raise HTTPException(
                status_code=409, detail="Availability response changed concurrently"
            )
        session.expire(existing)

    @classmethod
    def review_response(
        cls,
        request_id: str,
        context: TenantContext,
        payload: AvailabilityReview,
        *,
        now: datetime | None = None,
    ) -> AvailabilityRequestResponse:
        cls._require_recruiter(context)
        instant = cls._now(now)
        with SessionLocal.begin() as session:
            record = cls._load_request(session, request_id, context.organization_id)
            cls._assert_recruiter_campaign(session, record, context)
            cls._assert_request_open(record, instant)
            application, campaign, _ = cls._load_target(
                session, record.campaign_id, record.application_id, context
            )
            if campaign.approval_state != "approved" or application.status != "needs_review":
                raise HTTPException(
                    status_code=409, detail="Application or campaign is no longer eligible"
                )
            response = session.execute(
                select(CandidateAvailabilityResponseRecord).where(
                    CandidateAvailabilityResponseRecord.organization_id == context.organization_id,
                    CandidateAvailabilityResponseRecord.request_id == request_id,
                )
            ).scalar_one_or_none()
            if (
                record.status != "responded"
                or response is None
                or response.review_status != "pending"
            ):
                raise HTTPException(
                    status_code=409, detail="Availability response is not awaiting review"
                )
            windows = json.loads(response.windows_json)
            if any(
                index < 0 or index >= len(windows) for index in payload.preferred_window_indices
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Preferred window index is outside the submitted response",
                )
            response.review_status = "reviewed"
            response.reviewed_window_indices_json = json.dumps(
                payload.preferred_window_indices, separators=(",", ":")
            )
            response.reviewed_by_user_id = context.actor_user_id
            response.reviewed_at = instant
            response.updated_at = instant
            record.status = "reviewed"
            record.reviewed_by_user_id = context.actor_user_id
            record.reviewed_at = instant
            record.updated_at = instant
            cls._event(
                session,
                record,
                context.actor_user_id,
                "candidate_response_reviewed",
                "preference_marked",
                instant,
                {"preferred_window_indices": payload.preferred_window_indices},
            )
            cls._audit(session, record, context, "candidate_availability_response_reviewed")
            session.flush()
            return cls._serialize(session, record)

    @classmethod
    def cancel(
        cls, request_id: str, context: TenantContext, *, now: datetime | None = None
    ) -> AvailabilityRequestResponse:
        cls._require_recruiter(context)
        instant = cls._now(now)
        with SessionLocal.begin() as session:
            record = cls._load_request(session, request_id, context.organization_id)
            cls._assert_recruiter_campaign(session, record, context)
            if record.status not in {"pending_response", "responded", "reviewed"}:
                raise HTTPException(
                    status_code=409, detail="Availability request cannot be cancelled"
                )
            record.status = "cancelled"
            record.updated_at = instant
            cls._event(
                session, record, context.actor_user_id, "request_cancelled", "cancelled", instant
            )
            cls._audit(session, record, context, "candidate_availability_request_cancelled")
            return cls._serialize(session, record)

    @classmethod
    def expire_due(cls, *, limit: int = 100, now: datetime | None = None) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("Expiration batch limit must be between 1 and 500")
        instant = cls._now(now)
        with SessionLocal.begin() as session:
            due = list(
                session.execute(
                    select(CandidateAvailabilityRequestRecord)
                    .join(
                        RecruitmentApplicationRecord,
                        and_(
                            RecruitmentApplicationRecord.id
                            == CandidateAvailabilityRequestRecord.application_id,
                            RecruitmentApplicationRecord.organization_id
                            == CandidateAvailabilityRequestRecord.organization_id,
                        ),
                    )
                    .join(
                        RecruitmentCampaignRecord,
                        and_(
                            RecruitmentCampaignRecord.id
                            == CandidateAvailabilityRequestRecord.campaign_id,
                            RecruitmentCampaignRecord.organization_id
                            == CandidateAvailabilityRequestRecord.organization_id,
                        ),
                    )
                    .where(
                        or_(
                            and_(
                                CandidateAvailabilityRequestRecord.status.in_(
                                    ("pending_response", "responded")
                                ),
                                CandidateAvailabilityRequestRecord.response_deadline <= instant,
                            ),
                            and_(
                                CandidateAvailabilityRequestRecord.status.in_(
                                    ("pending_response", "responded", "reviewed")
                                ),
                                or_(
                                    RecruitmentApplicationRecord.status != "needs_review",
                                    RecruitmentCampaignRecord.approval_state != "approved",
                                ),
                            ),
                        ),
                    )
                    .order_by(CandidateAvailabilityRequestRecord.response_deadline)
                    .limit(limit)
                ).scalars()
            )
            transitioned = 0
            for record in due:
                deadline = record.response_deadline
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=UTC)
                application = session.execute(
                    select(RecruitmentApplicationRecord).where(
                        RecruitmentApplicationRecord.id == record.application_id,
                        RecruitmentApplicationRecord.organization_id == record.organization_id,
                    )
                ).scalar_one_or_none()
                campaign = session.execute(
                    select(RecruitmentCampaignRecord).where(
                        RecruitmentCampaignRecord.id == record.campaign_id,
                        RecruitmentCampaignRecord.organization_id == record.organization_id,
                    )
                ).scalar_one_or_none()
                expired = record.status in {"pending_response", "responded"} and deadline <= instant
                ineligible = (
                    application is None
                    or campaign is None
                    or application.status != "needs_review"
                    or campaign.approval_state != "approved"
                )
                if not expired and not ineligible:
                    continue
                if record.status == "reviewed" and not ineligible:
                    continue
                target = "expired" if expired else "cancelled"
                event_type = "request_expired" if expired else "request_cancelled_ineligible"
                conditions = [
                    CandidateAvailabilityRequestRecord.id == record.id,
                    CandidateAvailabilityRequestRecord.organization_id == record.organization_id,
                    CandidateAvailabilityRequestRecord.status.in_(
                        ("pending_response", "responded", "reviewed")
                    ),
                ]
                if expired:
                    conditions.append(
                        CandidateAvailabilityRequestRecord.response_deadline <= instant
                    )
                elif record.status != "reviewed":
                    conditions.append(
                        CandidateAvailabilityRequestRecord.response_deadline > instant
                    )
                changed = session.execute(
                    update(CandidateAvailabilityRequestRecord)
                    .execution_options(synchronize_session=False)
                    .where(*conditions)
                    .values(status=target, updated_at=instant)
                )
                if changed.rowcount == 1:
                    record.status = target
                    cls._event(session, record, None, event_type, target, instant)
                    transitioned += 1
            return transitioned

    @classmethod
    def _serialize(
        cls, session, record: CandidateAvailabilityRequestRecord
    ) -> AvailabilityRequestResponse:
        response = session.execute(
            select(CandidateAvailabilityResponseRecord).where(
                CandidateAvailabilityResponseRecord.organization_id == record.organization_id,
                CandidateAvailabilityResponseRecord.request_id == record.id,
            )
        ).scalar_one_or_none()
        response_data = None
        if response is not None:
            response_data = {
                "timezone_name": response.timezone_name,
                "windows": json.loads(response.windows_json),
                "version": response.version,
                "review_status": response.review_status,
                "preferred_window_indices": json.loads(response.reviewed_window_indices_json),
                "reviewed_by_user_id": response.reviewed_by_user_id,
                "reviewed_at": response.reviewed_at,
            }
        return AvailabilityRequestResponse(
            id=record.id,
            campaign_id=record.campaign_id,
            application_id=record.application_id,
            candidate_id=record.candidate_id,
            response_deadline=record.response_deadline,
            interview_duration_minutes=record.interview_duration_minutes,
            timezone_name=record.timezone_name,
            proposed_windows=json.loads(record.proposed_windows_json),
            status=record.status,
            created_by_user_id=record.created_by_user_id,
            reviewed_by_user_id=record.reviewed_by_user_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            response=response_data,
        )

    @staticmethod
    def _require_recruiter(context: TenantContext) -> None:
        if context.role not in {"recruiter", "org_admin", "platform_admin"}:
            raise HTTPException(status_code=403, detail="Recruiter access required")

    @classmethod
    def _load_campaign(cls, session, campaign_id: str, organization_id: str):
        campaign = session.execute(
            select(RecruitmentCampaignRecord).where(
                RecruitmentCampaignRecord.id == campaign_id,
                RecruitmentCampaignRecord.organization_id == organization_id,
            )
        ).scalar_one_or_none()
        if campaign is None:
            raise HTTPException(status_code=404, detail="Campaign not found")
        return campaign

    @classmethod
    def _load_target(cls, session, campaign_id: str, application_id: str, context: TenantContext):
        application = session.execute(
            select(RecruitmentApplicationRecord).where(
                RecruitmentApplicationRecord.id == application_id,
                RecruitmentApplicationRecord.campaign_id == campaign_id,
                RecruitmentApplicationRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        campaign = cls._load_campaign(session, campaign_id, context.organization_id)
        if application is None:
            raise HTTPException(status_code=404, detail="Application not found")
        candidate = session.execute(
            select(CandidateRecord).where(
                CandidateRecord.id == application.candidate_id,
                CandidateRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        if candidate is None:
            raise HTTPException(status_code=404, detail="Application not found")
        return application, campaign, candidate

    @classmethod
    def _load_request(cls, session, request_id: str, organization_id: str):
        record = session.execute(
            select(CandidateAvailabilityRequestRecord).where(
                CandidateAvailabilityRequestRecord.id == request_id,
                CandidateAvailabilityRequestRecord.organization_id == organization_id,
            )
        ).scalar_one_or_none()
        if record is None:
            raise HTTPException(status_code=404, detail="Availability request not found")
        return record

    @staticmethod
    def _assert_eligible(
        application: RecruitmentApplicationRecord, campaign: RecruitmentCampaignRecord
    ) -> None:
        if campaign.approval_state != "approved" or application.status != "needs_review":
            raise HTTPException(
                status_code=409,
                detail="Application or campaign is not eligible for availability collection",
            )

    @staticmethod
    def _assert_request_open(record: CandidateAvailabilityRequestRecord, now: datetime) -> None:
        deadline = record.response_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if record.status not in {"pending_response", "responded"} or deadline <= now:
            raise HTTPException(status_code=409, detail="Availability request is no longer open")

    @staticmethod
    def _assert_candidate_owner(
        session, record: CandidateAvailabilityRequestRecord, context: TenantContext
    ) -> None:
        candidate = session.execute(
            select(CandidateRecord).where(
                CandidateRecord.id == record.candidate_id,
                CandidateRecord.organization_id == context.organization_id,
                CandidateRecord.user_id == context.actor_user_id,
            )
        ).scalar_one_or_none()
        if candidate is None:
            raise HTTPException(status_code=404, detail="Availability request not found")

    @classmethod
    def _assert_recruiter_campaign(
        cls, session, record: CandidateAvailabilityRequestRecord, context: TenantContext
    ) -> None:
        cls._require_recruiter(context)
        campaign = cls._load_campaign(session, record.campaign_id, context.organization_id)
        RecruitmentCampaignService._assert_authorized(campaign, context)

    @staticmethod
    def _event(
        session,
        record: CandidateAvailabilityRequestRecord,
        actor_user_id: str | None,
        event_type: str,
        outcome: str,
        now: datetime,
        details: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            CandidateAvailabilityEventRecord(
                id=str(uuid4()),
                organization_id=record.organization_id,
                request_id=record.id,
                actor_user_id=actor_user_id,
                event_type=event_type,
                outcome=outcome,
                details_json=json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
                created_at=now,
            )
        )

    @staticmethod
    def _audit(
        session,
        record: CandidateAvailabilityRequestRecord,
        context: TenantContext,
        action: str,
    ) -> None:
        session.add(
            AccessAuditRecord(
                actor_user_id=context.actor_user_id,
                organization_id=context.organization_id,
                requested_organization_id=context.organization_id,
                action=action,
                resource_type="candidate_availability_request",
                resource_id=record.id,
                outcome="allowed",
                support_reason=context.support_reason,
            )
        )
