from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from services.candidate_screening import IncompatibleRubricError, assess_candidate_cv
from services.database import (
    AccessAuditRecord,
    CandidateAvailabilityRequestRecord,
    CandidateCommunicationRecord,
    InterviewScheduleRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignApprovalRecord,
    RecruitmentCampaignExecutionRecord,
    RecruitmentCampaignExecutionStepRecord,
    RecruitmentCampaignHiringOutcomeRecord,
    RecruitmentCampaignRecord,
    SessionLocal,
)
from services.tenant_authorization import TenantContext

WORKFLOW_STATES = (
    "brief_created",
    "requirements_defined",
    "job_description_ready",
    "rubric_ready",
    "application_form_ready",
    "pending_approval",
    "approved",
    "rejected",
)
APPROVAL_STATES = ("draft", "pending_review", "approved", "rejected")

WORKFLOW_TRANSITIONS = {
    "brief_created": {"requirements_defined"},
    "requirements_defined": {"job_description_ready"},
    "job_description_ready": {"rubric_ready"},
    "rubric_ready": {"application_form_ready"},
    "application_form_ready": {"pending_approval"},
    "pending_approval": {"approved", "rejected"},
    "approved": set(),
    "rejected": set(),
}
APPROVAL_TRANSITIONS = {
    "draft": {"pending_review"},
    "pending_review": {"approved", "rejected"},
    "approved": set(),
    "rejected": set(),
}


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    director_hiring_brief: str = Field(min_length=10, max_length=20000)
    structured_requirements: dict[str, Any] = Field(default_factory=dict)
    job_description: str | None = None
    competency_rubric: list[dict[str, Any]] = Field(default_factory=list)
    application_form_schema: dict[str, Any] = Field(default_factory=dict)
    objective: CampaignObjective | None = None


class CampaignUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    director_hiring_brief: str | None = None
    structured_requirements: dict[str, Any] | None = None
    job_description: str | None = None
    competency_rubric: list[dict[str, Any]] | None = None
    application_form_schema: dict[str, Any] | None = None
    approval_reason: str | None = None


class CampaignClarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=5, max_length=1000)
    answer: str | None = Field(default=None, max_length=2000)

    @field_validator("question", "answer")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Clarification text cannot be blank")
        return cleaned


class CampaignObjective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    requested_role: str | None = Field(default=None, min_length=1, max_length=256)
    seniority: str | None = Field(default=None, min_length=1, max_length=128)
    target_headcount: int | None = Field(default=None, ge=1, le=500)
    deadline: date | None = Field(
        default=None,
        description="Inclusive final calendar date in timezone_name.",
    )
    timezone_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="IANA timezone used to interpret the deadline date.",
    )
    required_qualifications: list[str] | None = None
    success_criteria: list[str] | None = None
    clarifications: list[CampaignClarification] = Field(default_factory=list)

    @field_validator("requested_role", "seniority", "timezone_name")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value cannot be blank")
        return normalized

    @field_validator("timezone_name")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError:
                raise PydanticCustomError(
                    "invalid_timezone",
                    "timezone_name must be a valid IANA timezone",
                ) from None
        return value

    @field_validator("required_qualifications", "success_criteria")
    @classmethod
    def normalize_criteria(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("Criteria cannot contain blank entries")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Criteria entries must be unique")
        if len(normalized) > 100 or any(len(value) > 1000 for value in normalized):
            raise ValueError("Criteria exceed supported limits")
        return normalized

    @field_validator("clarifications")
    @classmethod
    def clarification_ids_are_unique(
        cls, values: list[CampaignClarification]
    ) -> list[CampaignClarification]:
        identifiers = [item.id for item in values]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Clarification identifiers must be unique")
        return values

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        for field_name, value in (
            ("requested_role", self.requested_role),
            ("seniority", self.seniority),
            ("target_headcount", self.target_headcount),
            ("deadline", self.deadline),
            ("timezone_name", self.timezone_name),
            ("required_qualifications", self.required_qualifications),
            ("success_criteria", self.success_criteria),
        ):
            if value is None or (field_name == "success_criteria" and not value):
                missing.append(field_name)
        return missing

    def unresolved_questions(self) -> list[str]:
        questions = [
            clarification.question
            for clarification in self.clarifications
            if clarification.answer is None
        ]
        questions.extend(
            f"Clarify {field_name.replace('_', ' ')}."
            for field_name in self.missing_fields()
        )
        return questions

    def is_complete(self, screening_criteria: list[dict[str, Any]]) -> bool:
        if self.unresolved_questions():
            return False
        try:
            assess_candidate_cv(screening_criteria, "")
        except IncompatibleRubricError:
            return False
        return True


class CampaignObjectiveUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    objective: CampaignObjective


class CampaignClarificationResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    answer: str = Field(min_length=1, max_length=2000)

    @field_validator("answer")
    @classmethod
    def answer_not_blank(cls, value: str) -> str:
        answer = value.strip()
        if not answer:
            raise ValueError("answer cannot be blank")
        return answer


CampaignCreate.model_rebuild()


class CampaignHiringOutcomeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str = Field(pattern=r"^(hired|rejected|withdrawn)$")
    criteria_satisfied: bool = False
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    reason: str = Field(min_length=10, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("reason cannot be blank")
        return reason


class CampaignHiringOutcomeResponse(BaseModel):
    id: str
    campaign_id: str
    application_id: str
    outcome: str
    criteria_satisfied: bool
    objective_version: int
    plan_version: int
    decided_by_user_id: str
    decided_at: datetime
    reason: str


class CampaignWorkCounts(BaseModel):
    pending: int = 0
    in_progress: int = 0
    awaiting_human_approval: int = 0
    completed: int = 0
    rejected_or_withdrawn: int = 0
    retryable_failures: int = 0
    attention_needed: int = 0


class CampaignApplicationWorkflow(BaseModel):
    application_id: str
    stage: str
    next_action: str | None
    work_status: str
    handoffs: dict[str, Any]


class CampaignProgressResponse(BaseModel):
    campaign_id: str
    objective_status: str
    objective_version: int
    plan_version: int
    target_headcount: int | None
    hired_outcomes: int
    qualifying_hires: int
    rejected_outcomes: int
    withdrawn_outcomes: int
    remaining_hires: int | None
    work_counts: CampaignWorkCounts
    unresolved_questions: list[str]
    application_workflows: list[CampaignApplicationWorkflow] = Field(
        default_factory=list
    )


class CampaignResponse(BaseModel):
    id: str
    organization_id: str
    owner_user_id: str
    director_hiring_brief: str
    structured_requirements: dict[str, Any]
    job_description: str | None = None
    competency_rubric: list[dict[str, Any]]
    application_form_schema: dict[str, Any]
    objective: CampaignObjective
    objective_version: int
    plan_version: int
    approved_objective_version: int | None
    approved_plan_version: int | None
    unresolved_questions: list[str]
    objective_complete: bool
    approval_state: str
    workflow_state: str
    approval_reason: str | None = None
    approval_decided_by_user_id: str | None = None
    approval_decided_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RecruitmentCampaignService:
    @staticmethod
    def _audit_approval(
        session,
        campaign: RecruitmentCampaignRecord,
        context: TenantContext,
        decision: str,
    ) -> None:
        session.add(
            AccessAuditRecord(
                actor_user_id=context.actor_user_id,
                organization_id=campaign.organization_id,
                requested_organization_id=campaign.organization_id,
                action=f"campaign_{decision}",
                resource_type="recruitment_campaign",
                resource_id=campaign.id,
                outcome="allowed",
                support_reason=context.support_reason,
            )
        )

    @staticmethod
    def create_campaign(
        organization_id: str,
        owner_user_id: str,
        payload: CampaignCreate,
    ) -> RecruitmentCampaignRecord:
        with SessionLocal.begin() as session:
            record = RecruitmentCampaignRecord(
                organization_id=organization_id,
                owner_user_id=owner_user_id,
                director_hiring_brief=payload.director_hiring_brief,
                structured_requirements_json=json.dumps(payload.structured_requirements or {}),
                job_description=payload.job_description,
                competency_rubric_json=json.dumps(payload.competency_rubric or []),
                application_form_schema_json=json.dumps(payload.application_form_schema or {}),
                objective_json=(payload.objective or CampaignObjective()).model_dump_json(),
                objective_version=1,
                plan_version=1,
                approval_state="draft",
                workflow_state="brief_created",
            )
            session.add(record)
            session.flush()
            return record

    @staticmethod
    def _load_campaign(session, campaign_id: str, organization_id: str | None = None):
        query = select(RecruitmentCampaignRecord).where(RecruitmentCampaignRecord.id == campaign_id)
        if organization_id is not None:
            query = query.where(RecruitmentCampaignRecord.organization_id == organization_id)
        return session.execute(query).scalar_one_or_none()

    @staticmethod
    def _normalize_approval_reason(reason: str | None) -> str | None:
        if reason is None:
            return None
        cleaned = reason.strip()
        return cleaned or None

    @staticmethod
    def _objective(campaign: RecruitmentCampaignRecord) -> CampaignObjective:
        return CampaignObjective.model_validate_json(campaign.objective_json or "{}")

    @classmethod
    def _objective_complete(cls, campaign: RecruitmentCampaignRecord) -> bool:
        return cls._objective(campaign).is_complete(
            json.loads(campaign.competency_rubric_json or "[]")
        )

    @staticmethod
    def approval_is_current(campaign: RecruitmentCampaignRecord) -> bool:
        return (
            campaign.approval_state == "approved"
            and campaign.approved_objective_version == campaign.objective_version
            and campaign.approved_plan_version == campaign.plan_version
        )

    @staticmethod
    def _bump_plan_version(campaign: RecruitmentCampaignRecord) -> None:
        campaign.plan_version += 1

    @staticmethod
    def _record_approval_decision(
        session,
        campaign: RecruitmentCampaignRecord,
        context: TenantContext,
        decision: str,
        reason: str | None,
        decided_at: datetime,
    ) -> None:
        session.add(
            RecruitmentCampaignApprovalRecord(
                id=str(uuid4()),
                organization_id=campaign.organization_id,
                campaign_id=campaign.id,
                objective_version=campaign.objective_version,
                plan_version=campaign.plan_version,
                decision=decision,
                decided_by_user_id=context.actor_user_id,
                decided_at=decided_at,
                reason=reason,
            )
        )

    @staticmethod
    def _assert_authorized(
        campaign: RecruitmentCampaignRecord,
        context: TenantContext,
        *,
        allow_owner_only: bool = False,
    ) -> None:
        if context.is_platform_admin:
            return
        if context.role == "org_admin" and campaign.organization_id == context.organization_id:
            return
        if context.role == "recruiter" and campaign.organization_id == context.organization_id:
            if allow_owner_only:
                if campaign.owner_user_id != context.actor_user_id:
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Campaign access denied")
            return
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")

    @staticmethod
    def get_campaign(campaign_id: str, context: TenantContext) -> RecruitmentCampaignRecord:
        with SessionLocal() as session:
            campaign = RecruitmentCampaignService._load_campaign(
                session,
                campaign_id,
                organization_id=context.organization_id,
            )
            if campaign is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            return campaign

    @staticmethod
    def list_campaigns(context: TenantContext) -> list[RecruitmentCampaignRecord]:
        with SessionLocal() as session:
            query = select(RecruitmentCampaignRecord).where(
                RecruitmentCampaignRecord.organization_id == context.organization_id,
            )
            if context.role == "recruiter":
                query = query.where(RecruitmentCampaignRecord.owner_user_id == context.actor_user_id)
            campaigns = session.execute(query.order_by(RecruitmentCampaignRecord.created_at.desc())).scalars().all()
            return list(campaigns)

    @staticmethod
    def update_campaign(
        campaign_id: str,
        context: TenantContext,
        payload: CampaignUpdate,
    ) -> RecruitmentCampaignRecord:
        with SessionLocal.begin() as session:
            campaign = RecruitmentCampaignService._load_campaign(
                session,
                campaign_id,
                organization_id=context.organization_id,
            )
            if campaign is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            if campaign.approval_state != "draft":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Campaign plans cannot be edited after they are submitted for approval",
                )

            changed = False
            if payload.director_hiring_brief is not None:
                changed = changed or campaign.director_hiring_brief != payload.director_hiring_brief
                campaign.director_hiring_brief = payload.director_hiring_brief
            if payload.structured_requirements is not None:
                changed = changed or campaign.structured_requirements_json != json.dumps(payload.structured_requirements)
                campaign.structured_requirements_json = json.dumps(payload.structured_requirements)
            if payload.job_description is not None:
                changed = changed or campaign.job_description != payload.job_description
                campaign.job_description = payload.job_description
            if payload.competency_rubric is not None:
                changed = changed or campaign.competency_rubric_json != json.dumps(payload.competency_rubric)
                campaign.competency_rubric_json = json.dumps(payload.competency_rubric)
            if payload.application_form_schema is not None:
                changed = changed or campaign.application_form_schema_json != json.dumps(payload.application_form_schema)
                campaign.application_form_schema_json = json.dumps(payload.application_form_schema)
            if payload.approval_reason is not None:
                campaign.approval_reason = RecruitmentCampaignService._normalize_approval_reason(payload.approval_reason)
            if changed:
                RecruitmentCampaignService._bump_plan_version(campaign)
            campaign.updated_at = datetime.now(UTC)
            return campaign

    @classmethod
    def update_objective(
        cls,
        campaign_id: str,
        context: TenantContext,
        payload: CampaignObjectiveUpdate,
        *,
        audit_action: str = "campaign_objective_updated",
    ) -> RecruitmentCampaignRecord:
        objective_json = payload.objective.model_dump_json()
        request_hash = hashlib.sha256(objective_json.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            campaign = cls._load_campaign(
                session, campaign_id, organization_id=context.organization_id
            )
            if campaign is None:
                raise HTTPException(status_code=404, detail="Campaign not found")
            cls._assert_authorized(campaign, context)
            if (
                campaign.objective_update_idempotency_key == payload.idempotency_key
            ):
                if campaign.objective_update_request_hash != request_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was used for a different objective update",
                    )
                return campaign
            if campaign.objective_version != payload.expected_version:
                raise HTTPException(
                    status_code=409,
                    detail="Campaign objective version is stale",
                )
            if campaign.approval_state == "pending_review":
                raise HTTPException(
                    status_code=409,
                    detail="Withdraw the pending approval request before changing the objective",
                )

            was_approved = campaign.approval_state == "approved"
            values: dict[str, Any] = {
                "objective_json": objective_json,
                "objective_version": campaign.objective_version + 1,
                "objective_update_idempotency_key": payload.idempotency_key,
                "objective_update_request_hash": request_hash,
                "updated_at": now,
            }
            if was_approved or campaign.approval_state == "rejected":
                values.update(
                    approval_state="draft",
                    workflow_state="brief_created",
                    approval_reason=None,
                    approval_decided_by_user_id=None,
                    approval_decided_at=None,
                    approved_objective_version=None,
                    approved_plan_version=None,
                )
            changed = session.execute(
                update(RecruitmentCampaignRecord)
                .where(
                    RecruitmentCampaignRecord.id == campaign_id,
                    RecruitmentCampaignRecord.organization_id
                    == context.organization_id,
                    RecruitmentCampaignRecord.objective_version
                    == payload.expected_version,
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Campaign objective changed concurrently",
                )
            if was_approved:
                session.execute(
                    update(RecruitmentCampaignExecutionRecord)
                    .where(
                        RecruitmentCampaignExecutionRecord.campaign_id
                        == campaign_id,
                        RecruitmentCampaignExecutionRecord.organization_id
                        == context.organization_id,
                        RecruitmentCampaignExecutionRecord.status.in_(
                            ("pending", "running", "retryable_failure", "waiting_for_approval")
                        ),
                    )
                    .values(
                        status="needs_attention",
                        failure_code="objective_changed_requires_reapproval",
                        failure_message=(
                            "The campaign objective changed; review and approve the "
                            "updated plan before execution."
                        ),
                        lease_token=None,
                        lease_expires_at=None,
                        available_at=None,
                        updated_at=now,
                    )
                )
            session.add(
                AccessAuditRecord(
                    actor_user_id=context.actor_user_id,
                    organization_id=campaign.organization_id,
                    requested_organization_id=campaign.organization_id,
                    action=audit_action,
                    resource_type="recruitment_campaign",
                    resource_id=campaign.id,
                    outcome="allowed",
                    support_reason=context.support_reason,
                )
            )
            session.flush()
            session.refresh(campaign)
            return campaign

    @classmethod
    def resolve_clarification(
        cls,
        campaign_id: str,
        clarification_id: str,
        context: TenantContext,
        payload: CampaignClarificationResolution,
    ) -> RecruitmentCampaignRecord:
        campaign = cls.get_campaign(campaign_id, context)
        if campaign.objective_update_idempotency_key == payload.idempotency_key:
            current_objective = cls._objective(campaign)
            resolved = next(
                (
                    item
                    for item in current_objective.clarifications
                    if item.id == clarification_id
                ),
                None,
            )
            current_hash = hashlib.sha256(
                current_objective.model_dump_json().encode("utf-8")
            ).hexdigest()
            if (
                resolved is not None
                and resolved.answer == payload.answer
                and campaign.objective_update_request_hash == current_hash
            ):
                return campaign
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used for a different clarification update",
            )
        if campaign.objective_version != payload.expected_version:
            raise HTTPException(status_code=409, detail="Campaign objective version is stale")
        objective = cls._objective(campaign)
        clarification = next(
            (item for item in objective.clarifications if item.id == clarification_id),
            None,
        )
        if clarification is None:
            raise HTTPException(status_code=404, detail="Clarification not found")
        if clarification.answer is not None:
            if clarification.answer == payload.answer:
                return campaign
            raise HTTPException(status_code=409, detail="Clarification is already resolved")
        objective.clarifications = [
            item.model_copy(update={"answer": payload.answer})
            if item.id == clarification_id
            else item
            for item in objective.clarifications
        ]
        return cls.update_objective(
            campaign_id,
            context,
            CampaignObjectiveUpdate(
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                objective=objective,
            ),
            audit_action="campaign_objective_clarification_resolved",
        )

    @classmethod
    def get_objective(
        cls, campaign_id: str, context: TenantContext
    ) -> tuple[CampaignObjective, int, list[str]]:
        campaign = cls.get_campaign(campaign_id, context)
        objective = cls._objective(campaign)
        return objective, campaign.objective_version, objective.unresolved_questions()

    @staticmethod
    def set_requirements(
        campaign_id: str,
        context: TenantContext,
        structured_requirements: dict[str, Any],
    ) -> RecruitmentCampaignRecord:
        with SessionLocal.begin() as session:
            campaign = RecruitmentCampaignService._load_campaign(
                session,
                campaign_id,
                organization_id=context.organization_id,
            )
            if campaign is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            if campaign.workflow_state != "brief_created":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Requirements can only be set while the brief is open")
            serialized = json.dumps(structured_requirements)
            if campaign.structured_requirements_json != serialized:
                campaign.structured_requirements_json = serialized
                RecruitmentCampaignService._bump_plan_version(campaign)
            campaign.workflow_state = "requirements_defined"
            campaign.updated_at = datetime.now(UTC)
            return campaign

    @staticmethod
    def set_job_description(
        campaign_id: str,
        context: TenantContext,
        job_description: str,
    ) -> RecruitmentCampaignRecord:
        with SessionLocal.begin() as session:
            campaign = RecruitmentCampaignService._load_campaign(
                session,
                campaign_id,
                organization_id=context.organization_id,
            )
            if campaign is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            if campaign.workflow_state != "requirements_defined":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job description can only be added after requirements are defined")
            if campaign.job_description != job_description:
                campaign.job_description = job_description
                RecruitmentCampaignService._bump_plan_version(campaign)
            campaign.workflow_state = "job_description_ready"
            campaign.updated_at = datetime.now(UTC)
            return campaign

    @staticmethod
    def set_competency_rubric(
        campaign_id: str,
        context: TenantContext,
        competency_rubric: list[dict[str, Any]],
    ) -> RecruitmentCampaignRecord:
        with SessionLocal.begin() as session:
            campaign = RecruitmentCampaignService._load_campaign(
                session,
                campaign_id,
                organization_id=context.organization_id,
            )
            if campaign is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            if campaign.workflow_state != "job_description_ready":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Competency rubric can only be added after the job description is ready")
            serialized = json.dumps(competency_rubric)
            if campaign.competency_rubric_json != serialized:
                campaign.competency_rubric_json = serialized
                RecruitmentCampaignService._bump_plan_version(campaign)
            campaign.workflow_state = "rubric_ready"
            campaign.updated_at = datetime.now(UTC)
            return campaign

    @staticmethod
    def set_application_form_schema(
        campaign_id: str,
        context: TenantContext,
        application_form_schema: dict[str, Any],
    ) -> RecruitmentCampaignRecord:
        with SessionLocal.begin() as session:
            campaign = RecruitmentCampaignService._load_campaign(
                session,
                campaign_id,
                organization_id=context.organization_id,
            )
            if campaign is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            if campaign.workflow_state != "rubric_ready":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Application form schema can only be added after the rubric is ready")
            serialized = json.dumps(application_form_schema)
            if campaign.application_form_schema_json != serialized:
                campaign.application_form_schema_json = serialized
                RecruitmentCampaignService._bump_plan_version(campaign)
            campaign.workflow_state = "application_form_ready"
            campaign.updated_at = datetime.now(UTC)
            return campaign

    @staticmethod
    def submit_for_approval(
        campaign_id: str,
        context: TenantContext,
    ) -> RecruitmentCampaignRecord:
        with SessionLocal.begin() as session:
            campaign = RecruitmentCampaignService._load_campaign(
                session,
                campaign_id,
                organization_id=context.organization_id,
            )
            if campaign is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            if campaign.workflow_state != "application_form_ready":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Campaign must be ready for approval before submission")
            if not RecruitmentCampaignService._objective_complete(campaign):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": "Campaign objective and screening criteria are incomplete",
                        "unresolved_questions": RecruitmentCampaignService._objective(
                            campaign
                        ).unresolved_questions(),
                    },
                )
            campaign.approval_state = "pending_review"
            campaign.workflow_state = "pending_approval"
            campaign.approval_reason = None
            campaign.updated_at = datetime.now(UTC)
            RecruitmentCampaignService._audit_approval(
                session, campaign, context, "submitted_for_approval"
            )
            return campaign

    @staticmethod
    def approve(
        campaign_id: str,
        context: TenantContext,
        approval_reason: str | None = None,
    ) -> RecruitmentCampaignRecord:
        with SessionLocal.begin() as session:
            campaign = RecruitmentCampaignService._load_campaign(
                session,
                campaign_id,
                organization_id=context.organization_id,
            )
            if campaign is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            if context.role not in {"org_admin", "platform_admin"}:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only organization administrators can approve campaigns")
            if campaign.workflow_state != "pending_approval":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Campaign must be pending approval before it can be approved")
            if not RecruitmentCampaignService._objective_complete(campaign):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Campaign objective and screening criteria must be complete before approval",
                )
            campaign.approval_state = "approved"
            campaign.workflow_state = "approved"
            campaign.approval_reason = RecruitmentCampaignService._normalize_approval_reason(approval_reason)
            campaign.approval_decided_by_user_id = context.actor_user_id
            campaign.approval_decided_at = datetime.now(UTC)
            campaign.approved_objective_version = campaign.objective_version
            campaign.approved_plan_version = campaign.plan_version
            campaign.updated_at = campaign.approval_decided_at
            execution = session.execute(
                select(RecruitmentCampaignExecutionRecord).where(
                    RecruitmentCampaignExecutionRecord.campaign_id == campaign.id,
                    RecruitmentCampaignExecutionRecord.status == "waiting_for_approval",
                )
            ).scalar_one_or_none()
            if execution is not None:
                execution.status = "pending"
                execution.updated_at = campaign.approval_decided_at
            RecruitmentCampaignService._audit_approval(
                session, campaign, context, "approved"
            )
            RecruitmentCampaignService._record_approval_decision(
                session,
                campaign,
                context,
                "approved",
                campaign.approval_reason,
                campaign.approval_decided_at,
            )
            return campaign

    @staticmethod
    def reject(
        campaign_id: str,
        context: TenantContext,
        approval_reason: str | None = None,
    ) -> RecruitmentCampaignRecord:
        with SessionLocal.begin() as session:
            campaign = RecruitmentCampaignService._load_campaign(
                session,
                campaign_id,
                organization_id=context.organization_id,
            )
            if campaign is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
            RecruitmentCampaignService._assert_authorized(campaign, context)
            if context.role not in {"org_admin", "platform_admin"}:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only organization administrators can reject campaigns")
            if campaign.workflow_state != "pending_approval":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Campaign must be pending approval before it can be rejected")
            campaign.approval_state = "rejected"
            campaign.workflow_state = "rejected"
            campaign.approval_reason = RecruitmentCampaignService._normalize_approval_reason(approval_reason)
            campaign.approval_decided_by_user_id = context.actor_user_id
            campaign.approval_decided_at = datetime.now(UTC)
            campaign.approved_objective_version = None
            campaign.approved_plan_version = None
            campaign.updated_at = campaign.approval_decided_at
            execution = session.execute(
                select(RecruitmentCampaignExecutionRecord).where(
                    RecruitmentCampaignExecutionRecord.campaign_id == campaign.id,
                    RecruitmentCampaignExecutionRecord.status == "waiting_for_approval",
                )
            ).scalar_one_or_none()
            if execution is not None:
                execution.status = "needs_attention"
                execution.failure_code = "plan_rejected"
                execution.failure_message = (
                    "The generated plan was rejected; revise the campaign before starting "
                    "a new execution."
                )
                execution.updated_at = campaign.approval_decided_at
            RecruitmentCampaignService._audit_approval(
                session, campaign, context, "rejected"
            )
            RecruitmentCampaignService._record_approval_decision(
                session,
                campaign,
                context,
                "rejected",
                campaign.approval_reason,
                campaign.approval_decided_at,
            )
            return campaign

    @classmethod
    def record_hiring_outcome(
        cls,
        campaign_id: str,
        application_id: str,
        context: TenantContext,
        payload: CampaignHiringOutcomeRequest,
    ) -> CampaignHiringOutcomeResponse:
        if payload.outcome == "hired" and context.role not in {
            "org_admin",
            "platform_admin",
        }:
            raise HTTPException(
                status_code=403,
                detail="Only an organization administrator can confirm a hire",
            )
        cls._require_outcome_role(context)
        request_data = payload.model_dump(mode="json", exclude={"idempotency_key"})
        request_hash = hashlib.sha256(
            json.dumps(request_data, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        now = datetime.now(UTC)
        try:
            with SessionLocal.begin() as session:
                campaign = cls._load_campaign(
                    session, campaign_id, organization_id=context.organization_id
                )
                if campaign is None:
                    raise HTTPException(status_code=404, detail="Campaign not found")
                cls._assert_authorized(campaign, context)
                prior = session.execute(
                    select(RecruitmentCampaignHiringOutcomeRecord).where(
                        RecruitmentCampaignHiringOutcomeRecord.organization_id
                        == context.organization_id,
                        RecruitmentCampaignHiringOutcomeRecord.idempotency_key
                        == payload.idempotency_key,
                    )
                ).scalar_one_or_none()
                if prior is not None:
                    if (
                        prior.request_hash != request_hash
                        or prior.campaign_id != campaign_id
                        or prior.application_id != application_id
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="Idempotency key was used for a different hiring outcome",
                        )
                    return cls._serialize_outcome(prior)
                if not cls.approval_is_current(campaign) or not cls._objective_complete(
                    campaign
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="A current approved campaign objective is required",
                    )
                application = session.execute(
                    select(RecruitmentApplicationRecord).where(
                        RecruitmentApplicationRecord.id == application_id,
                        RecruitmentApplicationRecord.campaign_id == campaign_id,
                        RecruitmentApplicationRecord.organization_id
                        == context.organization_id,
                    )
                ).scalar_one_or_none()
                if application is None:
                    raise HTTPException(status_code=404, detail="Application not found")
                if application.review_status != "reviewed":
                    raise HTTPException(
                        status_code=409,
                        detail="Application screening must be reviewed before recording an outcome",
                    )
                record = RecruitmentCampaignHiringOutcomeRecord(
                    id=str(uuid4()),
                    organization_id=campaign.organization_id,
                    campaign_id=campaign.id,
                    application_id=application.id,
                    objective_version=campaign.objective_version,
                    plan_version=campaign.plan_version,
                    outcome=payload.outcome,
                    criteria_satisfied=payload.criteria_satisfied,
                    idempotency_key=payload.idempotency_key,
                    request_hash=request_hash,
                    reason=payload.reason,
                    decided_by_user_id=context.actor_user_id,
                    decided_at=now,
                )
                session.add(record)
                session.flush()
                session.add(
                    AccessAuditRecord(
                        actor_user_id=context.actor_user_id,
                        organization_id=campaign.organization_id,
                        requested_organization_id=campaign.organization_id,
                        action=f"campaign_candidate_{payload.outcome}",
                        resource_type="recruitment_application",
                        resource_id=application.id,
                        outcome="allowed",
                        support_reason=context.support_reason,
                    )
                )
                cls._update_application_outcome_step(
                    session,
                    campaign,
                    application,
                    record,
                    now,
                )
                cls._complete_execution_if_target_achieved(
                    session, campaign, context, now
                )
                return cls._serialize_outcome(record)
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="A hiring outcome already exists for this application or idempotency key",
            ) from exc

    @classmethod
    def _complete_execution_if_target_achieved(
        cls,
        session,
        campaign: RecruitmentCampaignRecord,
        context: TenantContext,
        now: datetime,
    ) -> None:
        if not cls.target_is_achieved(session, campaign, now=now):
            return
        execution = session.execute(
            select(RecruitmentCampaignExecutionRecord).where(
                RecruitmentCampaignExecutionRecord.organization_id
                == campaign.organization_id,
                RecruitmentCampaignExecutionRecord.campaign_id == campaign.id,
            )
        ).scalar_one_or_none()
        if execution is None or execution.status == "completed":
            return
        execution.status = "completed"
        execution.completed_at = now
        execution.lease_token = None
        execution.lease_expires_at = None
        execution.available_at = None
        execution.failure_code = None
        execution.failure_message = None
        execution.updated_at = now
        session.add(
            AccessAuditRecord(
                actor_user_id=context.actor_user_id,
                organization_id=campaign.organization_id,
                requested_organization_id=campaign.organization_id,
                action="campaign_hiring_target_achieved",
                resource_type="recruitment_campaign_execution",
                resource_id=execution.id,
                outcome="allowed",
                support_reason=context.support_reason,
            )
        )

    @staticmethod
    def _update_application_outcome_step(
        session,
        campaign: RecruitmentCampaignRecord,
        application: RecruitmentApplicationRecord,
        outcome: RecruitmentCampaignHiringOutcomeRecord,
        now: datetime,
    ) -> None:
        execution = session.execute(
            select(RecruitmentCampaignExecutionRecord).where(
                RecruitmentCampaignExecutionRecord.organization_id
                == campaign.organization_id,
                RecruitmentCampaignExecutionRecord.campaign_id == campaign.id,
            )
        ).scalar_one_or_none()
        if execution is None:
            return
        step = session.execute(
            select(RecruitmentCampaignExecutionStepRecord).where(
                RecruitmentCampaignExecutionStepRecord.execution_id == execution.id,
                RecruitmentCampaignExecutionStepRecord.step_key
                == f"application:{application.id}",
            )
        ).scalar_one_or_none()
        output = {} if step is None or not step.output_json else json.loads(step.output_json)
        output.update(
            {
                "application_id": application.id,
                "stage": outcome.outcome,
                "next_action": None,
                "hiring_outcome_id": outcome.id,
                "hiring_outcome": outcome.outcome,
                "criteria_satisfied": outcome.criteria_satisfied,
                "waiting_on": None,
                "blocking_reason": None,
            }
        )
        if step is None:
            step = RecruitmentCampaignExecutionStepRecord(
                execution_id=execution.id,
                step_key=f"application:{application.id}",
                status="completed",
                attempt_count=0,
                input_json="{}",
                output_json=json.dumps(output, sort_keys=True, separators=(",", ":")),
                created_at=now,
                updated_at=now,
            )
            session.add(step)
        else:
            step.status = "completed"
            step.output_json = json.dumps(output, sort_keys=True, separators=(",", ":"))
            step.updated_at = now

    @staticmethod
    def _require_outcome_role(context: TenantContext) -> None:
        if context.role not in {"recruiter", "org_admin", "platform_admin"}:
            raise HTTPException(status_code=403, detail="Hiring outcome access denied")

    @staticmethod
    def _serialize_outcome(
        record: RecruitmentCampaignHiringOutcomeRecord,
    ) -> CampaignHiringOutcomeResponse:
        return CampaignHiringOutcomeResponse(
            id=record.id,
            campaign_id=record.campaign_id,
            application_id=record.application_id,
            outcome=record.outcome,
            criteria_satisfied=record.criteria_satisfied,
            objective_version=record.objective_version,
            plan_version=record.plan_version,
            decided_by_user_id=record.decided_by_user_id,
            decided_at=record.decided_at,
            reason=record.reason,
        )

    @staticmethod
    def _add_progress_status(
        counts: CampaignWorkCounts,
        status_value: str,
        *,
        reviewed: bool = False,
    ) -> None:
        if status_value in {
            "pending",
            "draft",
            "proposed",
            "approved",
            "pending_response",
            "awaiting_progress",
        }:
            counts.pending += 1
        elif status_value in {"running", "sending", "reschedule_pending"}:
            counts.in_progress += 1
        elif status_value in {
            "waiting_for_approval",
            "needs_review",
            "awaiting_approval",
            "responded",
            "pending_confirmation",
        }:
            if reviewed:
                counts.completed += 1
            else:
                counts.awaiting_human_approval += 1
        elif status_value in {
            "completed",
            "sent",
            "simulated",
            "reviewed",
            "confirmed_internal",
            "cancelled",
            "expired",
        }:
            counts.completed += 1
        elif status_value == "retryable_failure":
            counts.retryable_failures += 1
        elif status_value in {"needs_attention", "failed"}:
            counts.attention_needed += 1

    @classmethod
    def _campaign_work_counts(cls, session, campaign) -> CampaignWorkCounts:
        counts = CampaignWorkCounts()
        if campaign.approval_state == "pending_review":
            counts.awaiting_human_approval += 1

        for execution_status in session.execute(
            select(RecruitmentCampaignExecutionRecord.status).where(
                RecruitmentCampaignExecutionRecord.organization_id
                == campaign.organization_id,
                RecruitmentCampaignExecutionRecord.campaign_id == campaign.id,
            )
        ).scalars():
            cls._add_progress_status(counts, execution_status)

        outcome_by_application = {
            item.application_id: item
            for item in session.execute(
                select(RecruitmentCampaignHiringOutcomeRecord).where(
                    RecruitmentCampaignHiringOutcomeRecord.organization_id
                    == campaign.organization_id,
                    RecruitmentCampaignHiringOutcomeRecord.campaign_id
                    == campaign.id,
                )
            ).scalars()
        }
        for application_id, application_status, review_status in session.execute(
            select(
                RecruitmentApplicationRecord.id,
                RecruitmentApplicationRecord.status,
                RecruitmentApplicationRecord.review_status,
            ).where(
                RecruitmentApplicationRecord.organization_id
                == campaign.organization_id,
                RecruitmentApplicationRecord.campaign_id == campaign.id,
            )
        ):
            outcome = outcome_by_application.get(application_id)
            if outcome is not None:
                if outcome.outcome == "hired":
                    counts.completed += 1
                else:
                    counts.rejected_or_withdrawn += 1
                continue
            cls._add_progress_status(
                counts,
                application_status,
                reviewed=review_status == "reviewed"
                and application_status == "needs_review",
            )

        for model, status_column in (
            (CandidateCommunicationRecord, CandidateCommunicationRecord.status),
            (CandidateAvailabilityRequestRecord, CandidateAvailabilityRequestRecord.status),
            (InterviewScheduleRecord, InterviewScheduleRecord.status),
        ):
            for item_status in session.execute(
                select(status_column).where(
                    model.organization_id == campaign.organization_id,
                    model.campaign_id == campaign.id,
                )
            ).scalars():
                cls._add_progress_status(counts, item_status)
        return counts

    @staticmethod
    def _qualifying_hire_count(
        objective: CampaignObjective,
        campaign: RecruitmentCampaignRecord,
        outcomes: list[RecruitmentCampaignHiringOutcomeRecord],
    ) -> int:
        if objective.deadline is None or objective.timezone_name is None:
            return 0
        count = 0
        for outcome in outcomes:
            decided_at = outcome.decided_at
            if decided_at.tzinfo is None:
                decided_at = decided_at.replace(tzinfo=UTC)
            if (
                outcome.outcome == "hired"
                and outcome.criteria_satisfied
                and outcome.objective_version == campaign.approved_objective_version
                and outcome.plan_version == campaign.approved_plan_version
                and decided_at.astimezone(
                    ZoneInfo(objective.timezone_name)
                ).date()
                <= objective.deadline
            ):
                count += 1
        return count

    @classmethod
    def _objective_progress_status(
        cls,
        objective: CampaignObjective,
        campaign: RecruitmentCampaignRecord,
        qualifying_hires: int,
        instant: datetime,
    ) -> str:
        if not cls._objective_complete(campaign):
            return "incomplete"
        if not cls.approval_is_current(campaign):
            return "awaiting_approval"
        if (
            objective.target_headcount is not None
            and qualifying_hires >= objective.target_headcount
        ):
            return "target_achieved"
        if objective.deadline is not None and objective.timezone_name is not None:
            local_today = instant.astimezone(ZoneInfo(objective.timezone_name)).date()
            if objective.deadline < local_today:
                return "deadline_passed"
        return "active"

    @classmethod
    def target_is_achieved(
        cls,
        session,
        campaign: RecruitmentCampaignRecord,
        *,
        now: datetime | None = None,
    ) -> bool:
        instant = now or datetime.now(UTC)
        objective = cls._objective(campaign)
        outcomes = list(
            session.execute(
                select(RecruitmentCampaignHiringOutcomeRecord).where(
                    RecruitmentCampaignHiringOutcomeRecord.organization_id
                    == campaign.organization_id,
                    RecruitmentCampaignHiringOutcomeRecord.campaign_id
                    == campaign.id,
                )
            ).scalars()
        )
        qualifying_hires = cls._qualifying_hire_count(objective, campaign, outcomes)
        return (
            cls._objective_progress_status(
                objective, campaign, qualifying_hires, instant
            )
            == "target_achieved"
        )

    @classmethod
    def get_progress(
        cls,
        campaign_id: str,
        context: TenantContext,
        *,
        now: datetime | None = None,
    ) -> CampaignProgressResponse:
        instant = now or datetime.now(UTC)
        with SessionLocal() as session:
            campaign = cls._load_campaign(
                session, campaign_id, organization_id=context.organization_id
            )
            if campaign is None:
                raise HTTPException(status_code=404, detail="Campaign not found")
            cls._assert_authorized(campaign, context)
            objective = cls._objective(campaign)
            outcomes = list(
                session.execute(
                    select(RecruitmentCampaignHiringOutcomeRecord).where(
                        RecruitmentCampaignHiringOutcomeRecord.organization_id
                        == campaign.organization_id,
                        RecruitmentCampaignHiringOutcomeRecord.campaign_id
                        == campaign.id,
                    )
                ).scalars()
            )
            counts = cls._campaign_work_counts(session, campaign)
            qualifying_hires = cls._qualifying_hire_count(
                objective, campaign, outcomes
            )
            objective_status = cls._objective_progress_status(
                objective, campaign, qualifying_hires, instant
            )

            hired = sum(item.outcome == "hired" for item in outcomes)
            rejected = sum(item.outcome == "rejected" for item in outcomes)
            withdrawn = sum(item.outcome == "withdrawn" for item in outcomes)
            remaining = (
                None
                if objective.target_headcount is None
                else max(0, objective.target_headcount - qualifying_hires)
            )
            return CampaignProgressResponse(
                campaign_id=campaign.id,
                objective_status=objective_status,
                objective_version=campaign.objective_version,
                plan_version=campaign.plan_version,
                target_headcount=objective.target_headcount,
                hired_outcomes=hired,
                qualifying_hires=qualifying_hires,
                rejected_outcomes=rejected,
                withdrawn_outcomes=withdrawn,
                remaining_hires=remaining,
                work_counts=counts,
                unresolved_questions=objective.unresolved_questions(),
                application_workflows=cls._application_workflows(session, campaign),
            )

    @staticmethod
    def _application_workflows(
        session, campaign: RecruitmentCampaignRecord
    ) -> list[CampaignApplicationWorkflow]:
        execution_id = session.execute(
            select(RecruitmentCampaignExecutionRecord.id).where(
                RecruitmentCampaignExecutionRecord.organization_id
                == campaign.organization_id,
                RecruitmentCampaignExecutionRecord.campaign_id == campaign.id,
            )
        ).scalar_one_or_none()
        if execution_id is None:
            return []
        steps = session.execute(
            select(RecruitmentCampaignExecutionStepRecord)
            .where(
                RecruitmentCampaignExecutionStepRecord.execution_id == execution_id,
                RecruitmentCampaignExecutionStepRecord.step_key.like("application:%"),
                RecruitmentCampaignExecutionStepRecord.output_json.is_not(None),
            )
            .order_by(RecruitmentCampaignExecutionStepRecord.step_key)
        ).scalars()
        workflows: list[CampaignApplicationWorkflow] = []
        for step in steps:
            output = json.loads(step.output_json or "{}")
            application_id = output.get("application_id")
            stage = output.get("stage")
            if not isinstance(application_id, str) or not isinstance(stage, str):
                continue
            handoffs = {
                key: value
                for key, value in output.items()
                if key not in {"application_id", "stage", "next_action"}
            }
            workflows.append(
                CampaignApplicationWorkflow(
                    application_id=application_id,
                    stage=stage,
                    next_action=output.get("next_action"),
                    work_status=step.status,
                    handoffs=handoffs,
                )
            )
        return workflows

    @staticmethod
    def serialize(record: RecruitmentCampaignRecord) -> CampaignResponse:
        objective = RecruitmentCampaignService._objective(record)
        rubric = json.loads(record.competency_rubric_json or "[]")
        return CampaignResponse(
            id=record.id,
            organization_id=record.organization_id,
            owner_user_id=record.owner_user_id,
            director_hiring_brief=record.director_hiring_brief,
            structured_requirements=json.loads(record.structured_requirements_json or "{}"),
            job_description=record.job_description,
            competency_rubric=rubric,
            application_form_schema=json.loads(record.application_form_schema_json or "{}"),
            objective=objective,
            objective_version=record.objective_version,
            plan_version=record.plan_version,
            approved_objective_version=record.approved_objective_version,
            approved_plan_version=record.approved_plan_version,
            unresolved_questions=objective.unresolved_questions(),
            objective_complete=objective.is_complete(rubric),
            approval_state=record.approval_state,
            workflow_state=record.workflow_state,
            approval_reason=record.approval_reason,
            approval_decided_by_user_id=record.approval_decided_by_user_id,
            approval_decided_at=record.approval_decided_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
