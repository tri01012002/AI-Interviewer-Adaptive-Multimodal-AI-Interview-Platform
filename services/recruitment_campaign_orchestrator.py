"""Durable, tenant-scoped orchestration for approved recruitment campaigns."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, case, exists, or_, select, update
from sqlalchemy.exc import IntegrityError

from config import settings
from integrations.llm_providers.provider import (
    LLMConfigurationError,
    LLMInvalidOutputError,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    provider_from_settings,
)
from services.candidate_availability_service import (
    AvailabilityRequestCreate,
    AvailabilityWindow,
    CandidateAvailabilityService,
)
from services.candidate_communication_service import (
    CandidateCommunicationService,
    CommunicationDraftRequest,
)
from services.database import (
    CAMPAIGN_WORKER_CONTROL_ID,
    AccessAuditRecord,
    CandidateAvailabilityRequestRecord,
    CandidateCommunicationRecord,
    InterviewRecord,
    InterviewScheduleRecord,
    OrganizationMembershipRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignExecutionRecord,
    RecruitmentCampaignExecutionStepRecord,
    RecruitmentCampaignHiringOutcomeRecord,
    RecruitmentCampaignRecord,
    RecruitmentCampaignWorkerControlRecord,
    SessionLocal,
)
from services.interview_scheduling_service import (
    InterviewScheduleProposal,
    InterviewScheduleTransition,
    InterviewSchedulingService,
)
from services.recruitment_campaign_service import RecruitmentCampaignService
from services.tenant_authorization import TenantContext

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class JobDesignInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    director_hiring_brief: str
    structured_requirements: dict[str, Any]
    job_description: str | None
    competency_rubric: list[dict[str, Any]]
    application_form_schema: dict[str, Any]


class GeneratedJobDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured_requirements: dict[str, Any] | None = None
    job_description: str | None = Field(default=None, min_length=1)
    competency_rubric: list[dict[str, Any]] | None = None
    application_form_schema: dict[str, Any] | None = None


class JobDesignResult(BaseModel):
    structured_requirements: dict[str, Any]
    job_description: str
    competency_rubric: list[dict[str, Any]]
    application_form_schema: dict[str, Any]
    output_source: Literal["existing", "openai", "mock", "test"]
    changed: bool


class ExecutionStepResponse(BaseModel):
    step_key: str
    status: str
    attempt_count: int
    output: dict[str, Any] | None
    output_source: str | None
    failure_code: str | None
    failure_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class CampaignExecutionResponse(BaseModel):
    id: str
    campaign_id: str
    organization_id: str
    requested_by_user_id: str
    status: str
    attempt_count: int
    max_attempts: int
    failure_code: str | None
    failure_message: str | None
    available_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    steps: list[ExecutionStepResponse]


class CampaignAdvancementResponse(BaseModel):
    campaign_id: str
    application_id: str
    action: str
    resulting_id: str | None = None


class CampaignAutomationPolicy(BaseModel):
    """Explicit approved scheduling policy; missing policy remains a human gate."""

    model_config = ConfigDict(extra="forbid")

    response_deadline: datetime
    interview_duration_minutes: int = Field(ge=15, le=240)
    timezone_name: str = Field(min_length=1, max_length=64)
    proposed_windows: list[AvailabilityWindow] = Field(min_length=1, max_length=10)
    interviewer_user_id: str = Field(min_length=1, max_length=64)
    exclusive_resources: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("response_deadline")
    @classmethod
    def validate_response_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Automation policy datetimes must be timezone-aware")
        return value

    @field_validator("proposed_windows")
    @classmethod
    def validate_proposed_windows(
        cls, value: list[AvailabilityWindow]
    ) -> list[AvailabilityWindow]:
        if any(
            window.start.tzinfo is None
            or window.start.utcoffset() is None
            or window.end.tzinfo is None
            or window.end.utcoffset() is None
            for window in value
        ):
            raise ValueError("Automation policy windows must be timezone-aware")
        return value

    @field_validator("timezone_name")
    @classmethod
    def validate_timezone_name(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Automation policy timezone must be a valid IANA zone") from exc
        return value


@dataclass(frozen=True)
class _ApplicationAdvancementState:
    campaign_id: str
    application_id: str
    context: TenantContext
    communication: CandidateCommunicationRecord | None
    request: CandidateAvailabilityRequestRecord | None
    schedule: InterviewScheduleRecord | None
    policy_state: str
    policy: CampaignAutomationPolicy | None


class JobDesignNeedsAttention(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class JobDesignAgent:
    """Validate stored campaign artifacts and generate only missing artifacts."""

    def __init__(self, provider: LLMProvider | None) -> None:
        self._provider = provider

    def execute(self, payload: JobDesignInput) -> JobDesignResult:
        missing = self._missing_artifacts(payload)
        if not missing:
            return JobDesignResult(
                structured_requirements=payload.structured_requirements,
                job_description=payload.job_description or "",
                competency_rubric=payload.competency_rubric,
                application_form_schema=payload.application_form_schema,
                output_source="existing",
                changed=False,
            )

        if self._provider is None:
            raise JobDesignNeedsAttention(
                "llm_provider_disabled",
                "Approved campaign is missing valid artifacts "
                f"({', '.join(missing)}); configure a provider or update the plan.",
            )

        generated = self._provider.generate_structured(
            "Design a recruitment plan from the hiring brief. "
            "Return only the missing or invalid artifacts, with objective, "
            "job-related criteria. Do not make candidate decisions.",
            payload.model_dump_json(),
            GeneratedJobDesign,
        )
        requirements = (
            payload.structured_requirements
            if "structured_requirements" not in missing
            else generated.structured_requirements
        )
        job_description = (
            payload.job_description
            if "job_description" not in missing
            else generated.job_description
        )
        rubric = (
            payload.competency_rubric
            if "competency_rubric" not in missing
            else generated.competency_rubric
        )
        form_schema = (
            payload.application_form_schema
            if "application_form_schema" not in missing
            else generated.application_form_schema
        )
        if (
            not requirements
            or not job_description
            or not rubric
            or not self._valid_form_schema(form_schema)
        ):
            raise JobDesignNeedsAttention(
                "incomplete_agent_output",
                "Job Design Agent output did not provide all required campaign artifacts.",
            )
        source = self._provider.provider_name
        if source not in {"mock", "openai"}:
            source = "test"
        return JobDesignResult(
            structured_requirements=requirements,
            job_description=job_description,
            competency_rubric=rubric,
            application_form_schema=form_schema,
            output_source=source,
            changed=True,
        )

    @classmethod
    def _missing_artifacts(cls, payload: JobDesignInput) -> list[str]:
        missing: list[str] = []
        if not payload.structured_requirements:
            missing.append("structured_requirements")
        if not payload.job_description or not payload.job_description.strip():
            missing.append("job_description")
        if not payload.competency_rubric:
            missing.append("competency_rubric")
        if not cls._valid_form_schema(payload.application_form_schema):
            missing.append("application_form_schema")
        return missing

    @staticmethod
    def _valid_form_schema(value: dict[str, Any] | None) -> bool:
        return (
            isinstance(value, dict)
            and value.get("type") == "object"
            and isinstance(value.get("properties"), dict)
            and bool(value["properties"])
        )


class RecruitmentCampaignOrchestrator:
    @staticmethod
    def _audit(
        session,
        *,
        context: TenantContext,
        action: str,
        resource_type: str,
        resource_id: str,
    ) -> None:
        session.add(
            AccessAuditRecord(
                actor_user_id=None if context.is_automated else context.actor_user_id,
                organization_id=context.organization_id,
                requested_organization_id=context.organization_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome="allowed",
                support_reason=context.support_reason,
            )
        )

    @staticmethod
    def _require_execution_role(context: TenantContext) -> None:
        if context.role not in {"recruiter", "org_admin", "platform_admin"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Campaign execution access denied",
            )

    @classmethod
    def start(
        cls,
        campaign_id: str,
        context: TenantContext,
        provider: LLMProvider | None = None,
    ) -> CampaignExecutionResponse:
        cls._require_execution_role(context)
        try:
            with SessionLocal.begin() as session:
                campaign = session.execute(
                    select(RecruitmentCampaignRecord).where(
                        RecruitmentCampaignRecord.id == campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == context.organization_id,
                    )
                ).scalar_one_or_none()
                if campaign is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Campaign not found",
                    )
                RecruitmentCampaignService._assert_authorized(campaign, context)
                if not RecruitmentCampaignService.approval_is_current(campaign):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A complete, current campaign objective and plan approval are required before execution",
                    )
                execution = session.execute(
                    select(RecruitmentCampaignExecutionRecord).where(
                        RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
                    )
                ).scalar_one_or_none()
                if execution is None:
                    now = datetime.now(UTC)
                    execution = RecruitmentCampaignExecutionRecord(
                        campaign_id=campaign.id,
                        organization_id=campaign.organization_id,
                        requested_by_user_id=context.actor_user_id,
                        status="pending",
                        max_attempts=MAX_ATTEMPTS,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(execution)
                    session.flush()
                    session.add(
                        RecruitmentCampaignExecutionStepRecord(
                            execution_id=execution.id,
                            step_key="job_design",
                            status="pending",
                            attempt_count=0,
                            input_json=cls._campaign_input(campaign).model_dump_json(),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    cls._audit(
                        session,
                        context=context,
                        action="campaign_execution_requested",
                        resource_type="recruitment_campaign_execution",
                        resource_id=execution.id,
                    )
                execution_id = execution.id
        except IntegrityError:
            with SessionLocal() as session:
                existing = session.execute(
                    select(RecruitmentCampaignExecutionRecord).where(
                        RecruitmentCampaignExecutionRecord.campaign_id == campaign_id,
                        RecruitmentCampaignExecutionRecord.organization_id
                        == context.organization_id,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                execution_id = existing.id

        return cls._run(execution_id, context, provider)

    @classmethod
    def resume(
        cls,
        execution_id: str,
        context: TenantContext,
        provider: LLMProvider | None = None,
    ) -> CampaignExecutionResponse:
        cls._require_execution_role(context)
        return cls._run(
            execution_id,
            context,
            provider,
            allow_manual_retry=True,
        )

    @classmethod
    def dispatch_once(
        cls,
        *,
        limit: int,
        provider: LLMProvider | None = None,
    ) -> list[CampaignExecutionResponse]:
        """Discover approved work and process a bounded batch of execution IDs."""
        execution_ids = cls.discover_eligible(limit)
        results: list[CampaignExecutionResponse] = []
        for execution_id in execution_ids:
            result = cls.run_dispatched(execution_id, provider=provider)
            if result is not None:
                results.append(result)
        return results

    @classmethod
    def discover_eligible(cls, limit: int) -> list[str]:
        """Create missing runs for approved campaigns and return eligible run IDs."""
        if limit < 1:
            return []
        cls._create_approved_executions(limit)
        return cls._eligible_execution_ids(limit)

    @classmethod
    def run_dispatched(
        cls,
        execution_id: str,
        *,
        provider: LLMProvider | None = None,
    ) -> CampaignExecutionResponse | None:
        context = cls._worker_context(execution_id)
        if context is None:
            return None
        return cls._run(
            execution_id,
            context,
            provider,
            worker_internal=True,
        )

    @classmethod
    def advance_eligible_applications(
        cls, limit: int
    ) -> list[CampaignAdvancementResponse]:
        """Apply only safe, idempotent system-owned campaign handoffs."""
        if limit < 1:
            return []
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
                        RecruitmentApplicationRecord.status == "needs_review",
                        RecruitmentApplicationRecord.review_status == "reviewed",
                        RecruitmentCampaignRecord.approval_state == "approved",
                        RecruitmentCampaignRecord.approved_objective_version
                        == RecruitmentCampaignRecord.objective_version,
                        RecruitmentCampaignRecord.approved_plan_version
                        == RecruitmentCampaignRecord.plan_version,
                        ~exists().where(
                            RecruitmentCampaignHiringOutcomeRecord.application_id
                            == RecruitmentApplicationRecord.id
                        ),
                    )
                    .order_by(RecruitmentApplicationRecord.updated_at)
                    .limit(100)
                ).scalars()
            )

        advanced: list[CampaignAdvancementResponse] = []
        for application_id in application_ids:
            # One application's rejected handoff must not block the others:
            # it stays in its current stage and is retried on a later tick.
            try:
                result = cls.advance_application_once(application_id)
            except HTTPException as exc:
                logger.warning(
                    "Recruitment application advancement skipped",
                    extra={
                        "application_id": application_id,
                        "status_code": exc.status_code,
                    },
                )
                continue
            if result is not None:
                advanced.append(result)
                if len(advanced) >= limit:
                    break
        return advanced

    @classmethod
    def advance_application_once(
        cls, application_id: str
    ) -> CampaignAdvancementResponse | None:
        """Advance one application by one approved, durable system action."""
        state = cls._load_application_advancement_state(application_id)
        if state is None:
            return None
        if state.communication is None:
            communication = CandidateCommunicationService.create_draft(
                application_id,
                state.context,
                CommunicationDraftRequest(
                    template_type="interview_invitation",
                    idempotency_key=f"r53:invitation:{application_id}",
                ),
            )
            return CampaignAdvancementResponse(
                campaign_id=state.campaign_id,
                application_id=application_id,
                action="communication_draft_created",
                resulting_id=communication.id,
            )
        if state.communication.status not in {"sent", "simulated"}:
            return None
        if state.request is None:
            return cls._create_automation_availability_request(state)
        if state.request.status != "reviewed":
            return None
        return cls._advance_to_schedule_confirmation(state)

    @classmethod
    def _load_application_advancement_state(
        cls, application_id: str
    ) -> _ApplicationAdvancementState | None:
        with SessionLocal() as session:
            application = session.execute(
                select(RecruitmentApplicationRecord).where(
                    RecruitmentApplicationRecord.id == application_id
                )
            ).scalar_one_or_none()
            if application is None:
                return None
            campaign = session.execute(
                select(RecruitmentCampaignRecord).where(
                    RecruitmentCampaignRecord.id == application.campaign_id,
                    RecruitmentCampaignRecord.organization_id
                    == application.organization_id,
                )
            ).scalar_one_or_none()
            if (
                campaign is None
                or not RecruitmentCampaignService.approval_is_current(campaign)
                or not RecruitmentCampaignService._objective_complete(campaign)
                or RecruitmentCampaignService.target_is_achieved(session, campaign)
                or application.status != "needs_review"
                or application.review_status != "reviewed"
            ):
                return None

            membership = session.execute(
                select(OrganizationMembershipRecord).where(
                    OrganizationMembershipRecord.organization_id
                    == campaign.organization_id,
                    OrganizationMembershipRecord.user_id == campaign.owner_user_id,
                    OrganizationMembershipRecord.status == "active",
                )
            ).scalar_one_or_none()
            if membership is None or membership.role not in {"recruiter", "org_admin"}:
                return None
            context = TenantContext(
                actor_user_id=campaign.owner_user_id,
                organization_id=campaign.organization_id,
                role=membership.role,
                is_automated=True,
            )
            policy_state, policy = cls._automation_policy(campaign)
            communication = session.execute(
                select(CandidateCommunicationRecord)
                .where(
                    CandidateCommunicationRecord.organization_id
                    == campaign.organization_id,
                    CandidateCommunicationRecord.application_id == application.id,
                    CandidateCommunicationRecord.template_type
                    == "interview_invitation",
                )
                .order_by(CandidateCommunicationRecord.updated_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            request = session.execute(
                select(CandidateAvailabilityRequestRecord)
                .where(
                    CandidateAvailabilityRequestRecord.organization_id
                    == campaign.organization_id,
                    CandidateAvailabilityRequestRecord.application_id
                    == application.id,
                )
                .order_by(CandidateAvailabilityRequestRecord.updated_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if request is None:
                policy_state = cls._availability_policy_state(
                    policy_state, policy, datetime.now(UTC)
                )
            schedule = session.execute(
                select(InterviewScheduleRecord)
                .where(
                    InterviewScheduleRecord.organization_id
                    == campaign.organization_id,
                    InterviewScheduleRecord.application_id == application.id,
                )
                .order_by(InterviewScheduleRecord.updated_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if session.execute(
                select(RecruitmentCampaignHiringOutcomeRecord.id).where(
                    RecruitmentCampaignHiringOutcomeRecord.organization_id
                    == campaign.organization_id,
                    RecruitmentCampaignHiringOutcomeRecord.application_id
                    == application.id,
                )
            ).scalar_one_or_none():
                return None
            return _ApplicationAdvancementState(
                campaign_id=campaign.id,
                application_id=application_id,
                context=context,
                communication=communication,
                request=request,
                schedule=schedule,
                policy_state=policy_state,
                policy=policy,
            )

    @staticmethod
    def _create_automation_availability_request(
        state: _ApplicationAdvancementState,
    ) -> CampaignAdvancementResponse | None:
        if state.policy_state != "available" or state.policy is None:
            return None
        request = CandidateAvailabilityService.create_request(
            state.campaign_id,
            state.context,
            AvailabilityRequestCreate(
                application_id=state.application_id,
                idempotency_key=f"r53:availability:{state.application_id}",
                response_deadline=state.policy.response_deadline,
                interview_duration_minutes=state.policy.interview_duration_minutes,
                timezone_name=state.policy.timezone_name,
                proposed_windows=state.policy.proposed_windows,
            ),
        )
        return CampaignAdvancementResponse(
            campaign_id=state.campaign_id,
            application_id=state.application_id,
            action="availability_request_created",
            resulting_id=request.id,
        )

    @classmethod
    def _advance_to_schedule_confirmation(
        cls, state: _ApplicationAdvancementState
    ) -> CampaignAdvancementResponse | None:
        if state.schedule is not None:
            if state.schedule.status != "proposed":
                return None
            submitted = InterviewSchedulingService.submit(
                state.schedule.id,
                state.context,
                InterviewScheduleTransition(expected_version=state.schedule.version),
            )
            return CampaignAdvancementResponse(
                campaign_id=state.campaign_id,
                application_id=state.application_id,
                action="schedule_submitted_for_confirmation",
                resulting_id=submitted.id,
            )
        if state.policy_state != "available" or state.policy is None:
            return None
        compatible = InterviewSchedulingService.compatible_slots(
            state.request.id, state.context
        )
        if not compatible.windows:
            return None
        proposed = InterviewSchedulingService.propose(
            state.campaign_id,
            state.application_id,
            state.context,
            InterviewScheduleProposal(
                availability_request_id=state.request.id,
                window_index=int(compatible.windows[0]["window_index"]),
                interviewer_user_id=state.policy.interviewer_user_id,
                exclusive_resources=state.policy.exclusive_resources,
                idempotency_key=f"r53:schedule:{state.application_id}",
            ),
        )
        submitted = InterviewSchedulingService.submit(
            proposed.id,
            state.context,
            InterviewScheduleTransition(expected_version=proposed.version),
        )
        return CampaignAdvancementResponse(
            campaign_id=state.campaign_id,
            application_id=state.application_id,
            action="schedule_submitted_for_confirmation",
            resulting_id=submitted.id,
        )

    @staticmethod
    def _automation_policy(
        campaign: RecruitmentCampaignRecord,
    ) -> tuple[str, CampaignAutomationPolicy | None]:
        try:
            requirements = json.loads(campaign.structured_requirements_json or "{}")
            if not isinstance(requirements, dict):
                return "invalid", None
            if "campaign_automation" not in requirements:
                return "missing", None
            policy = CampaignAutomationPolicy.model_validate(
                requirements["campaign_automation"]
            )
            return "available", policy
        except (json.JSONDecodeError, TypeError, ValueError):
            return "invalid", None

    @staticmethod
    def _availability_policy_state(
        policy_state: str,
        policy: CampaignAutomationPolicy | None,
        now: datetime,
    ) -> str:
        if policy_state != "available" or policy is None:
            return policy_state
        if policy.response_deadline <= now:
            return "expired"
        try:
            CandidateAvailabilityService._normalize_windows(
                policy.proposed_windows,
                policy.timezone_name,
                policy.interview_duration_minutes,
                now=now,
            )
        except HTTPException:
            return "invalid"
        return "available"

    @classmethod
    def _create_approved_executions(cls, limit: int) -> None:
        with SessionLocal() as session:
            campaign_ids = session.execute(
                select(RecruitmentCampaignRecord.id)
                .where(
                    RecruitmentCampaignRecord.approval_state == "approved",
                    RecruitmentCampaignRecord.approved_objective_version
                    == RecruitmentCampaignRecord.objective_version,
                    RecruitmentCampaignRecord.approved_plan_version
                    == RecruitmentCampaignRecord.plan_version,
                    ~exists().where(
                        RecruitmentCampaignExecutionRecord.campaign_id
                        == RecruitmentCampaignRecord.id
                    ),
                )
                .order_by(RecruitmentCampaignRecord.updated_at)
                .limit(limit)
            ).scalars().all()

        for campaign_id in campaign_ids:
            try:
                dispatched: tuple[str, str] | None = None
                with SessionLocal.begin() as session:
                    campaign = session.execute(
                        select(RecruitmentCampaignRecord).where(
                            RecruitmentCampaignRecord.id == campaign_id,
                            RecruitmentCampaignRecord.approval_state == "approved",
                            RecruitmentCampaignRecord.approved_objective_version
                            == RecruitmentCampaignRecord.objective_version,
                            RecruitmentCampaignRecord.approved_plan_version
                            == RecruitmentCampaignRecord.plan_version,
                            ~exists().where(
                                RecruitmentCampaignExecutionRecord.campaign_id
                                == RecruitmentCampaignRecord.id
                            ),
                        )
                    ).scalar_one_or_none()
                    if campaign is None:
                        continue
                    now = datetime.now(UTC)
                    execution = RecruitmentCampaignExecutionRecord(
                        campaign_id=campaign.id,
                        organization_id=campaign.organization_id,
                        requested_by_user_id=(
                            campaign.approval_decided_by_user_id
                            or campaign.owner_user_id
                        ),
                        status="pending",
                        attempt_count=0,
                        max_attempts=MAX_ATTEMPTS,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(execution)
                    session.flush()
                    session.add(
                        RecruitmentCampaignExecutionStepRecord(
                            execution_id=execution.id,
                            step_key="job_design",
                            status="pending",
                            attempt_count=0,
                            input_json=cls._campaign_input(campaign).model_dump_json(),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    context = TenantContext(
                        actor_user_id=execution.requested_by_user_id,
                        organization_id=campaign.organization_id,
                        role="recruiter",
                    )
                    cls._audit(
                        session,
                        context=context,
                        action="campaign_execution_dispatched",
                        resource_type="recruitment_campaign_execution",
                        resource_id=execution.id,
                    )
                    dispatched = (campaign.id, execution.id)
                if dispatched is not None:
                    logger.info(
                        "Approved recruitment campaign queued for execution",
                        extra={
                            "campaign_id": dispatched[0],
                            "execution_id": dispatched[1],
                        },
                    )
            except IntegrityError:
                with SessionLocal() as session:
                    existing = session.execute(
                        select(RecruitmentCampaignExecutionRecord.id).where(
                            RecruitmentCampaignExecutionRecord.campaign_id
                            == campaign_id
                        )
                    ).scalar_one_or_none()
                if existing is None:
                    raise

    @staticmethod
    def _eligible_execution_ids(limit: int) -> list[str]:
        now = datetime.now(UTC)
        due_retry = and_(
            RecruitmentCampaignExecutionRecord.status == "retryable_failure",
            RecruitmentCampaignExecutionRecord.available_at <= now,
        )
        with SessionLocal() as session:
            return list(
                session.execute(
                    select(RecruitmentCampaignExecutionRecord.id)
                    .join(
                        RecruitmentCampaignRecord,
                        and_(
                            RecruitmentCampaignRecord.id
                            == RecruitmentCampaignExecutionRecord.campaign_id,
                            RecruitmentCampaignRecord.organization_id
                            == RecruitmentCampaignExecutionRecord.organization_id,
                        ),
                    )
                    .where(
                        or_(
                            and_(
                                RecruitmentCampaignRecord.approval_state
                                == "approved",
                                RecruitmentCampaignRecord.approved_objective_version
                                == RecruitmentCampaignRecord.objective_version,
                                RecruitmentCampaignRecord.approved_plan_version
                                == RecruitmentCampaignRecord.plan_version,
                                or_(
                                    RecruitmentCampaignExecutionRecord.status
                                    == "pending",
                                    due_retry,
                                    and_(
                                        RecruitmentCampaignExecutionRecord.status
                                        == "awaiting_progress",
                                        RecruitmentCampaignExecutionRecord.available_at
                                        <= now,
                                    ),
                                    and_(
                                        RecruitmentCampaignExecutionRecord.status
                                        == "running",
                                        RecruitmentCampaignExecutionRecord.lease_expires_at
                                        <= now,
                                    ),
                                ),
                            ),
                            and_(
                                RecruitmentCampaignRecord.approval_state
                                != "approved",
                                RecruitmentCampaignExecutionRecord.status.in_(
                                    (
                                        "pending",
                                        "retryable_failure",
                                        "awaiting_progress",
                                    )
                                ),
                            ),
                        ),
                    )
                    .order_by(RecruitmentCampaignExecutionRecord.created_at)
                    .limit(limit)
                ).scalars()
            )

    @staticmethod
    def _worker_context(execution_id: str) -> TenantContext | None:
        with SessionLocal() as session:
            row = session.execute(
                select(
                    RecruitmentCampaignExecutionRecord.organization_id,
                    RecruitmentCampaignExecutionRecord.requested_by_user_id,
                )
                .join(
                    RecruitmentCampaignRecord,
                    and_(
                        RecruitmentCampaignRecord.id
                        == RecruitmentCampaignExecutionRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == RecruitmentCampaignExecutionRecord.organization_id,
                    ),
                )
                .where(
                    RecruitmentCampaignExecutionRecord.id == execution_id,
                )
            ).one_or_none()
            if row is None:
                return None
            return TenantContext(
                actor_user_id=row.requested_by_user_id,
                organization_id=row.organization_id,
                role="recruiter",
                is_automated=True,
            )

    @classmethod
    def get(
        cls, execution_id: str, context: TenantContext
    ) -> CampaignExecutionResponse:
        cls._require_execution_role(context)
        with SessionLocal() as session:
            execution = session.execute(
                select(RecruitmentCampaignExecutionRecord).where(
                    RecruitmentCampaignExecutionRecord.id == execution_id,
                    RecruitmentCampaignExecutionRecord.organization_id
                    == context.organization_id,
                )
            ).scalar_one_or_none()
            if execution is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Campaign execution not found",
                )
            campaign = session.get(RecruitmentCampaignRecord, execution.campaign_id)
            if campaign is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Campaign not found",
                )
            RecruitmentCampaignService._assert_authorized(campaign, context)
            return cls._serialize(session, execution)

    @classmethod
    def _run(
        cls,
        execution_id: str,
        context: TenantContext,
        provider: LLMProvider | None,
        *,
        worker_internal: bool = False,
        allow_manual_retry: bool = False,
    ) -> CampaignExecutionResponse:
        if not worker_internal:
            cls._require_execution_role(context)
        lease_token = str(uuid4())
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            execution = session.execute(
                select(RecruitmentCampaignExecutionRecord).where(
                    RecruitmentCampaignExecutionRecord.id == execution_id,
                    RecruitmentCampaignExecutionRecord.organization_id
                    == context.organization_id,
                )
            ).scalar_one_or_none()
            if execution is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Campaign execution not found",
                )
            campaign = session.get(RecruitmentCampaignRecord, execution.campaign_id)
            if campaign is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Campaign not found",
                )
            if execution.organization_id != campaign.organization_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Campaign execution not found",
                )
            if not worker_internal:
                RecruitmentCampaignService._assert_authorized(campaign, context)
            if not RecruitmentCampaignService.approval_is_current(campaign):
                if execution.status in {
                    "pending",
                    "running",
                    "retryable_failure",
                    "waiting_for_approval",
                    "awaiting_progress",
                }:
                    execution.status = "needs_attention"
                    execution.failure_code = (
                        "campaign_approval_revoked"
                        if campaign.approval_state != "approved"
                        else "campaign_objective_approval_stale"
                    )
                    execution.failure_message = (
                        "A complete, current campaign objective and plan approval are "
                        "required; execution was stopped before claiming work."
                    )
                    execution.lease_token = None
                    execution.lease_expires_at = None
                    execution.available_at = None
                    execution.updated_at = now
                    cls._audit(
                        session,
                        context=context,
                        action="campaign_execution_needs_attention",
                        resource_type="recruitment_campaign_execution",
                        resource_id=execution.id,
                    )
                return cls._serialize(session, execution)
            if execution.status in {"completed", "needs_attention"}:
                return cls._serialize(session, execution)
            worker_control = session.get(
                RecruitmentCampaignWorkerControlRecord,
                CAMPAIGN_WORKER_CONTROL_ID,
            )
            if worker_control is not None and worker_control.paused:
                return cls._serialize(session, execution)
            lease_expires_at = execution.lease_expires_at
            lease_expired = (
                lease_expires_at is not None
                and (
                    lease_expires_at.replace(tzinfo=UTC)
                    if lease_expires_at.tzinfo is None
                    else lease_expires_at
                )
                <= now
            )
            if (
                execution.status == "running"
                and lease_expired
                and execution.attempt_count >= execution.max_attempts
            ):
                execution.status = "needs_attention"
                execution.failure_code = "attempt_limit_reached"
                execution.failure_message = (
                    "Execution lease expired after the maximum number of attempts; "
                    "inspect the persisted step and resolve it manually."
                )
                execution.lease_token = None
                execution.lease_expires_at = None
                execution.available_at = None
                execution.updated_at = now
                cls._audit(
                    session,
                    context=context,
                    action="campaign_execution_needs_attention",
                    resource_type="recruitment_campaign_execution",
                    resource_id=execution.id,
                )
                return cls._serialize(session, execution)
            if execution.status == "waiting_for_approval":
                execution.status = "pending"
                execution.available_at = None
                execution.updated_at = now
                cls._audit(
                    session,
                    context=context,
                    action="campaign_execution_resumed_after_approval",
                    resource_type="recruitment_campaign_execution",
                    resource_id=execution.id,
                )
            claimed = session.execute(
                update(RecruitmentCampaignExecutionRecord)
                .where(
                    RecruitmentCampaignExecutionRecord.id == execution_id,
                    RecruitmentCampaignExecutionRecord.organization_id
                    == context.organization_id,
                    or_(
                        RecruitmentCampaignExecutionRecord.status
                        == "awaiting_progress",
                        RecruitmentCampaignExecutionRecord.attempt_count
                        < RecruitmentCampaignExecutionRecord.max_attempts,
                    ),
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == RecruitmentCampaignExecutionRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == RecruitmentCampaignExecutionRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state == "approved",
                        RecruitmentCampaignRecord.approved_objective_version
                        == RecruitmentCampaignRecord.objective_version,
                        RecruitmentCampaignRecord.approved_plan_version
                        == RecruitmentCampaignRecord.plan_version,
                    ),
                    ~exists().where(
                        RecruitmentCampaignWorkerControlRecord.id
                        == CAMPAIGN_WORKER_CONTROL_ID,
                        RecruitmentCampaignWorkerControlRecord.paused.is_(True),
                    ),
                    or_(
                        RecruitmentCampaignExecutionRecord.status == "pending",
                        and_(
                            RecruitmentCampaignExecutionRecord.status
                            == "awaiting_progress",
                            RecruitmentCampaignExecutionRecord.available_at <= now,
                        ),
                        and_(
                            RecruitmentCampaignExecutionRecord.status
                            == "retryable_failure",
                            or_(
                                allow_manual_retry,
                                RecruitmentCampaignExecutionRecord.available_at <= now,
                            ),
                        ),
                        (
                            (RecruitmentCampaignExecutionRecord.status == "running")
                            & (
                                RecruitmentCampaignExecutionRecord.lease_expires_at
                                <= now
                            )
                        ),
                    ),
                )
                .values(
                    status="running",
                    attempt_count=case(
                        (
                            RecruitmentCampaignExecutionRecord.status
                            == "awaiting_progress",
                            RecruitmentCampaignExecutionRecord.attempt_count,
                        ),
                        else_=RecruitmentCampaignExecutionRecord.attempt_count + 1,
                    ),
                    lease_token=lease_token,
                    lease_expires_at=now
                    + timedelta(seconds=settings.CAMPAIGN_WORKER_LEASE_SECONDS),
                    available_at=None,
                    failure_code=None,
                    failure_message=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                session.refresh(execution)
                campaign = session.get(
                    RecruitmentCampaignRecord, execution.campaign_id
                )
                if (
                    campaign is not None
                    and not RecruitmentCampaignService.approval_is_current(campaign)
                    and execution.status
                    in {"pending", "retryable_failure"}
                ):
                    execution.status = "needs_attention"
                    execution.failure_code = "campaign_approval_revoked"
                    execution.failure_message = (
                        "Campaign approval changed before the execution claim; "
                        "no job design work was performed."
                    )
                    execution.available_at = None
                    execution.updated_at = datetime.now(UTC)
                    cls._audit(
                        session,
                        context=context,
                        action="campaign_execution_needs_attention",
                        resource_type="recruitment_campaign_execution",
                        resource_id=execution.id,
                    )
                return cls._serialize(session, execution)
            execution = session.get(RecruitmentCampaignExecutionRecord, execution_id)
            session.refresh(execution)
            step = session.execute(
                select(RecruitmentCampaignExecutionStepRecord).where(
                    RecruitmentCampaignExecutionStepRecord.execution_id == execution_id,
                    RecruitmentCampaignExecutionStepRecord.step_key == "job_design",
                )
            ).scalar_one()
            if step.status == "completed":
                cls._complete_execution(session, execution, context)
                return cls._serialize(session, execution)
            cls._audit(
                session,
                context=context,
                action="campaign_execution_started",
                resource_type="recruitment_campaign_execution",
                resource_id=execution_id,
            )
            step.status = "running"
            step.attempt_count += 1
            step.started_at = now
            step.updated_at = now
            step.failure_code = None
            step.failure_message = None
            execution.available_at = None

        try:
            input_data = JobDesignInput.model_validate_json(step.input_json)
            result = JobDesignAgent(
                provider if provider is not None else provider_from_settings(settings)
            ).execute(input_data)
        except ValueError:
            return cls._fail(
                execution_id,
                step.id,
                lease_token,
                context,
                "invalid_persisted_step_input",
                "Persisted campaign step input is invalid; inspect and correct the campaign plan.",
                retryable=False,
            )
        except JobDesignNeedsAttention as exc:
            return cls._fail(
                execution_id,
                step.id,
                lease_token,
                context,
                exc.code,
                str(exc),
                retryable=False,
            )
        except (LLMConfigurationError, LLMInvalidOutputError) as exc:
            return cls._fail(
                execution_id,
                step.id,
                lease_token,
                context,
                type(exc).__name__,
                str(exc),
                retryable=False,
            )
        except (LLMTimeoutError, LLMRateLimitError, LLMProviderError) as exc:
            return cls._fail(
                execution_id,
                step.id,
                lease_token,
                context,
                type(exc).__name__,
                str(exc),
                retryable=True,
            )

        with SessionLocal.begin() as session:
            execution = session.get(RecruitmentCampaignExecutionRecord, execution_id)
            step = session.get(RecruitmentCampaignExecutionStepRecord, step.id)
            now = datetime.now(UTC)
            final_status = (
                "waiting_for_approval" if result.changed else "awaiting_progress"
            )
            completion_update = session.execute(
                update(RecruitmentCampaignExecutionRecord)
                .where(
                    RecruitmentCampaignExecutionRecord.id == execution_id,
                    RecruitmentCampaignExecutionRecord.status == "running",
                    RecruitmentCampaignExecutionRecord.lease_token == lease_token,
                    RecruitmentCampaignExecutionRecord.lease_expires_at > now,
                    exists().where(
                        RecruitmentCampaignRecord.id
                        == RecruitmentCampaignExecutionRecord.campaign_id,
                        RecruitmentCampaignRecord.organization_id
                        == RecruitmentCampaignExecutionRecord.organization_id,
                        RecruitmentCampaignRecord.approval_state == "approved",
                        RecruitmentCampaignRecord.approved_objective_version
                        == RecruitmentCampaignRecord.objective_version,
                        RecruitmentCampaignRecord.approved_plan_version
                        == RecruitmentCampaignRecord.plan_version,
                    ),
                )
                .values(
                    status=final_status,
                    lease_token=None,
                    lease_expires_at=None,
                    available_at=None,
                    completed_at=None,
                    failure_code=None,
                    failure_message=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if completion_update.rowcount != 1:
                session.refresh(execution)
                campaign = session.get(
                    RecruitmentCampaignRecord, execution.campaign_id
                )
                if (
                    execution.status == "running"
                    and execution.lease_token == lease_token
                    and campaign is not None
                    and not RecruitmentCampaignService.approval_is_current(campaign)
                ):
                    execution.status = "needs_attention"
                    execution.failure_code = "campaign_approval_revoked"
                    execution.failure_message = (
                        "Campaign approval was revoked while the execution was running; "
                        "the generated result was not persisted."
                    )
                    execution.lease_token = None
                    execution.lease_expires_at = None
                    execution.available_at = None
                    execution.updated_at = now
                    cls._audit(
                        session,
                        context=context,
                        action="campaign_execution_needs_attention",
                        resource_type="recruitment_campaign_execution",
                        resource_id=execution.id,
                    )
                if execution.status == "running" and execution.lease_token == lease_token:
                    session.refresh(execution)
                return cls._serialize(session, execution)
            session.refresh(execution)
            campaign = session.get(RecruitmentCampaignRecord, execution.campaign_id)
            campaign.structured_requirements_json = json.dumps(
                result.structured_requirements
            )
            campaign.job_description = result.job_description
            campaign.competency_rubric_json = json.dumps(result.competency_rubric)
            campaign.application_form_schema_json = json.dumps(
                result.application_form_schema
            )
            step.output_json = result.model_dump_json()
            step.output_source = result.output_source
            step.status = "completed"
            step.failure_code = None
            step.failure_message = None
            step.completed_at = datetime.now(UTC)
            step.updated_at = step.completed_at
            execution.updated_at = step.completed_at
            if result.changed:
                campaign.plan_version += 1
                campaign.approval_state = "pending_review"
                campaign.workflow_state = "pending_approval"
                campaign.approval_reason = None
                campaign.approval_decided_by_user_id = None
                campaign.approval_decided_at = None
                campaign.approved_objective_version = None
                campaign.approved_plan_version = None
                campaign.updated_at = step.completed_at
                cls._audit(
                    session,
                    context=context,
                    action="campaign_plan_reapproval_required",
                    resource_type="recruitment_campaign",
                    resource_id=campaign.id,
                )
            else:
                cls._complete_execution(session, execution, context)
            return cls._serialize(session, execution)

    @staticmethod
    def _campaign_input(campaign: RecruitmentCampaignRecord) -> JobDesignInput:
        return JobDesignInput(
            director_hiring_brief=campaign.director_hiring_brief,
            structured_requirements=json.loads(
                campaign.structured_requirements_json or "{}"
            ),
            job_description=campaign.job_description,
            competency_rubric=json.loads(campaign.competency_rubric_json or "[]"),
            application_form_schema=json.loads(
                campaign.application_form_schema_json or "{}"
            ),
        )

    @classmethod
    def _complete_execution(
        cls,
        session,
        execution: RecruitmentCampaignExecutionRecord,
        context: TenantContext,
    ) -> None:
        campaign = session.get(RecruitmentCampaignRecord, execution.campaign_id)
        if campaign is None:
            raise RuntimeError("Campaign disappeared during lifecycle reconciliation")
        session.refresh(campaign)
        now = datetime.now(UTC)
        if not RecruitmentCampaignService.approval_is_current(campaign):
            execution.status = "needs_attention"
            execution.lease_token = None
            execution.lease_expires_at = None
            execution.available_at = None
            execution.completed_at = None
            execution.failure_code = "campaign_approval_revoked"
            execution.failure_message = (
                "Campaign approval is no longer current; lifecycle reconciliation stopped."
            )
            execution.updated_at = now
            cls._audit(
                session,
                context=context,
                action="campaign_execution_needs_attention",
                resource_type="recruitment_campaign_execution",
                resource_id=execution.id,
            )
            return
        lifecycle_changed = cls._reconcile_application_steps(
            session, execution, campaign, context, now
        )
        objective = RecruitmentCampaignService._objective(campaign)
        outcomes = list(
            session.execute(
                select(RecruitmentCampaignHiringOutcomeRecord).where(
                    RecruitmentCampaignHiringOutcomeRecord.organization_id
                    == campaign.organization_id,
                    RecruitmentCampaignHiringOutcomeRecord.campaign_id == campaign.id,
                )
            ).scalars()
        )
        qualifying_hires = RecruitmentCampaignService._qualifying_hire_count(
            objective, campaign, outcomes
        )
        objective_status = RecruitmentCampaignService._objective_progress_status(
            objective, campaign, qualifying_hires, now
        )
        previous_status = execution.status
        if objective_status == "target_achieved":
            execution.status = "completed"
            execution.completed_at = now
            execution.failure_code = None
            execution.failure_message = None
            if previous_status != "completed":
                cls._audit(
                    session,
                    context=context,
                    action="campaign_hiring_target_achieved",
                    resource_type="recruitment_campaign_execution",
                    resource_id=execution.id,
                )
        elif objective_status == "deadline_passed":
            execution.status = "needs_attention"
            execution.completed_at = None
            execution.failure_code = "campaign_deadline_passed"
            execution.failure_message = (
                "The campaign deadline passed before the approved hiring target "
                "was achieved."
            )
            cls._audit(
                session,
                context=context,
                action="campaign_execution_needs_attention",
                resource_type="recruitment_campaign_execution",
                resource_id=execution.id,
            )
        else:
            execution.status = "awaiting_progress"
            execution.completed_at = None
            execution.failure_code = None
            execution.failure_message = None
            execution.available_at = now + timedelta(
                seconds=max(settings.CAMPAIGN_WORKER_POLL_SECONDS, 1)
            )
            if lifecycle_changed:
                cls._audit(
                    session,
                    context=context,
                    action="campaign_execution_awaiting_progress",
                    resource_type="recruitment_campaign_execution",
                    resource_id=execution.id,
                )
        execution.lease_token = None
        execution.lease_expires_at = None
        if execution.status != "awaiting_progress":
            execution.available_at = None
        execution.updated_at = now

    @classmethod
    def _reconcile_application_steps(
        cls,
        session,
        execution: RecruitmentCampaignExecutionRecord,
        campaign: RecruitmentCampaignRecord,
        context: TenantContext,
        now: datetime,
    ) -> bool:
        pool_step = session.execute(
            select(RecruitmentCampaignExecutionStepRecord).where(
                RecruitmentCampaignExecutionStepRecord.execution_id == execution.id,
                RecruitmentCampaignExecutionStepRecord.step_key == "candidate_pool",
            )
        ).scalar_one_or_none()
        cursor = None
        if pool_step is not None and pool_step.output_json:
            cursor = json.loads(pool_step.output_json).get("reconciliation_cursor")
        application_query = select(RecruitmentApplicationRecord).where(
            RecruitmentApplicationRecord.organization_id == campaign.organization_id,
            RecruitmentApplicationRecord.campaign_id == campaign.id,
        )
        if cursor:
            application_query = application_query.where(
                RecruitmentApplicationRecord.id > cursor
            )
        applications = list(
            session.execute(
                application_query.order_by(RecruitmentApplicationRecord.id).limit(100)
            ).scalars()
        )
        if not applications and cursor:
            applications = list(
                session.execute(
                    select(RecruitmentApplicationRecord)
                    .where(
                        RecruitmentApplicationRecord.organization_id
                        == campaign.organization_id,
                        RecruitmentApplicationRecord.campaign_id == campaign.id,
                    )
                    .order_by(RecruitmentApplicationRecord.id)
                    .limit(100)
                ).scalars()
            )
        if not applications:
            return cls._save_lifecycle_step(
                session,
                execution,
                "candidate_pool",
                {
                    "stage": "awaiting_candidate_intake",
                    "next_action": "submit_candidate_application",
                    "application_count": 0,
                },
                "pending",
                context,
                now,
            )

        next_cursor = (
            applications[-1].id if len(applications) == 100 else None
        )
        changed = cls._save_lifecycle_step(
            session,
            execution,
            "candidate_pool",
            {
                "stage": "candidate_pool_received",
                "next_action": "reconcile_candidate_batch",
                "batch_size": len(applications),
                "reconciliation_cursor": next_cursor,
            },
            "completed",
            context,
            now,
        )
        policy_state, policy = cls._automation_policy(campaign)
        application_ids = [item.id for item in applications]
        grouped: dict[type, dict[str, list[Any]]] = {}
        for model in (
            CandidateCommunicationRecord,
            CandidateAvailabilityRequestRecord,
            InterviewScheduleRecord,
            InterviewRecord,
            RecruitmentCampaignHiringOutcomeRecord,
        ):
            rows = session.execute(
                select(model)
                .where(
                    model.organization_id == campaign.organization_id,
                    model.application_id.in_(application_ids),
                )
                .order_by(model.id)
            ).scalars()
            grouped[model] = {}
            for row in rows:
                grouped[model].setdefault(row.application_id, []).append(row)

        for application in applications:
            communications = grouped[CandidateCommunicationRecord].get(
                application.id, []
            )
            availability = grouped[CandidateAvailabilityRequestRecord].get(
                application.id, []
            )
            schedules = grouped[InterviewScheduleRecord].get(application.id, [])
            interviews = grouped[InterviewRecord].get(application.id, [])
            outcomes = grouped[RecruitmentCampaignHiringOutcomeRecord].get(
                application.id, []
            )
            communication = (
                max(communications, key=lambda item: item.updated_at)
                if communications
                else None
            )
            request = (
                max(availability, key=lambda item: item.updated_at)
                if availability
                else None
            )
            schedule = max(
                schedules,
                key=lambda item: (
                    item.status == "confirmed_internal",
                    item.updated_at,
                ),
                default=None,
            )
            interview = (
                max(interviews, key=lambda item: item.created_at)
                if interviews
                else None
            )
            outcome = outcomes[-1] if outcomes else None
            application_policy_state = (
                cls._availability_policy_state(policy_state, policy, now)
                if request is None
                else policy_state
            )
            stage, next_action = cls._application_stage(
                application,
                communication,
                request,
                schedule,
                interview,
                outcome,
                scheduling_policy_state=application_policy_state,
            )

            handoffs = {
                "application_id": application.id,
                "waiting_on": cls._waiting_on(stage),
                "blocking_reason": cls._blocking_reason(
                    stage, application_policy_state
                ),
                "communication_id": None if communication is None else communication.id,
                "communication_status": (
                    None if communication is None else communication.status
                ),
                "delivery_status": (
                    None if communication is None else communication.delivery_status
                ),
                "availability_request_id": None if request is None else request.id,
                "schedule_id": None if schedule is None else schedule.id,
                "schedule_status": None if schedule is None else schedule.status,
                "interview_id": None if interview is None else interview.id,
                "hiring_outcome_id": None if outcome is None else outcome.id,
                "hiring_outcome": None if outcome is None else outcome.outcome,
                "criteria_satisfied": (
                    None if outcome is None else outcome.criteria_satisfied
                ),
            }
            step_status = (
                "completed"
                if outcome is not None
                else "retryable_failure"
                if stage == "retryable_failure"
                else "needs_attention"
                if stage == "needs_attention"
                else "pending"
            )
            changed = cls._save_lifecycle_step(
                session,
                execution,
                f"application:{application.id}",
                {"stage": stage, "next_action": next_action, **handoffs},
                step_status,
                context,
                now,
            ) or changed
        return changed

    @classmethod
    def _application_stage(
        cls,
        application: RecruitmentApplicationRecord,
        communication: CandidateCommunicationRecord | None,
        request: CandidateAvailabilityRequestRecord | None,
        schedule: InterviewScheduleRecord | None,
        interview: InterviewRecord | None,
        outcome: RecruitmentCampaignHiringOutcomeRecord | None,
        *,
        scheduling_policy_state: str = "missing",
    ) -> tuple[str, str | None]:
        if outcome is not None:
            return outcome.outcome, None
        if application.status == "retryable_failure":
            return "retryable_failure", "retry_screening"
        if application.status in {"needs_attention", "failed"}:
            return "needs_attention", "review_screening_failure"
        if application.status != "needs_review":
            return "screening_in_progress", "await_screening_result"
        if application.review_status != "reviewed":
            return "awaiting_screening_review", "review_screening"
        if communication is not None and communication.status == "cancelled":
            return "needs_attention", "resolve_cancelled_communication"
        if communication is None:
            return "awaiting_communication", "create_communication_draft"
        if communication.status in {"draft", "awaiting_approval", "approved", "sending"}:
            return "communication_in_progress", "approve_or_dispatch_communication"
        if communication.status in {"retryable_failure", "needs_attention"}:
            return communication.status, "resolve_communication"
        return cls._after_communication_stage(
            request,
            schedule,
            interview,
            scheduling_policy_state=scheduling_policy_state,
        )

    @classmethod
    def _after_communication_stage(
        cls,
        request: CandidateAvailabilityRequestRecord | None,
        schedule: InterviewScheduleRecord | None,
        interview: InterviewRecord | None,
        *,
        scheduling_policy_state: str,
    ) -> tuple[str, str | None]:
        if request is None:
            if scheduling_policy_state == "missing":
                return "awaiting_scheduling_policy", "approve_scheduling_policy"
            if scheduling_policy_state in {"invalid", "expired"}:
                return "needs_attention", "resolve_scheduling_policy"
            return "awaiting_availability_request", "request_availability"
        if request.status == "pending_response":
            return "awaiting_candidate_availability", "await_candidate_response"
        if request.status == "responded":
            return "awaiting_availability_review", "review_availability"
        if request.status in {"expired", "cancelled", "needs_attention"}:
            return "needs_attention", "resolve_availability"
        if request.status != "reviewed":
            return "availability_in_progress", "review_availability"
        return cls._after_availability_stage(
            schedule,
            interview,
            scheduling_policy_state=scheduling_policy_state,
        )

    @classmethod
    def _after_availability_stage(
        cls,
        schedule: InterviewScheduleRecord | None,
        interview: InterviewRecord | None,
        *,
        scheduling_policy_state: str,
    ) -> tuple[str, str | None]:
        if schedule is not None and schedule.status in {"cancelled", "expired"}:
            return "needs_attention", "resolve_schedule"
        if schedule is None:
            if scheduling_policy_state == "missing":
                return "awaiting_scheduling_policy", "approve_scheduling_policy"
            if scheduling_policy_state in {"invalid", "expired"}:
                return "needs_attention", "resolve_scheduling_policy"
            return "awaiting_schedule_proposal", "propose_internal_schedule"
        if schedule.status in {"proposed", "pending_confirmation", "reschedule_pending"}:
            return "awaiting_schedule_confirmation", "confirm_internal_schedule"
        if schedule.status == "needs_attention":
            return "needs_attention", "resolve_schedule"
        if interview is None:
            return "awaiting_interview", "candidate_start_interview"
        interview_status = json.loads(interview.state_json).get("status")
        if interview_status == "completed":
            return "awaiting_hiring_decision", "record_hiring_outcome"
        if interview_status == "active":
            return "interview_in_progress", "candidate_continue_interview"
        return "needs_attention", "review_interview_state"

    @staticmethod
    def _waiting_on(stage: str) -> str | None:
        if stage in {
            "awaiting_candidate_availability",
            "awaiting_schedule_confirmation",
            "awaiting_interview",
            "interview_in_progress",
        }:
            return "candidate"
        if stage in {
            "awaiting_screening_review",
            "awaiting_communication",
            "communication_in_progress",
            "awaiting_availability_review",
            "awaiting_hiring_decision",
        }:
            return "recruiter_or_authorized_reviewer"
        if stage == "awaiting_scheduling_policy":
            return "campaign_owner_or_authorized_admin"
        if stage in {"needs_attention", "retryable_failure"}:
            return "authorized_operator"
        return None

    @staticmethod
    def _blocking_reason(stage: str, policy_state: str) -> str | None:
        if stage == "awaiting_scheduling_policy":
            return "approved_explicit_scheduling_policy_required"
        if stage == "needs_attention" and policy_state in {"invalid", "expired"}:
            return f"approved_scheduling_policy_{policy_state}"
        return None

    @classmethod
    def _save_lifecycle_step(
        cls,
        session,
        execution: RecruitmentCampaignExecutionRecord,
        step_key: str,
        output: dict[str, Any],
        status_value: str,
        context: TenantContext,
        now: datetime,
    ) -> bool:
        output_json = json.dumps(output, sort_keys=True, separators=(",", ":"))
        step = session.execute(
            select(RecruitmentCampaignExecutionStepRecord).where(
                RecruitmentCampaignExecutionStepRecord.execution_id == execution.id,
                RecruitmentCampaignExecutionStepRecord.step_key == step_key,
            )
        ).scalar_one_or_none()
        changed = step is None or (
            step.output_json != output_json or step.status != status_value
        )
        if step is None:
            step = RecruitmentCampaignExecutionStepRecord(
                execution_id=execution.id,
                step_key=step_key,
                status=status_value,
                attempt_count=0,
                input_json="{}",
                output_json=output_json,
                created_at=now,
                updated_at=now,
            )
            session.add(step)
        else:
            step.status = status_value
            step.output_json = output_json
            step.updated_at = now
        step.completed_at = now if status_value == "completed" else None
        if changed:
            cls._audit(
                session,
                context=context,
                action="campaign_application_stage_changed",
                resource_type="recruitment_campaign_execution_step",
                resource_id=step_key,
            )
        step.completed_at = now if status_value == "completed" else None
        return changed

    @classmethod
    def _fail(
        cls,
        execution_id: str,
        step_id: str,
        lease_token: str,
        context: TenantContext,
        failure_code: str,
        failure_message: str,
        *,
        retryable: bool,
    ) -> CampaignExecutionResponse:
        with SessionLocal.begin() as session:
            execution = session.get(RecruitmentCampaignExecutionRecord, execution_id)
            step = session.get(RecruitmentCampaignExecutionStepRecord, step_id)
            if execution.lease_token != lease_token:
                return cls._serialize(session, execution)
            exhausted = execution.attempt_count >= execution.max_attempts
            final_status = (
                "retryable_failure"
                if retryable and not exhausted
                else "needs_attention"
            )
            now = datetime.now(UTC)
            retry_delay = min(
                settings.CAMPAIGN_WORKER_RETRY_BASE_SECONDS
                * (2 ** max(0, execution.attempt_count - 1)),
                settings.CAMPAIGN_WORKER_RETRY_MAX_SECONDS,
            )
            step.status = final_status
            step.failure_code = failure_code
            step.failure_message = failure_message
            step.updated_at = now
            execution.status = final_status
            execution.failure_code = failure_code
            execution.failure_message = failure_message
            execution.lease_token = None
            execution.lease_expires_at = None
            execution.available_at = (
                now + timedelta(seconds=retry_delay)
                if final_status == "retryable_failure"
                else None
            )
            execution.updated_at = now
            cls._audit(
                session,
                context=context,
                action=f"campaign_execution_{final_status}",
                resource_type="recruitment_campaign_execution",
                resource_id=execution.id,
            )
            logger.warning(
                "Recruitment campaign execution step failed",
                extra={
                    "execution_id": execution.id,
                    "step_key": step.step_key,
                    "failure_code": failure_code,
                    "retryable": retryable and not exhausted,
                },
            )
            return cls._serialize(session, execution)

    @staticmethod
    def _serialize(
        session, execution: RecruitmentCampaignExecutionRecord
    ) -> CampaignExecutionResponse:
        steps = session.execute(
            select(RecruitmentCampaignExecutionStepRecord)
            .where(
                RecruitmentCampaignExecutionStepRecord.execution_id == execution.id
            )
            .order_by(
                case(
                    (
                        RecruitmentCampaignExecutionStepRecord.step_key
                        == "job_design",
                        0,
                    ),
                    else_=1,
                ),
                RecruitmentCampaignExecutionStepRecord.created_at,
                RecruitmentCampaignExecutionStepRecord.step_key,
            )
        ).scalars()
        return CampaignExecutionResponse(
            id=execution.id,
            campaign_id=execution.campaign_id,
            organization_id=execution.organization_id,
            requested_by_user_id=execution.requested_by_user_id,
            status=execution.status,
            attempt_count=execution.attempt_count,
            max_attempts=execution.max_attempts,
            failure_code=execution.failure_code,
            failure_message=execution.failure_message,
            available_at=execution.available_at,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
            completed_at=execution.completed_at,
            steps=[
                ExecutionStepResponse(
                    step_key=step.step_key,
                    status=step.status,
                    attempt_count=step.attempt_count,
                    output=(
                        json.loads(step.output_json)
                        if step.output_json is not None
                        else None
                    ),
                    output_source=step.output_source,
                    failure_code=step.failure_code,
                    failure_message=step.failure_message,
                    started_at=step.started_at,
                    completed_at=step.completed_at,
                )
                for step in steps
            ],
        )
