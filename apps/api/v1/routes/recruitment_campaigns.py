from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from config import settings
from services.candidate_application_service import (
    ApplicationResponse,
    ApplicationReviewRequest,
    CandidateApplicationService,
)
from services.candidate_availability_service import (
    AvailabilityRequestCreate,
    AvailabilityRequestResponse,
    AvailabilityResponseSubmit,
    AvailabilityReview,
    CandidateAvailabilityService,
)
from services.candidate_communication_service import (
    CandidateCommunicationService,
    CommunicationDraftRequest,
    CommunicationReconciliationRequest,
    CommunicationResponse,
)
from services.interview_scheduling_service import (
    CompatibleSlotResponse,
    InterviewScheduleCancellation,
    InterviewScheduleDetailResponse,
    InterviewScheduleProposal,
    InterviewScheduleReschedule,
    InterviewScheduleResponse,
    InterviewScheduleTransition,
    InterviewSchedulingService,
)
from services.recruitment_campaign_orchestrator import (
    CampaignExecutionResponse,
    RecruitmentCampaignOrchestrator,
)
from services.recruitment_campaign_service import (
    CampaignClarificationResolution,
    CampaignCreate,
    CampaignHiringOutcomeRequest,
    CampaignHiringOutcomeResponse,
    CampaignObjectiveUpdate,
    CampaignProgressResponse,
    CampaignResponse,
    CampaignUpdate,
    RecruitmentCampaignService,
)
from services.tenant_authorization import TenantContext, tenant_context

router = APIRouter()


@router.post("/recruitment-campaigns", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreate,
    context: TenantContext = Depends(tenant_context),
):
    if context.role not in {"recruiter", "org_admin", "platform_admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Campaign access denied")
    campaign = RecruitmentCampaignService.create_campaign(
        organization_id=context.organization_id,
        owner_user_id=context.actor_user_id,
        payload=payload,
    )
    return RecruitmentCampaignService.serialize(campaign)


@router.get("/recruitment-campaigns", response_model=list[CampaignResponse])
async def list_campaigns(context: TenantContext = Depends(tenant_context)):
    return [RecruitmentCampaignService.serialize(c) for c in RecruitmentCampaignService.list_campaigns(context)]


@router.get("/recruitment-campaigns/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(campaign_id: str, context: TenantContext = Depends(tenant_context)):
    return RecruitmentCampaignService.serialize(RecruitmentCampaignService.get_campaign(campaign_id, context))


@router.patch("/recruitment-campaigns/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: str,
    payload: CampaignUpdate,
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.update_campaign(campaign_id, context, payload)
    )


@router.get(
    "/recruitment-campaigns/{campaign_id}/objective",
    response_model=CampaignResponse,
)
async def get_campaign_objective(
    campaign_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.get_campaign(campaign_id, context)
    )


@router.put(
    "/recruitment-campaigns/{campaign_id}/objective",
    response_model=CampaignResponse,
)
async def update_campaign_objective(
    campaign_id: str,
    payload: CampaignObjectiveUpdate,
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.update_objective(campaign_id, context, payload)
    )


@router.post(
    "/recruitment-campaigns/{campaign_id}/objective/clarifications/{clarification_id}/resolve",
    response_model=CampaignResponse,
)
async def resolve_campaign_objective_clarification(
    campaign_id: str,
    clarification_id: str,
    payload: CampaignClarificationResolution,
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.resolve_clarification(
            campaign_id, clarification_id, context, payload
        )
    )


@router.get(
    "/recruitment-campaigns/{campaign_id}/progress",
    response_model=CampaignProgressResponse,
)
async def get_campaign_progress(
    campaign_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignService.get_progress(campaign_id, context)


@router.post(
    "/recruitment-campaigns/{campaign_id}/applications/{application_id}/outcome",
    response_model=CampaignHiringOutcomeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_campaign_hiring_outcome(
    campaign_id: str,
    application_id: str,
    payload: CampaignHiringOutcomeRequest,
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignService.record_hiring_outcome(
        campaign_id, application_id, context, payload
    )


@router.post("/recruitment-campaigns/{campaign_id}/requirements")
async def set_requirements(
    campaign_id: str,
    payload: dict[str, Any],
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.set_requirements(campaign_id, context, payload)
    )


@router.post("/recruitment-campaigns/{campaign_id}/job-description")
async def set_job_description(
    campaign_id: str,
    payload: dict[str, Any],
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.set_job_description(campaign_id, context, payload.get("job_description", ""))
    )


@router.post("/recruitment-campaigns/{campaign_id}/rubric")
async def set_rubric(
    campaign_id: str,
    payload: dict[str, Any],
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.set_competency_rubric(campaign_id, context, payload.get("competency_rubric", []))
    )


@router.post("/recruitment-campaigns/{campaign_id}/application-form")
async def set_application_form(
    campaign_id: str,
    payload: dict[str, Any],
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.set_application_form_schema(campaign_id, context, payload.get("application_form_schema", {}))
    )


@router.post("/recruitment-campaigns/{campaign_id}/submit-for-approval")
async def submit_for_approval(campaign_id: str, context: TenantContext = Depends(tenant_context)):
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.submit_for_approval(campaign_id, context)
    )


@router.post("/recruitment-campaigns/{campaign_id}/approve")
async def approve_campaign(
    campaign_id: str,
    payload: dict[str, Any] | None = None,
    context: TenantContext = Depends(tenant_context),
):
    reason = None if payload is None else payload.get("approval_reason")
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.approve(campaign_id, context, reason)
    )


@router.post("/recruitment-campaigns/{campaign_id}/reject")
async def reject_campaign(
    campaign_id: str,
    payload: dict[str, Any] | None = None,
    context: TenantContext = Depends(tenant_context),
):
    reason = None if payload is None else payload.get("approval_reason")
    return RecruitmentCampaignService.serialize(
        RecruitmentCampaignService.reject(campaign_id, context, reason)
    )


@router.post(
    "/recruitment-campaigns/{campaign_id}/executions",
    response_model=CampaignExecutionResponse,
)
async def start_campaign_execution(
    campaign_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignOrchestrator.start(campaign_id, context)


@router.get(
    "/recruitment-campaign-executions/{execution_id}",
    response_model=CampaignExecutionResponse,
)
async def get_campaign_execution(
    execution_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignOrchestrator.get(execution_id, context)


@router.post(
    "/recruitment-campaign-executions/{execution_id}/resume",
    response_model=CampaignExecutionResponse,
)
async def resume_campaign_execution(
    execution_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return RecruitmentCampaignOrchestrator.resume(execution_id, context)


@router.post(
    "/recruitment-campaigns/{campaign_id}/applications/cv",
    response_model=ApplicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_candidate_cv(
    campaign_id: str,
    file: UploadFile = File(...),
    context: TenantContext = Depends(tenant_context),
):
    if file.size is not None and file.size > settings.CV_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="CV exceeds the maximum upload size",
        )
    content = await file.read(settings.CV_MAX_UPLOAD_BYTES + 1)
    return CandidateApplicationService.submit_cv(
        campaign_id,
        context,
        file.filename or "",
        file.content_type,
        content,
    )


@router.get(
    "/recruitment-applications/{application_id}",
    response_model=ApplicationResponse,
)
async def get_recruitment_application(
    application_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateApplicationService.get_application(application_id, context)


@router.post(
    "/recruitment-applications/{application_id}/review",
    response_model=ApplicationResponse,
)
async def review_recruitment_application(
    application_id: str,
    payload: ApplicationReviewRequest,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateApplicationService.review_application(
        application_id, context, payload
    )


@router.post(
    "/recruitment-applications/{application_id}/communications",
    response_model=CommunicationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_candidate_communication(
    application_id: str,
    payload: CommunicationDraftRequest,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateCommunicationService.create_draft(
        application_id, context, payload
    )


@router.get(
    "/candidate-communications/{communication_id}",
    response_model=CommunicationResponse,
)
async def get_candidate_communication(
    communication_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateCommunicationService.get(communication_id, context)


@router.post(
    "/candidate-communications/{communication_id}/submit",
    response_model=CommunicationResponse,
)
async def submit_candidate_communication(
    communication_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateCommunicationService.submit_for_approval(
        communication_id, context
    )


@router.post(
    "/candidate-communications/{communication_id}/approve",
    response_model=CommunicationResponse,
)
async def approve_candidate_communication(
    communication_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateCommunicationService.approve(communication_id, context)


@router.post(
    "/candidate-communications/{communication_id}/retry",
    response_model=CommunicationResponse,
)
async def retry_candidate_communication(
    communication_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateCommunicationService.retry(communication_id, context)


@router.post(
    "/candidate-communications/{communication_id}/reconcile",
    response_model=CommunicationResponse,
)
async def reconcile_candidate_communication(
    communication_id: str,
    payload: CommunicationReconciliationRequest,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateCommunicationService.reconcile(
        communication_id, context, payload
    )


@router.post(
    "/candidate-communications/{communication_id}/cancel",
    response_model=CommunicationResponse,
)
async def cancel_candidate_communication(
    communication_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateCommunicationService.cancel(communication_id, context)


@router.post(
    "/recruitment-campaigns/{campaign_id}/availability-requests",
    response_model=AvailabilityRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_candidate_availability_request(
    campaign_id: str,
    payload: AvailabilityRequestCreate,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateAvailabilityService.create_request(campaign_id, context, payload)


@router.get(
    "/recruitment-campaigns/{campaign_id}/availability-requests",
    response_model=list[AvailabilityRequestResponse],
)
async def list_candidate_availability_requests(
    campaign_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateAvailabilityService.list_for_campaign(campaign_id, context)


@router.get(
    "/candidate-availability-requests/{request_id}",
    response_model=AvailabilityRequestResponse,
)
async def get_candidate_availability_request(
    request_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateAvailabilityService.get_request(
        request_id, context, candidate_only=context.role == "candidate"
    )


@router.post(
    "/candidate-availability-requests/{request_id}/responses",
    response_model=AvailabilityRequestResponse,
)
async def submit_candidate_availability_response(
    request_id: str,
    payload: AvailabilityResponseSubmit,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateAvailabilityService.submit_response(request_id, context, payload)


@router.post(
    "/candidate-availability-requests/{request_id}/review",
    response_model=AvailabilityRequestResponse,
)
async def review_candidate_availability_response(
    request_id: str,
    payload: AvailabilityReview,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateAvailabilityService.review_response(request_id, context, payload)


@router.post(
    "/candidate-availability-requests/{request_id}/cancel",
    response_model=AvailabilityRequestResponse,
)
async def cancel_candidate_availability_request(
    request_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return CandidateAvailabilityService.cancel(request_id, context)


@router.get(
    "/candidate-availability-requests/{request_id}/schedule-slots",
    response_model=CompatibleSlotResponse,
)
async def get_candidate_compatible_schedule_slots(
    request_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return InterviewSchedulingService.compatible_slots(request_id, context)


@router.post(
    "/recruitment-campaigns/{campaign_id}/applications/{application_id}/interview-schedules",
    response_model=InterviewScheduleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def propose_interview_schedule(
    campaign_id: str,
    application_id: str,
    payload: InterviewScheduleProposal,
    context: TenantContext = Depends(tenant_context),
):
    return InterviewSchedulingService.propose(campaign_id, application_id, context, payload)


@router.get(
    "/recruitment-campaigns/{campaign_id}/interview-schedules",
    response_model=list[InterviewScheduleResponse],
)
async def list_interview_schedules(
    campaign_id: str,
    application_id: str | None = None,
    context: TenantContext = Depends(tenant_context),
):
    return InterviewSchedulingService.list_for_campaign(
        campaign_id, context, application_id=application_id
    )


@router.get(
    "/interview-schedules/{schedule_id}",
    response_model=InterviewScheduleDetailResponse,
)
async def get_interview_schedule(
    schedule_id: str,
    context: TenantContext = Depends(tenant_context),
):
    return InterviewSchedulingService.get(schedule_id, context)


@router.post(
    "/interview-schedules/{schedule_id}/submit",
    response_model=InterviewScheduleResponse,
)
async def submit_interview_schedule(
    schedule_id: str,
    payload: InterviewScheduleTransition,
    context: TenantContext = Depends(tenant_context),
):
    return InterviewSchedulingService.submit(schedule_id, context, payload)


@router.post(
    "/interview-schedules/{schedule_id}/confirm",
    response_model=InterviewScheduleResponse,
)
async def confirm_interview_schedule(
    schedule_id: str,
    payload: InterviewScheduleTransition,
    context: TenantContext = Depends(tenant_context),
):
    return InterviewSchedulingService.confirm(schedule_id, context, payload)


@router.post(
    "/interview-schedules/{schedule_id}/reschedule",
    response_model=InterviewScheduleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def propose_interview_reschedule(
    schedule_id: str,
    payload: InterviewScheduleReschedule,
    context: TenantContext = Depends(tenant_context),
):
    return InterviewSchedulingService.reschedule(schedule_id, context, payload)


@router.post(
    "/interview-schedules/{schedule_id}/cancel",
    response_model=InterviewScheduleResponse,
)
async def cancel_interview_schedule(
    schedule_id: str,
    payload: InterviewScheduleCancellation,
    context: TenantContext = Depends(tenant_context),
):
    return InterviewSchedulingService.cancel(schedule_id, context, payload)
