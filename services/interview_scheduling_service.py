"""Tenant-scoped internal interview scheduling and reservation workflows."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from services.candidate_availability_service import (
    AvailabilityWindow,
    CandidateAvailabilityService,
)
from services.database import (
    AccessAuditRecord,
    CandidateAvailabilityRequestRecord,
    CandidateAvailabilityResponseRecord,
    CandidateRecord,
    InterviewScheduleEventRecord,
    InterviewScheduleMutexRecord,
    InterviewScheduleRecord,
    InterviewScheduleReservationRecord,
    OrganizationMembershipRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignRecord,
    SessionLocal,
)
from services.recruitment_campaign_service import RecruitmentCampaignService
from services.tenant_authorization import TenantContext

MAX_PENDING_LIFETIME = timedelta(days=7)
MAX_EXCLUSIVE_RESOURCES = 10
RESOURCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MUTEX_ID = "global"


class InterviewScheduleProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    availability_request_id: str = Field(min_length=1, max_length=64)
    window_index: int = Field(ge=0, le=9)
    interviewer_user_id: str = Field(min_length=1, max_length=64)
    exclusive_resources: list[str] = Field(default_factory=list, max_length=MAX_EXCLUSIVE_RESOURCES)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class InterviewScheduleReschedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    availability_request_id: str = Field(min_length=1, max_length=64)
    window_index: int = Field(ge=0, le=9)
    interviewer_user_id: str = Field(min_length=1, max_length=64)
    exclusive_resources: list[str] = Field(default_factory=list, max_length=MAX_EXCLUSIVE_RESOURCES)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class InterviewScheduleTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class InterviewScheduleCancellation(InterviewScheduleTransition):
    reason: str = Field(min_length=1, max_length=256)


class InterviewScheduleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    campaign_id: str
    application_id: str
    candidate_id: str
    availability_request_id: str
    status: str
    start_at: datetime
    end_at: datetime
    timezone_name: str
    duration_minutes: int
    interviewer_user_id: str
    exclusive_resources: list[str]
    rescheduled_from_id: str | None
    reservation_token: str | None
    version: int
    expires_at: datetime
    cancellation_reason: str | None
    created_at: datetime
    updated_at: datetime


class InterviewScheduleEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str
    outcome: str
    details: dict[str, object]
    created_at: datetime


class InterviewScheduleDetailResponse(InterviewScheduleResponse):
    events: list[InterviewScheduleEventResponse]


class CompatibleSlotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    availability_request_id: str
    application_id: str
    interview_duration_minutes: int
    timezone_name: str
    windows: list[dict[str, object]]


class InterviewSchedulingService:
    @staticmethod
    def _now(now: datetime | None = None) -> datetime:
        value = now or datetime.now(UTC)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Internal clock must return an aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _lock_reservations(session) -> None:
        changed = session.execute(
            update(InterviewScheduleMutexRecord)
            .where(InterviewScheduleMutexRecord.id == MUTEX_ID)
            .values(generation=InterviewScheduleMutexRecord.generation + 1)
        )
        if changed.rowcount != 1:
            raise RuntimeError("Interview scheduling mutex is missing; apply database migrations")

    @staticmethod
    def _require_recruiter(context: TenantContext) -> None:
        if context.role not in {"recruiter", "org_admin"} and not context.is_platform_admin:
            raise HTTPException(status_code=403, detail="Interview scheduling access denied")

    @staticmethod
    def _load_application(session, campaign_id: str, application_id: str, context: TenantContext):
        application = session.execute(
            select(RecruitmentApplicationRecord).where(
                RecruitmentApplicationRecord.id == application_id,
                RecruitmentApplicationRecord.campaign_id == campaign_id,
                RecruitmentApplicationRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        campaign = session.execute(
            select(RecruitmentCampaignRecord).where(
                RecruitmentCampaignRecord.id == campaign_id,
                RecruitmentCampaignRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        if application is None or campaign is None:
            raise HTTPException(status_code=404, detail="Application not found")
        RecruitmentCampaignService._assert_authorized(campaign, context)
        return application, campaign

    @staticmethod
    def _ensure_eligible(application, campaign) -> None:
        if application.status != "needs_review" or campaign.approval_state != "approved":
            raise HTTPException(
                status_code=409,
                detail="Application and approved campaign must remain eligible for scheduling",
            )

    @staticmethod
    def _require_active_interviewer(session, organization_id: str, user_id: str) -> None:
        membership = session.execute(
            select(OrganizationMembershipRecord).where(
                OrganizationMembershipRecord.organization_id == organization_id,
                OrganizationMembershipRecord.user_id == user_id,
                OrganizationMembershipRecord.status == "active",
                OrganizationMembershipRecord.role.in_(("org_admin", "recruiter")),
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(status_code=422, detail="Interviewer must be an active recruiter")

    @classmethod
    def _availability_slot(
        cls,
        session,
        *,
        organization_id: str,
        application_id: str,
        candidate_id: str,
        request_id: str,
        window_index: int,
        now: datetime,
    ) -> tuple[CandidateAvailabilityRequestRecord, CandidateAvailabilityResponseRecord, datetime, datetime]:
        request = session.execute(
            select(CandidateAvailabilityRequestRecord).where(
                CandidateAvailabilityRequestRecord.organization_id == organization_id,
                CandidateAvailabilityRequestRecord.id == request_id,
                CandidateAvailabilityRequestRecord.application_id == application_id,
                CandidateAvailabilityRequestRecord.candidate_id == candidate_id,
            )
        ).scalar_one_or_none()
        response = session.execute(
            select(CandidateAvailabilityResponseRecord).where(
                CandidateAvailabilityResponseRecord.organization_id == organization_id,
                CandidateAvailabilityResponseRecord.request_id == request_id,
                CandidateAvailabilityResponseRecord.application_id == application_id,
                CandidateAvailabilityResponseRecord.candidate_id == candidate_id,
            )
        ).scalar_one_or_none()
        if (
            request is None
            or response is None
            or request.status != "reviewed"
            or response.review_status != "reviewed"
        ):
            raise HTTPException(status_code=409, detail="A reviewed candidate response is required")

        windows = json.loads(response.windows_json)
        preferred = json.loads(response.reviewed_window_indices_json)
        if window_index not in preferred or window_index >= len(windows):
            raise HTTPException(status_code=422, detail="The selected availability window was not approved")
        try:
            zone = CandidateAvailabilityService._zone(response.timezone_name)
            def persisted_local(value: str) -> datetime:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("Persisted availability instants must be offset-aware")
                return parsed.astimezone(UTC).astimezone(zone)

            local_windows = []
            for item in windows:
                local_windows.append(
                    AvailabilityWindow(
                        start=persisted_local(item["start"]),
                        end=persisted_local(item["end"]),
                    )
                )
            normalized = CandidateAvailabilityService._normalize_windows(
                local_windows,
                response.timezone_name,
                request.interview_duration_minutes,
                now=now,
            )
            start = datetime.fromisoformat(normalized[window_index]["start"]).astimezone(UTC)
            end = datetime.fromisoformat(normalized[window_index]["end"]).astimezone(UTC)
        except HTTPException as exc:
            raise HTTPException(status_code=409, detail="Stored availability response is invalid") from exc
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise HTTPException(status_code=409, detail="Stored availability response is invalid") from exc
        return request, response, start, end

    @classmethod
    def compatible_slots(
        cls, request_id: str, context: TenantContext, *, now: datetime | None = None
    ) -> CompatibleSlotResponse:
        instant = cls._now(now)
        with SessionLocal() as session:
            request = session.execute(
                select(CandidateAvailabilityRequestRecord).where(
                    CandidateAvailabilityRequestRecord.organization_id == context.organization_id,
                    CandidateAvailabilityRequestRecord.id == request_id,
                )
            ).scalar_one_or_none()
            if request is None:
                raise HTTPException(status_code=404, detail="Availability request not found")
            campaign = session.execute(
                select(RecruitmentCampaignRecord).where(
                    RecruitmentCampaignRecord.organization_id == context.organization_id,
                    RecruitmentCampaignRecord.id == request.campaign_id,
                )
            ).scalar_one_or_none()
            if context.role == "candidate":
                candidate = session.execute(
                    select(CandidateRecord).where(
                        CandidateRecord.organization_id == context.organization_id,
                        CandidateRecord.id == request.candidate_id,
                        CandidateRecord.user_id == context.actor_user_id,
                    )
                ).scalar_one_or_none()
                if candidate is None:
                    raise HTTPException(status_code=404, detail="Availability request not found")
            elif campaign is not None:
                RecruitmentCampaignService._assert_authorized(campaign, context)
            else:
                raise HTTPException(status_code=404, detail="Availability request not found")
            response = session.execute(
                select(CandidateAvailabilityResponseRecord).where(
                    CandidateAvailabilityResponseRecord.organization_id == request.organization_id,
                    CandidateAvailabilityResponseRecord.request_id == request.id,
                )
            ).scalar_one_or_none()
            if (
                request.status != "reviewed"
                or response is None
                or response.review_status != "reviewed"
            ):
                raise HTTPException(status_code=409, detail="A reviewed candidate response is required")
            indices = json.loads(response.reviewed_window_indices_json)
            selected: list[dict[str, object]] = []
            for index in indices:
                _, _, start, end = cls._availability_slot(
                    session,
                    organization_id=request.organization_id,
                    application_id=request.application_id,
                    candidate_id=request.candidate_id,
                    request_id=request.id,
                    window_index=index,
                    now=instant,
                )
                selected.append(
                    {
                        "window_index": index,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    }
                )
            return CompatibleSlotResponse(
                availability_request_id=request.id,
                application_id=request.application_id,
                interview_duration_minutes=request.interview_duration_minutes,
                timezone_name=response.timezone_name,
                windows=selected,
            )

    @staticmethod
    def _clean_resources(resources: list[str]) -> list[str]:
        if len(set(resources)) != len(resources) or any(
            RESOURCE_KEY_PATTERN.fullmatch(value) is None for value in resources
        ):
            raise HTTPException(
                status_code=422,
                detail="Exclusive resource identifiers must be unique and use 1-128 safe characters",
            )
        return sorted(resources)

    @staticmethod
    def _digest(payload: dict[str, object]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _event(session, schedule, actor_id, event_type, outcome, now, details=None) -> None:
        session.add(
            InterviewScheduleEventRecord(
                id=str(uuid4()),
                organization_id=schedule.organization_id,
                schedule_id=schedule.id,
                actor_user_id=actor_id,
                event_type=event_type,
                outcome=outcome,
                details_json=json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
                created_at=now,
            )
        )

    @staticmethod
    def _audit(session, schedule, context, action: str, outcome: str = "allowed") -> None:
        session.add(
            AccessAuditRecord(
                actor_user_id=None if context.is_automated else context.actor_user_id,
                organization_id=context.organization_id,
                requested_organization_id=context.organization_id,
                action=action,
                resource_type="interview_schedule",
                resource_id=schedule.id,
                outcome=outcome,
                support_reason=context.support_reason,
            )
        )

    @classmethod
    def _serialize(cls, schedule) -> InterviewScheduleResponse:
        return InterviewScheduleResponse(
            id=schedule.id,
            campaign_id=schedule.campaign_id,
            application_id=schedule.application_id,
            candidate_id=schedule.candidate_id,
            availability_request_id=schedule.availability_request_id,
            status=schedule.status,
            start_at=cls._utc(schedule.start_at),
            end_at=cls._utc(schedule.end_at),
            timezone_name=schedule.timezone_name,
            duration_minutes=schedule.duration_minutes,
            interviewer_user_id=schedule.interviewer_user_id,
            exclusive_resources=json.loads(schedule.exclusive_resources_json),
            rescheduled_from_id=schedule.rescheduled_from_id,
            reservation_token=schedule.reservation_token,
            version=schedule.version,
            expires_at=cls._utc(schedule.expires_at),
            cancellation_reason=schedule.cancellation_reason,
            created_at=cls._utc(schedule.created_at),
            updated_at=cls._utc(schedule.updated_at),
        )

    @classmethod
    def _load_visible_schedule(cls, session, schedule_id: str, context: TenantContext):
        schedule = session.execute(
            select(InterviewScheduleRecord).where(
                InterviewScheduleRecord.id == schedule_id,
                InterviewScheduleRecord.organization_id == context.organization_id,
            )
        ).scalar_one_or_none()
        if schedule is None:
            raise HTTPException(status_code=404, detail="Interview schedule not found")
        if context.role == "candidate":
            candidate = session.execute(
                select(CandidateRecord).where(
                    CandidateRecord.organization_id == context.organization_id,
                    CandidateRecord.id == schedule.candidate_id,
                    CandidateRecord.user_id == context.actor_user_id,
                )
            ).scalar_one_or_none()
            if candidate is None:
                raise HTTPException(status_code=404, detail="Interview schedule not found")
        else:
            campaign = session.execute(
                select(RecruitmentCampaignRecord).where(
                    RecruitmentCampaignRecord.organization_id == context.organization_id,
                    RecruitmentCampaignRecord.id == schedule.campaign_id,
                )
            ).scalar_one_or_none()
            if campaign is None:
                raise HTTPException(status_code=404, detail="Interview schedule not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
        return schedule

    @classmethod
    def propose(
        cls,
        campaign_id: str,
        application_id: str,
        context: TenantContext,
        payload: InterviewScheduleProposal,
        *,
        now: datetime | None = None,
    ) -> InterviewScheduleResponse:
        cls._require_recruiter(context)
        instant = cls._now(now)
        resources = cls._clean_resources(payload.exclusive_resources)
        request_data: dict[str, object] = {
            "campaign_id": campaign_id,
            "application_id": application_id,
            "availability_request_id": payload.availability_request_id,
            "window_index": payload.window_index,
            "interviewer_user_id": payload.interviewer_user_id,
            "exclusive_resources": resources,
        }
        digest = cls._digest(request_data)
        try:
            with SessionLocal.begin() as session:
                cls._lock_reservations(session)
                existing = session.execute(
                    select(InterviewScheduleRecord).where(
                        InterviewScheduleRecord.organization_id == context.organization_id,
                        InterviewScheduleRecord.idempotency_key == payload.idempotency_key,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.request_hash != digest:
                        raise HTTPException(status_code=409, detail="Idempotency key was already used")
                    cls._load_visible_schedule(session, existing.id, context)
                    return cls._serialize(existing)
                application, campaign = cls._load_application(
                    session, campaign_id, application_id, context
                )
                cls._ensure_eligible(application, campaign)
                cls._require_active_interviewer(
                    session, context.organization_id, payload.interviewer_user_id
                )
                availability, response, start, end = cls._availability_slot(
                    session,
                    organization_id=context.organization_id,
                    application_id=application_id,
                    candidate_id=application.candidate_id,
                    request_id=payload.availability_request_id,
                    window_index=payload.window_index,
                    now=instant,
                )
                schedule = InterviewScheduleRecord(
                    id=str(uuid4()),
                    organization_id=context.organization_id,
                    campaign_id=campaign_id,
                    application_id=application_id,
                    candidate_id=application.candidate_id,
                    availability_request_id=availability.id,
                    availability_response_version=response.version,
                    availability_window_index=payload.window_index,
                    idempotency_key=payload.idempotency_key,
                    request_hash=digest,
                    status="proposed",
                    start_at=start,
                    end_at=end,
                    timezone_name=response.timezone_name,
                    duration_minutes=availability.interview_duration_minutes,
                    interviewer_user_id=payload.interviewer_user_id,
                    exclusive_resources_json=json.dumps(resources, separators=(",", ":")),
                    rescheduled_from_id=None,
                    reservation_token=None,
                    version=1,
                    expires_at=instant + MAX_PENDING_LIFETIME,
                    created_by_user_id=context.actor_user_id,
                    created_at=instant,
                    updated_at=instant,
                )
                session.add(schedule)
                session.flush()
                cls._event(
                    session,
                    schedule,
                    None if context.is_automated else context.actor_user_id,
                    "proposal_created",
                    "proposed",
                    instant,
                )
                cls._audit(session, schedule, context, "interview_schedule_proposed")
                return cls._serialize(schedule)
        except OperationalError as exc:
            raise HTTPException(
                status_code=503,
                detail="Scheduling is temporarily busy; retry the same idempotent request",
                headers={"Retry-After": "1"},
            ) from exc

    @classmethod
    def submit(
        cls,
        schedule_id: str,
        context: TenantContext,
        payload: InterviewScheduleTransition,
        *,
        now: datetime | None = None,
    ) -> InterviewScheduleResponse:
        cls._require_recruiter(context)
        instant = cls._now(now)
        with SessionLocal.begin() as session:
            cls._lock_reservations(session)
            schedule = cls._load_visible_schedule(session, schedule_id, context)
            if schedule.status == "pending_confirmation":
                return cls._serialize(schedule)
            if schedule.status != "proposed" or schedule.version != payload.expected_version:
                raise HTTPException(status_code=409, detail="Schedule proposal is stale or not submittable")
            if cls._utc(schedule.expires_at) <= instant:
                raise HTTPException(status_code=409, detail="Schedule proposal has expired")
            app, campaign = cls._load_application(
                session, schedule.campaign_id, schedule.application_id, context
            )
            cls._ensure_eligible(app, campaign)
            schedule.status = "pending_confirmation"
            schedule.version += 1
            schedule.updated_at = instant
            cls._event(
                session,
                schedule,
                None if context.is_automated else context.actor_user_id,
                "submitted_for_confirmation",
                "pending_confirmation",
                instant,
            )
            cls._audit(session, schedule, context, "interview_schedule_submitted")
            return cls._serialize(schedule)

    @classmethod
    def reschedule(
        cls,
        schedule_id: str,
        context: TenantContext,
        payload: InterviewScheduleReschedule,
        *,
        now: datetime | None = None,
    ) -> InterviewScheduleResponse:
        cls._require_recruiter(context)
        instant = cls._now(now)
        resources = cls._clean_resources(payload.exclusive_resources)
        try:
            with SessionLocal.begin() as session:
                cls._lock_reservations(session)
                original = cls._load_visible_schedule(session, schedule_id, context)
                request_data: dict[str, object] = {
                    "rescheduled_from_id": schedule_id,
                    "expected_version": payload.expected_version,
                    "availability_request_id": payload.availability_request_id,
                    "window_index": payload.window_index,
                    "interviewer_user_id": payload.interviewer_user_id,
                    "exclusive_resources": resources,
                }
                digest = cls._digest(request_data)
                existing = session.execute(
                    select(InterviewScheduleRecord).where(
                        InterviewScheduleRecord.organization_id == context.organization_id,
                        InterviewScheduleRecord.idempotency_key == payload.idempotency_key,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.request_hash != digest:
                        raise HTTPException(status_code=409, detail="Idempotency key was already used")
                    return cls._serialize(existing)
                if original.status != "confirmed_internal":
                    raise HTTPException(status_code=409, detail="Only an internal reservation can be rescheduled")
                if original.version != payload.expected_version:
                    raise HTTPException(status_code=409, detail="Schedule version is stale")
                app, campaign = cls._load_application(
                    session, original.campaign_id, original.application_id, context
                )
                cls._ensure_eligible(app, campaign)
                cls._require_active_interviewer(
                    session, context.organization_id, payload.interviewer_user_id
                )
                availability, response, start, end = cls._availability_slot(
                    session,
                    organization_id=context.organization_id,
                    application_id=original.application_id,
                    candidate_id=original.candidate_id,
                    request_id=payload.availability_request_id,
                    window_index=payload.window_index,
                    now=instant,
                )
                replacement = InterviewScheduleRecord(
                    id=str(uuid4()),
                    organization_id=original.organization_id,
                    campaign_id=original.campaign_id,
                    application_id=original.application_id,
                    candidate_id=original.candidate_id,
                    availability_request_id=availability.id,
                    availability_response_version=response.version,
                    availability_window_index=payload.window_index,
                    idempotency_key=payload.idempotency_key,
                    request_hash=digest,
                    status="reschedule_pending",
                    start_at=start,
                    end_at=end,
                    timezone_name=response.timezone_name,
                    duration_minutes=availability.interview_duration_minutes,
                    interviewer_user_id=payload.interviewer_user_id,
                    exclusive_resources_json=json.dumps(resources, separators=(",", ":")),
                    rescheduled_from_id=original.id,
                    reservation_token=None,
                    version=1,
                    expires_at=instant + MAX_PENDING_LIFETIME,
                    created_by_user_id=context.actor_user_id,
                    created_at=instant,
                    updated_at=instant,
                )
                session.add(replacement)
                session.flush()
                cls._event(
                    session,
                    replacement,
                    context.actor_user_id,
                    "reschedule_proposed",
                    "reschedule_pending",
                    instant,
                    {
                        "previous_schedule_id": original.id,
                        "previous_start_at": cls._utc(original.start_at).isoformat(),
                        "previous_end_at": cls._utc(original.end_at).isoformat(),
                    },
                )
                cls._audit(session, replacement, context, "interview_schedule_reschedule_proposed")
                return cls._serialize(replacement)
        except OperationalError as exc:
            raise HTTPException(
                status_code=503,
                detail="Scheduling is temporarily busy; retry the same idempotent request",
                headers={"Retry-After": "1"},
            ) from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A replacement is already pending") from exc

    @classmethod
    def _reservation_resources(cls, schedule) -> list[tuple[str, str]]:
        return [
            ("candidate", schedule.candidate_id),
            ("interviewer", schedule.interviewer_user_id),
            *[("exclusive", item) for item in json.loads(schedule.exclusive_resources_json)],
        ]

    @classmethod
    def _conflict(
        cls,
        session,
        schedule,
        *,
        exclude_schedule_id: str | None,
    ) -> InterviewScheduleReservationRecord | None:
        resource_filters = [
            and_(
                InterviewScheduleReservationRecord.resource_type == kind,
                InterviewScheduleReservationRecord.resource_key == key,
            )
            for kind, key in cls._reservation_resources(schedule)
        ]
        query = select(InterviewScheduleReservationRecord).where(
            InterviewScheduleReservationRecord.organization_id == schedule.organization_id,
            InterviewScheduleReservationRecord.start_at < schedule.end_at,
            InterviewScheduleReservationRecord.end_at > schedule.start_at,
            or_(*resource_filters),
        )
        if exclude_schedule_id is not None:
            query = query.where(
                InterviewScheduleReservationRecord.schedule_id != exclude_schedule_id
            )
        return session.execute(query.limit(1)).scalar_one_or_none()

    @staticmethod
    def _validate_confirmation_state(schedule, expected_version: int, now: datetime) -> None:
        if schedule.status not in {"pending_confirmation", "reschedule_pending"}:
            raise HTTPException(status_code=409, detail="Schedule is not awaiting confirmation")
        if schedule.version != expected_version:
            raise HTTPException(status_code=409, detail="Schedule version is stale")
        if InterviewSchedulingService._utc(schedule.expires_at) <= now:
            raise HTTPException(status_code=409, detail="Schedule proposal has expired")

    @classmethod
    def _validate_confirmation_authority(cls, session, schedule, context: TenantContext) -> None:
        if context.actor_user_id != schedule.interviewer_user_id:
            raise HTTPException(status_code=403, detail="Only the assigned interviewer can confirm")
        cls._require_recruiter(context)
        cls._require_active_interviewer(
            session, schedule.organization_id, schedule.interviewer_user_id
        )

    @classmethod
    def _validate_confirmation_eligibility(
        cls, session, schedule, context: TenantContext, now: datetime
    ) -> None:
        application, campaign = cls._load_application(
            session, schedule.campaign_id, schedule.application_id, context
        )
        cls._ensure_eligible(application, campaign)
        availability, response, start, end = cls._availability_slot(
            session,
            organization_id=schedule.organization_id,
            application_id=schedule.application_id,
            candidate_id=schedule.candidate_id,
            request_id=schedule.availability_request_id,
            window_index=schedule.availability_window_index,
            now=now,
        )
        if (
            response.version != schedule.availability_response_version
            or start != cls._utc(schedule.start_at)
            or end != cls._utc(schedule.end_at)
            or availability.interview_duration_minutes != schedule.duration_minutes
        ):
            raise HTTPException(
                status_code=409,
                detail="Candidate availability changed; propose a new slot",
            )

    @classmethod
    def _load_replacement_parent(cls, session, schedule, context: TenantContext):
        if schedule.rescheduled_from_id is None:
            return None
        old_schedule = cls._load_visible_schedule(
            session, schedule.rescheduled_from_id, context
        )
        if old_schedule.status != "confirmed_internal":
            raise HTTPException(
                status_code=409,
                detail="The existing reservation is no longer active",
            )
        if old_schedule.reservation_token is None:
            raise HTTPException(
                status_code=409,
                detail="The existing reservation requires manual review",
            )
        return old_schedule

    @classmethod
    def _record_conflict(
        cls, session, schedule, conflict_record, context: TenantContext, now: datetime
    ) -> None:
        cls._event(
            session,
            schedule,
            context.actor_user_id,
            "reservation_conflict",
            "rejected",
            now,
            {
                "resource_type": conflict_record.resource_type,
                "resource_key": conflict_record.resource_key,
            },
        )
        cls._audit(
            session,
            schedule,
            context,
            "interview_schedule_conflict_rejected",
            "denied",
        )

    @classmethod
    def _commit_reservation(
        cls, session, schedule, old_schedule, context: TenantContext, now: datetime
    ) -> InterviewScheduleResponse:
        token = str(uuid4())
        for resource_type, resource_key in cls._reservation_resources(schedule):
            session.add(
                InterviewScheduleReservationRecord(
                    id=str(uuid4()),
                    organization_id=schedule.organization_id,
                    schedule_id=schedule.id,
                    reservation_token=token,
                    resource_type=resource_type,
                    resource_key=resource_key,
                    start_at=schedule.start_at,
                    end_at=schedule.end_at,
                    created_at=now,
                )
            )
        if old_schedule is not None:
            old_token = old_schedule.reservation_token
            session.flush()
            session.execute(
                delete(InterviewScheduleReservationRecord).where(
                    InterviewScheduleReservationRecord.organization_id
                    == old_schedule.organization_id,
                    InterviewScheduleReservationRecord.schedule_id == old_schedule.id,
                    InterviewScheduleReservationRecord.reservation_token == old_token,
                )
            )
            old_version = old_schedule.version
            changed_old = session.execute(
                update(InterviewScheduleRecord)
                .execution_options(synchronize_session=False)
                .where(
                    InterviewScheduleRecord.id == old_schedule.id,
                    InterviewScheduleRecord.status == "confirmed_internal",
                    InterviewScheduleRecord.version == old_version,
                )
                .values(
                    status="cancelled",
                    cancellation_reason="rescheduled",
                    version=old_version + 1,
                    updated_at=now,
                )
            )
            if changed_old.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="The existing reservation changed concurrently",
                )
            old_schedule.status = "cancelled"
            old_schedule.cancellation_reason = "rescheduled"
            old_schedule.version = old_version + 1
            old_schedule.updated_at = now
            cls._event(
                session,
                old_schedule,
                context.actor_user_id,
                "reservation_replaced",
                "cancelled",
                now,
                {
                    "replacement_schedule_id": schedule.id,
                    "previous_start_at": cls._utc(old_schedule.start_at).isoformat(),
                    "previous_end_at": cls._utc(old_schedule.end_at).isoformat(),
                    "replacement_start_at": cls._utc(schedule.start_at).isoformat(),
                    "replacement_end_at": cls._utc(schedule.end_at).isoformat(),
                },
            )
            cls._audit(session, old_schedule, context, "interview_schedule_rescheduled")

        schedule_version = schedule.version
        changed_schedule = session.execute(
            update(InterviewScheduleRecord)
            .execution_options(synchronize_session=False)
            .where(
                InterviewScheduleRecord.id == schedule.id,
                InterviewScheduleRecord.status == schedule.status,
                InterviewScheduleRecord.version == schedule_version,
            )
            .values(
                status="confirmed_internal",
                reservation_token=token,
                confirmed_by_user_id=context.actor_user_id,
                version=schedule_version + 1,
                updated_at=now,
            )
        )
        if changed_schedule.rowcount != 1:
            raise HTTPException(status_code=409, detail="Schedule changed concurrently")
        schedule.status = "confirmed_internal"
        schedule.reservation_token = token
        schedule.confirmed_by_user_id = context.actor_user_id
        schedule.version = schedule_version + 1
        schedule.updated_at = now
        cls._event(
            session,
            schedule,
            context.actor_user_id,
            "reservation_confirmed_internal",
            "confirmed_internal",
            now,
            {
                "start_at": cls._utc(schedule.start_at).isoformat(),
                "end_at": cls._utc(schedule.end_at).isoformat(),
                "timezone_name": schedule.timezone_name,
                "replaces_schedule_id": (
                    None if old_schedule is None else old_schedule.id
                ),
            },
        )
        cls._audit(session, schedule, context, "interview_schedule_confirmed_internal")
        return cls._serialize(schedule)

    @classmethod
    def confirm(
        cls,
        schedule_id: str,
        context: TenantContext,
        payload: InterviewScheduleTransition,
        *,
        now: datetime | None = None,
    ) -> InterviewScheduleResponse:
        instant = cls._now(now)
        conflict = False
        result: InterviewScheduleResponse | None = None
        try:
            with SessionLocal.begin() as session:
                cls._lock_reservations(session)
                schedule = cls._load_visible_schedule(session, schedule_id, context)
                if schedule.status == "confirmed_internal":
                    result = cls._serialize(schedule)
                else:
                    cls._validate_confirmation_state(
                        schedule, payload.expected_version, instant
                    )
                    cls._validate_confirmation_authority(session, schedule, context)
                    cls._validate_confirmation_eligibility(
                        session, schedule, context, instant
                    )
                    old_schedule = cls._load_replacement_parent(session, schedule, context)
                    conflict_record = cls._conflict(
                        session,
                        schedule,
                        exclude_schedule_id=None if old_schedule is None else old_schedule.id,
                    )
                    if conflict_record is not None:
                        cls._record_conflict(
                            session, schedule, conflict_record, context, instant
                        )
                        conflict = True
                    else:
                        result = cls._commit_reservation(
                            session, schedule, old_schedule, context, instant
                        )
        except OperationalError as exc:
            raise HTTPException(
                status_code=503,
                detail="Scheduling is temporarily busy; retry the same request",
                headers={"Retry-After": "1"},
            ) from exc
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="The reservation changed concurrently; reload and retry",
            ) from exc
        if conflict:
            raise HTTPException(status_code=409, detail="The selected slot conflicts with an active reservation")
        if result is None:
            raise RuntimeError("Interview confirmation completed without a result")
        return result

    @classmethod
    def cancel(
        cls,
        schedule_id: str,
        context: TenantContext,
        payload: InterviewScheduleCancellation,
        *,
        now: datetime | None = None,
    ) -> InterviewScheduleResponse:
        instant = cls._now(now)
        with SessionLocal.begin() as session:
            cls._lock_reservations(session)
            schedule = cls._load_visible_schedule(session, schedule_id, context)
            if schedule.status == "cancelled":
                return cls._serialize(schedule)
            if schedule.version != payload.expected_version:
                raise HTTPException(status_code=409, detail="Schedule version is stale")
            if context.role == "candidate" and schedule.status not in {
                "pending_confirmation",
                "confirmed_internal",
                "reschedule_pending",
            }:
                raise HTTPException(status_code=409, detail="Schedule cannot be cancelled in this state")
            if schedule.status not in {
                "proposed",
                "pending_confirmation",
                "confirmed_internal",
                "reschedule_pending",
            }:
                raise HTTPException(status_code=409, detail="Schedule cannot be cancelled in this state")
            if schedule.status == "confirmed_internal":
                replacements = session.execute(
                    select(InterviewScheduleRecord).where(
                        InterviewScheduleRecord.organization_id == schedule.organization_id,
                        InterviewScheduleRecord.rescheduled_from_id == schedule.id,
                        InterviewScheduleRecord.status == "reschedule_pending",
                    )
                ).scalars()
                for replacement in replacements:
                    replacement.status = "cancelled"
                    replacement.cancellation_reason = "original_schedule_cancelled"
                    replacement.version += 1
                    replacement.updated_at = instant
                    cls._event(
                        session,
                        replacement,
                        context.actor_user_id,
                        "reschedule_cancelled_with_original",
                        "cancelled",
                        instant,
                        {"original_schedule_id": schedule.id},
                    )
                    cls._audit(
                        session,
                        replacement,
                        context,
                        "interview_schedule_reschedule_cancelled",
                    )
            if schedule.status == "confirmed_internal" and schedule.reservation_token is not None:
                session.execute(
                    delete(InterviewScheduleReservationRecord).where(
                        InterviewScheduleReservationRecord.organization_id == schedule.organization_id,
                        InterviewScheduleReservationRecord.schedule_id == schedule.id,
                        InterviewScheduleReservationRecord.reservation_token == schedule.reservation_token,
                    )
                )
            schedule.status = "cancelled"
            schedule.reservation_token = None
            schedule.cancellation_reason = payload.reason.strip()
            schedule.version += 1
            schedule.updated_at = instant
            cls._event(
                session,
                schedule,
                context.actor_user_id,
                "schedule_cancelled",
                "cancelled",
                instant,
                {
                    "start_at": cls._utc(schedule.start_at).isoformat(),
                    "end_at": cls._utc(schedule.end_at).isoformat(),
                    "reason": schedule.cancellation_reason,
                },
            )
            cls._audit(session, schedule, context, "interview_schedule_cancelled")
            return cls._serialize(schedule)

    @classmethod
    def get(
        cls, schedule_id: str, context: TenantContext
    ) -> InterviewScheduleDetailResponse:
        with SessionLocal() as session:
            schedule = cls._load_visible_schedule(session, schedule_id, context)
            serialized = cls._serialize(schedule).model_dump()
            events = session.execute(
                select(InterviewScheduleEventRecord)
                .where(
                    InterviewScheduleEventRecord.organization_id == schedule.organization_id,
                    InterviewScheduleEventRecord.schedule_id == schedule.id,
                )
                .order_by(InterviewScheduleEventRecord.created_at, InterviewScheduleEventRecord.id)
            ).scalars()
            serialized["events"] = [
                InterviewScheduleEventResponse(
                    event_type=event.event_type,
                    outcome=event.outcome,
                    details=json.loads(event.details_json),
                    created_at=cls._utc(event.created_at),
                )
                for event in events
            ]
            return InterviewScheduleDetailResponse(**serialized)

    @classmethod
    def list_for_campaign(
        cls,
        campaign_id: str,
        context: TenantContext,
        *,
        application_id: str | None = None,
    ) -> list[InterviewScheduleResponse]:
        cls._require_recruiter(context)
        with SessionLocal() as session:
            campaign = session.execute(
                select(RecruitmentCampaignRecord).where(
                    RecruitmentCampaignRecord.id == campaign_id,
                    RecruitmentCampaignRecord.organization_id == context.organization_id,
                )
            ).scalar_one_or_none()
            if campaign is None:
                raise HTTPException(status_code=404, detail="Campaign not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            query = select(InterviewScheduleRecord).where(
                InterviewScheduleRecord.organization_id == context.organization_id,
                InterviewScheduleRecord.campaign_id == campaign_id,
            )
            if application_id is not None:
                query = query.where(InterviewScheduleRecord.application_id == application_id)
            records = session.execute(
                query.order_by(InterviewScheduleRecord.created_at.desc())
            ).scalars()
            return [cls._serialize(record) for record in records]

    @classmethod
    def expire_due(cls, *, limit: int = 100, now: datetime | None = None) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("Expiration batch limit must be between 1 and 500")
        instant = cls._now(now)
        with SessionLocal.begin() as session:
            cls._lock_reservations(session)
            due = list(
                session.execute(
                    select(InterviewScheduleRecord)
                    .join(
                        RecruitmentApplicationRecord,
                        and_(
                            RecruitmentApplicationRecord.id
                            == InterviewScheduleRecord.application_id,
                            RecruitmentApplicationRecord.organization_id
                            == InterviewScheduleRecord.organization_id,
                        ),
                    )
                    .join(
                        RecruitmentCampaignRecord,
                        and_(
                            RecruitmentCampaignRecord.id == InterviewScheduleRecord.campaign_id,
                            RecruitmentCampaignRecord.organization_id
                            == InterviewScheduleRecord.organization_id,
                        ),
                    )
                    .where(
                        or_(
                            and_(
                                InterviewScheduleRecord.status.in_(
                                    ("proposed", "pending_confirmation", "reschedule_pending")
                                ),
                                InterviewScheduleRecord.expires_at <= instant,
                            ),
                            and_(
                                InterviewScheduleRecord.status.in_(
                                    (
                                        "proposed",
                                        "pending_confirmation",
                                        "confirmed_internal",
                                        "reschedule_pending",
                                    )
                                ),
                                or_(
                                    RecruitmentApplicationRecord.status != "needs_review",
                                    RecruitmentCampaignRecord.approval_state != "approved",
                                ),
                            ),
                        )
                    )
                    .order_by(InterviewScheduleRecord.expires_at, InterviewScheduleRecord.id)
                    .limit(limit)
                ).scalars()
            )
            transitioned = 0
            for schedule in due:
                app, campaign = cls._load_application(
                    session,
                    schedule.campaign_id,
                    schedule.application_id,
                    TenantContext(
                        actor_user_id=schedule.created_by_user_id,
                        organization_id=schedule.organization_id,
                        role="org_admin",
                    ),
                )
                ineligible = app.status != "needs_review" or campaign.approval_state != "approved"
                expired = (
                    schedule.status in {"proposed", "pending_confirmation", "reschedule_pending"}
                    and cls._utc(schedule.expires_at) <= instant
                )
                if not ineligible and not expired:
                    continue
                if schedule.status == "confirmed_internal":
                    expected_claims = len(cls._reservation_resources(schedule))
                    actual_claims = session.execute(
                        select(InterviewScheduleReservationRecord.id).where(
                            InterviewScheduleReservationRecord.organization_id
                            == schedule.organization_id,
                            InterviewScheduleReservationRecord.schedule_id == schedule.id,
                            InterviewScheduleReservationRecord.reservation_token
                            == schedule.reservation_token,
                        )
                    ).scalars().all()
                    if schedule.reservation_token is None or len(actual_claims) != expected_claims:
                        schedule.status = "needs_attention"
                        schedule.version += 1
                        schedule.updated_at = instant
                        cls._event(
                            session,
                            schedule,
                            None,
                            "reservation_integrity_requires_attention",
                            "needs_attention",
                            instant,
                            {"expected_claims": expected_claims, "actual_claims": len(actual_claims)},
                        )
                        transitioned += 1
                        continue
                    session.execute(
                        delete(InterviewScheduleReservationRecord).where(
                            InterviewScheduleReservationRecord.organization_id
                            == schedule.organization_id,
                            InterviewScheduleReservationRecord.schedule_id == schedule.id,
                            InterviewScheduleReservationRecord.reservation_token
                            == schedule.reservation_token,
                        )
                    )
                schedule.status = "cancelled" if ineligible else "expired"
                schedule.reservation_token = None
                schedule.cancellation_reason = (
                    "application_or_campaign_ineligible" if ineligible else "confirmation_timeout"
                )
                schedule.version += 1
                schedule.updated_at = instant
                cls._event(
                    session,
                    schedule,
                    None,
                    "schedule_cancelled_ineligible" if ineligible else "schedule_expired",
                    schedule.status,
                    instant,
                    {
                        "start_at": cls._utc(schedule.start_at).isoformat(),
                        "end_at": cls._utc(schedule.end_at).isoformat(),
                        "reason": schedule.cancellation_reason,
                    },
                )
                transitioned += 1
            return transitioned
