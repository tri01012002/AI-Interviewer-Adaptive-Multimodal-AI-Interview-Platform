from __future__ import annotations

import json
import socket
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from apps.api.main import app
from config import settings
from services import email_provider
from services.candidate_application_service import CandidateApplicationService
from services.candidate_communication_service import CandidateCommunicationService
from services.database import (
    AccessAuditRecord,
    CandidateAvailabilityEventRecord,
    CandidateAvailabilityRequestRecord,
    CandidateCommunicationEventRecord,
    CandidateCommunicationRecord,
    InterviewRecord,
    InterviewScheduleEventRecord,
    InterviewScheduleRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignExecutionRecord,
    RecruitmentCampaignExecutionStepRecord,
    RecruitmentCampaignHiringOutcomeRecord,
    RecruitmentCampaignRecord,
    SessionLocal,
)
from services.recruitment_campaign_orchestrator import RecruitmentCampaignOrchestrator
from services.recruitment_campaign_worker import RecruitmentCampaignWorker
from services.report_service import ReportService
from services.tenant_authorization import TenantContext
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    tenant_headers,
)
from tests.unit.test_interview_scheduling import (
    _confirm,
    _future_local_slots,
)
from tests.unit.test_recruitment_candidate_intake import (
    create_approved_campaign,
    make_docx,
    submit_cv,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _prepare_candidate_with_worker(
    client: TestClient,
    tenant,
    campaign_id: str,
    candidate_headers: dict[str, str],
    *,
    slot_index: int,
) -> tuple[str, str, dict]:
    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx("Senior engineer built Python and Kubernetes services."),
    )
    assert uploaded.status_code == 202, uploaded.text
    application_id = uploaded.json()["id"]
    processed = CandidateApplicationService.process_dispatched(application_id)
    assert processed is not None and processed.status == "needs_review"
    reviewed = client.post(
        f"/api/v1/recruitment-applications/{application_id}/review",
        json={
            "criteria": [
                {"criterion_id": item["criterion_id"], "status": "confirmed"}
                for item in processed.screening["assessments"]
            ]
        },
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert reviewed.status_code == 200, reviewed.text

    RecruitmentCampaignWorker().dispatch_once()
    RecruitmentCampaignWorker().dispatch_once()
    with SessionLocal() as session:
        communication = session.execute(
            select(CandidateCommunicationRecord).where(
                CandidateCommunicationRecord.application_id == application_id,
                CandidateCommunicationRecord.template_type == "interview_invitation",
            )
        ).scalar_one()
        communication_id = communication.id
        assert session.scalar(
            select(func.count(CandidateAvailabilityRequestRecord.id)).where(
                CandidateAvailabilityRequestRecord.application_id == application_id
            )
        ) == 0
        assert session.scalar(
            select(func.count(CandidateCommunicationRecord.id)).where(
                CandidateCommunicationRecord.application_id == application_id,
                CandidateCommunicationRecord.template_type == "interview_invitation",
            )
        ) == 1
        draft_event = session.execute(
            select(CandidateCommunicationEventRecord).where(
                CandidateCommunicationEventRecord.communication_id
                == communication_id,
                CandidateCommunicationEventRecord.event_type
                == "communication_draft_created",
            )
        ).scalar_one()
        assert draft_event.actor_user_id is None
    submitted = client.post(
        f"/api/v1/candidate-communications/{communication_id}/submit",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert submitted.status_code == 200, submitted.text
    RecruitmentCampaignWorker().dispatch_once()
    with SessionLocal() as session:
        pending_approval = session.get(CandidateCommunicationRecord, communication_id)
        assert pending_approval is not None
        assert pending_approval.status == "awaiting_approval"
        assert session.scalar(
            select(func.count(CandidateAvailabilityRequestRecord.id)).where(
                CandidateAvailabilityRequestRecord.application_id == application_id
            )
        ) == 0
    approved = client.post(
        f"/api/v1/candidate-communications/{communication_id}/approve",
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert approved.status_code == 200, approved.text

    execution_id = _wake_execution(campaign_id)
    reconciled = RecruitmentCampaignOrchestrator.run_dispatched(execution_id)
    assert reconciled is not None and reconciled.status == "awaiting_progress"
    RecruitmentCampaignWorker().dispatch_once()
    with SessionLocal() as session:
        request = session.execute(
            select(CandidateAvailabilityRequestRecord).where(
                CandidateAvailabilityRequestRecord.application_id == application_id
            )
        ).scalar_one()
        assert session.scalar(
            select(func.count(CandidateAvailabilityRequestRecord.id)).where(
                CandidateAvailabilityRequestRecord.application_id == application_id
            )
        ) == 1
        request_event = session.execute(
            select(CandidateAvailabilityEventRecord).where(
                CandidateAvailabilityEventRecord.request_id == request.id,
                CandidateAvailabilityEventRecord.event_type == "request_created",
            )
        ).scalar_one()
        assert request_event.actor_user_id is None
        proposed_windows = json.loads(request.proposed_windows_json)
    selected = proposed_windows[slot_index]
    zone = ZoneInfo(request.timezone_name)
    candidate_window = {
        "start": datetime.fromisoformat(selected["start"])
        .astimezone(zone)
        .isoformat(),
        "end": datetime.fromisoformat(selected["end"])
        .astimezone(zone)
        .isoformat(),
    }
    candidate_response = client.post(
        f"/api/v1/candidate-availability-requests/{request.id}/responses",
        json={
            "idempotency_key": f"r53-candidate-response-{application_id[:16]}",
            "timezone_name": request.timezone_name,
            "windows": [candidate_window],
        },
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    )
    assert candidate_response.status_code == 200, candidate_response.text
    reviewed_availability = client.post(
        f"/api/v1/candidate-availability-requests/{request.id}/review",
        json={"preferred_window_indices": [0]},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert reviewed_availability.status_code == 200, reviewed_availability.text

    advanced = RecruitmentCampaignWorker().dispatch_once()
    schedule_action = next(
        item
        for item in advanced
        if getattr(item, "action", None) == "schedule_submitted_for_confirmation"
        and getattr(item, "application_id", None) == application_id
    )
    with SessionLocal() as session:
        schedule = session.get(InterviewScheduleRecord, schedule_action.resulting_id)
        assert schedule is not None and schedule.status == "pending_confirmation"
        schedule_payload = {"id": schedule.id, "version": schedule.version}
        schedule_events = session.execute(
            select(InterviewScheduleEventRecord).where(
                InterviewScheduleEventRecord.schedule_id == schedule.id,
                InterviewScheduleEventRecord.event_type.in_(
                    ("proposal_created", "submitted_for_confirmation")
                ),
            )
        ).scalars().all()
        assert len(schedule_events) == 2
        assert all(event.actor_user_id is None for event in schedule_events)
        automation_audits = session.execute(
            select(AccessAuditRecord).where(
                AccessAuditRecord.resource_id == schedule.id,
                AccessAuditRecord.action.in_(
                    ("interview_schedule_proposed", "interview_schedule_submitted")
                ),
            )
        ).scalars().all()
        assert len(automation_audits) == 2
        assert all(audit.actor_user_id is None for audit in automation_audits)

    replayed = RecruitmentCampaignWorker().dispatch_once()
    assert not any(
        getattr(item, "action", None) == "schedule_submitted_for_confirmation"
        and getattr(item, "application_id", None) == application_id
        for item in replayed
    )
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count(InterviewScheduleRecord.id)).where(
                InterviewScheduleRecord.application_id == application_id
            )
        ) == 1

    confirmed = _confirm(
        client,
        tenant,
        schedule_payload,
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed_internal"
    with SessionLocal() as session:
        assert session.scalar(
            select(InterviewRecord.id).where(
                InterviewRecord.application_id == application_id
            )
        ) is None
    return application_id, communication_id, confirmed.json()


def _complete_candidate_interview(
    client: TestClient,
    tenant,
    campaign_id: str,
    application_id: str,
    schedule: dict,
    candidate_headers: dict[str, str],
) -> tuple[str, dict]:
    headers = tenant_headers(candidate_headers, tenant.organization_id)
    endpoint = (
        f"/api/v1/interview/campaigns/{campaign_id}/applications/"
        f"{application_id}/start"
    )
    first = client.post(
        endpoint,
        json={"schedule_id": schedule["id"], "mode": "text"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    replay = client.post(
        endpoint,
        json={"schedule_id": schedule["id"], "mode": "text"},
        headers=headers,
    )
    assert replay.status_code == 200
    assert replay.json()["interview_id"] == first.json()["interview_id"]

    state = first.json()
    interview_id = state["interview_id"]
    for index in range(10):
        if state["status"] == "completed":
            break
        response = client.post(
            f"/api/v1/interview/{interview_id}/answer",
            json={
                "turn_id": f"campaign-turn-{application_id[:8]}-{index}",
                "answer": (
                    "I built and deployed Python and Kubernetes services, "
                    "then improved production latency by 30 percent."
                ),
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        state = response.json()
    assert state["status"] == "completed"
    assert state["rubric_snapshot"]
    report = ReportService().build_report(
        state["candidate_id"],
        interview_id,
        organization_id=tenant.organization_id,
    )
    assert report["is_final"] is True
    assert report["campaign"] == {
        "campaign_id": campaign_id,
        "application_id": application_id,
        "schedule_id": schedule["id"],
        "objective_version": state["objective_version"],
        "plan_version": state["plan_version"],
        "rubric_snapshot": state["rubric_snapshot"],
    }
    return interview_id, report


def _record_hire(client: TestClient, tenant, campaign_id: str, application_id: str):
    response = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/applications/{application_id}/outcome",
        json={
            "outcome": "hired",
            "criteria_satisfied": True,
            "idempotency_key": f"hire-{application_id[:20]}",
            "reason": "Administrator confirmed the approved hiring criteria.",
        },
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wake_execution(campaign_id: str) -> str:
    with SessionLocal.begin() as session:
        execution = session.execute(
            select(RecruitmentCampaignExecutionRecord).where(
                RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
            )
        ).scalar_one()
        execution.available_at = datetime.now(UTC) - timedelta(seconds=1)
        return execution.id


def _setup_offline_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "CV_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "offline_simulation")
    monkeypatch.setattr(
        email_provider.smtplib,
        "SMTP",
        lambda *args, **kwargs: pytest.fail("campaign test attempted SMTP"),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: pytest.fail("campaign test attempted a network connection"),
    )


def test_approved_campaign_recovers_across_intake_to_explicit_hiring_outcomes(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_offline_env(monkeypatch, tmp_path)

    tenant = create_tenant(client)
    _, first_headers, _, _ = create_candidate_through_invitation(client, tenant)
    policy_windows = _future_local_slots(count=2)
    campaign_id = create_approved_campaign(
        client,
        tenant,
        campaign_automation={
            "response_deadline": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "interview_duration_minutes": 30,
            "timezone_name": "Asia/Ho_Chi_Minh",
            "proposed_windows": [
                {
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=30)).isoformat(),
                }
                for start in policy_windows
            ],
            "interviewer_user_id": tenant.recruiter["id"],
            "exclusive_resources": [],
        },
    )
    execution = RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )
    assert execution.status == "awaiting_progress"

    first_application, first_communication, first_schedule = _prepare_candidate_with_worker(
        client,
        tenant,
        campaign_id,
        first_headers,
        slot_index=0,
    )
    first_interview, first_report = _complete_candidate_interview(
        client,
        tenant,
        campaign_id,
        first_application,
        first_schedule,
        first_headers,
    )
    execution_id = _wake_execution(campaign_id)
    reconciled = RecruitmentCampaignOrchestrator.run_dispatched(execution_id)
    assert reconciled is not None and reconciled.status == "awaiting_progress"
    assert first_report["campaign"]["rubric_snapshot"]
    with SessionLocal() as session:
        persisted_interview = session.execute(
            select(InterviewRecord).where(
                InterviewRecord.id == first_interview,
                InterviewRecord.campaign_id == campaign_id,
                InterviewRecord.application_id == first_application,
                InterviewRecord.schedule_id == first_schedule["id"],
            )
        ).scalar_one()
        assert persisted_interview.rubric_snapshot_json == json.dumps(
            first_report["campaign"]["rubric_snapshot"],
            separators=(",", ":"),
        )
    before_hire = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}/progress",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert before_hire.status_code == 200
    assert before_hire.json()["qualifying_hires"] == 0
    assert before_hire.json()["objective_status"] == "active"
    first_progress = next(
        item
        for item in before_hire.json()["application_workflows"]
        if item["application_id"] == first_application
    )
    assert first_progress["stage"] == "awaiting_hiring_decision"
    assert first_progress["work_status"] == "pending"
    assert first_progress["handoffs"]["waiting_on"] == "recruiter_or_authorized_reviewer"

    _record_hire(client, tenant, campaign_id, first_application)
    intermediate = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}/progress",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    ).json()
    assert intermediate["target_headcount"] == 2
    assert intermediate["qualifying_hires"] == 1
    assert intermediate["remaining_hires"] == 1
    assert intermediate["objective_status"] == "active"

    _, other_headers, _, _ = create_candidate_through_invitation(
        client, tenant
    )
    second_application, second_communication, second_schedule = _prepare_candidate_with_worker(
        client,
        tenant,
        campaign_id,
        other_headers,
        slot_index=1,
    )
    assert second_application != first_application
    second_interview, _ = _complete_candidate_interview(
        client,
        tenant,
        campaign_id,
        second_application,
        second_schedule,
        other_headers,
    )
    assert second_interview != first_interview
    _record_hire(client, tenant, campaign_id, second_application)
    assert CandidateApplicationService.discover_eligible(10) == []
    assert CandidateCommunicationService.discover_eligible(10) == []

    execution_id = _wake_execution(campaign_id)
    recovered = RecruitmentCampaignOrchestrator.run_dispatched(execution_id)
    assert recovered is not None and recovered.status == "completed"
    progress = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}/progress",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert progress.status_code == 200
    payload = progress.json()
    assert payload["objective_status"] == "target_achieved"
    assert payload["qualifying_hires"] == 2
    assert payload["remaining_hires"] == 0
    assert {item["stage"] for item in payload["application_workflows"]} == {"hired"}

    for communication_id in (first_communication, second_communication):
        with SessionLocal() as session:
            communication = session.get(CandidateCommunicationRecord, communication_id)
            assert communication is not None
            assert communication.delivery_status == "simulated"
            assert communication.sent_at is None


def test_missing_and_invalid_automation_policy_produces_actionable_waiting_states(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    _, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)

    campaign_id = create_approved_campaign(client, tenant, campaign_automation=None)
    execution = RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )
    assert execution.status == "awaiting_progress"

    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx("Backend engineer with Python expertise."),
    )
    assert uploaded.status_code == 202
    application_id = uploaded.json()["id"]
    processed = CandidateApplicationService.process_dispatched(application_id)
    assert processed is not None and processed.status == "needs_review"
    reviewed = client.post(
        f"/api/v1/recruitment-applications/{application_id}/review",
        json={
            "criteria": [
                {"criterion_id": item["criterion_id"], "status": "confirmed"}
                for item in processed.screening["assessments"]
            ]
        },
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert reviewed.status_code == 200

    RecruitmentCampaignWorker().dispatch_once()
    with SessionLocal() as session:
        communication = session.execute(
            select(CandidateCommunicationRecord).where(
                CandidateCommunicationRecord.application_id == application_id,
                CandidateCommunicationRecord.template_type == "interview_invitation",
            )
        ).scalar_one()
        communication_id = communication.id

    client.post(
        f"/api/v1/candidate-communications/{communication_id}/submit",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    client.post(
        f"/api/v1/candidate-communications/{communication_id}/approve",
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )

    RecruitmentCampaignWorker().dispatch_once()

    execution_id = _wake_execution(campaign_id)
    reconciled = RecruitmentCampaignOrchestrator.run_dispatched(execution_id)
    assert reconciled is not None

    with SessionLocal() as session:
        assert session.scalar(
            select(func.count(CandidateAvailabilityRequestRecord.id)).where(
                CandidateAvailabilityRequestRecord.application_id == application_id
            )
        ) == 0

    progress = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}/progress",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    ).json()
    workflow = next(
        w for w in progress["application_workflows"] if w["application_id"] == application_id
    )
    assert workflow["stage"] == "awaiting_scheduling_policy"
    assert workflow["handoffs"]["waiting_on"] == "campaign_owner_or_authorized_admin"
    assert workflow["handoffs"]["blocking_reason"] == "approved_explicit_scheduling_policy_required"

    expired_policy = {
        "response_deadline": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "interview_duration_minutes": 30,
        "timezone_name": "UTC",
        "proposed_windows": [
            {
                "start": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                "end": (datetime.now(UTC) - timedelta(hours=1, minutes=30)).isoformat(),
            }
        ],
        "interviewer_user_id": tenant.recruiter["id"],
        "exclusive_resources": [],
    }
    with SessionLocal.begin() as session:
        camp = session.get(RecruitmentCampaignRecord, campaign_id)
        reqs = json.loads(camp.structured_requirements_json)
        reqs["campaign_automation"] = expired_policy
        camp.structured_requirements_json = json.dumps(reqs)

    execution_id = _wake_execution(campaign_id)
    reconciled = RecruitmentCampaignOrchestrator.run_dispatched(execution_id)
    assert reconciled is not None

    progress2 = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}/progress",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    ).json()
    workflow2 = next(
        w for w in progress2["application_workflows"] if w["application_id"] == application_id
    )
    assert workflow2["stage"] == "needs_attention"
    assert workflow2["handoffs"]["waiting_on"] == "authorized_operator"
    assert workflow2["handoffs"]["blocking_reason"] == "approved_scheduling_policy_expired"


def test_version_change_and_stale_claim_commit_fencing(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    campaign_id = create_approved_campaign(client, tenant)
    execution = RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )
    assert execution.status == "awaiting_progress"

    stale_token = "stale-worker-token-999"
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        rec = session.get(RecruitmentCampaignExecutionRecord, execution.id)
        rec.status = "running"
        rec.lease_token = stale_token
        rec.lease_expires_at = now + timedelta(seconds=60)

    with SessionLocal.begin() as session:
        camp = session.get(RecruitmentCampaignRecord, campaign_id)
        camp.plan_version += 1
        camp.approval_state = "pending_review"

    context = TenantContext(
        actor_user_id=tenant.recruiter["id"],
        organization_id=tenant.organization_id,
        role="recruiter",
        is_automated=True,
    )
    res = RecruitmentCampaignOrchestrator._run(
        execution.id,
        context,
        provider=None,
        worker_internal=True,
    )
    assert res.status == "needs_attention"
    assert res.failure_code == "campaign_approval_revoked"

    campaign_b_id = create_approved_campaign(client, tenant)
    _, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)
    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_b_id,
        make_docx("Python software engineer."),
    )
    assert uploaded.status_code == 202
    app_id = uploaded.json()["id"]

    from services.candidate_screening import CandidateScreeningResult
    screening = CandidateScreeningResult(
        rubric_version="stale-claim-rubric",
        rubric_snapshot=[],
        assessments=[],
    )
    stale_result = CandidateApplicationService._persist_screening(
        app_id,
        tenant.organization_id,
        "non-matching-lease-token",
        "some text",
        {"parsed": True},
        screening,
    )
    assert stale_result is None

    with SessionLocal() as session:
        app_rec = session.get(RecruitmentApplicationRecord, app_id)
        assert app_rec.status == "pending"


def test_replaying_and_worker_restart_are_strictly_idempotent(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    _, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)
    policy_windows = _future_local_slots(count=2)
    campaign_id = create_approved_campaign(
        client,
        tenant,
        campaign_automation={
            "response_deadline": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "interview_duration_minutes": 30,
            "timezone_name": "Asia/Ho_Chi_Minh",
            "proposed_windows": [
                {
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=30)).isoformat(),
                }
                for start in policy_windows
            ],
            "interviewer_user_id": tenant.recruiter["id"],
            "exclusive_resources": [],
        },
    )
    RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )

    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx("Senior developer with Python and Kubernetes."),
    )
    app_id = uploaded.json()["id"]
    processed = CandidateApplicationService.process_dispatched(app_id)
    assert processed is not None and processed.status == "needs_review"
    client.post(
        f"/api/v1/recruitment-applications/{app_id}/review",
        json={
            "criteria": [
                {"criterion_id": item["criterion_id"], "status": "confirmed"}
                for item in processed.screening["assessments"]
            ]
        },
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )

    worker = RecruitmentCampaignWorker()
    for _ in range(5):
        worker.dispatch_once()

    with SessionLocal() as session:
        comms = session.execute(
            select(CandidateCommunicationRecord).where(
                CandidateCommunicationRecord.application_id == app_id
            )
        ).scalars().all()
        assert len(comms) == 1
        comm_id = comms[0].id

    client.post(
        f"/api/v1/candidate-communications/{comm_id}/submit",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    client.post(
        f"/api/v1/candidate-communications/{comm_id}/approve",
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )

    for _ in range(5):
        worker.dispatch_once()

    with SessionLocal() as session:
        requests = session.execute(
            select(CandidateAvailabilityRequestRecord).where(
                CandidateAvailabilityRequestRecord.application_id == app_id
            )
        ).scalars().all()
        assert len(requests) == 1
        req = requests[0]
        proposed = json.loads(req.proposed_windows_json)

    zone = ZoneInfo(req.timezone_name)
    slot = proposed[0]
    client.post(
        f"/api/v1/candidate-availability-requests/{req.id}/responses",
        json={
            "idempotency_key": f"idemp-resp-{app_id[:16]}",
            "timezone_name": req.timezone_name,
            "windows": [
                {
                    "start": datetime.fromisoformat(slot["start"]).astimezone(zone).isoformat(),
                    "end": datetime.fromisoformat(slot["end"]).astimezone(zone).isoformat(),
                }
            ],
        },
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    )
    client.post(
        f"/api/v1/candidate-availability-requests/{req.id}/review",
        json={"preferred_window_indices": [0]},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )

    for _ in range(5):
        worker.dispatch_once()

    with SessionLocal() as session:
        schedules = session.execute(
            select(InterviewScheduleRecord).where(
                InterviewScheduleRecord.application_id == app_id
            )
        ).scalars().all()
        assert len(schedules) == 1


def test_failure_on_one_application_does_not_block_unrelated_applications(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    _, headers_a, _, _ = create_candidate_through_invitation(client, tenant)
    _, headers_b, _, _ = create_candidate_through_invitation(client, tenant)
    campaign_id = create_approved_campaign(client, tenant)
    RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )

    upload_a = submit_cv(client, tenant, headers_a, campaign_id, make_docx("Candidate A CV"))
    assert upload_a.status_code == 202
    app_a_id = upload_a.json()["id"]

    with SessionLocal.begin() as session:
        rec_a = session.get(RecruitmentApplicationRecord, app_a_id)
        rec_a.status = "needs_attention"
        rec_a.failure_code = "cv_corrupted"
        rec_a.failure_message = "CV parsing could not extract valid text"

    upload_b = submit_cv(client, tenant, headers_b, campaign_id, make_docx("Candidate B CV with Python experience"))
    assert upload_b.status_code == 202
    app_b_id = upload_b.json()["id"]

    worker = RecruitmentCampaignWorker()
    worker.dispatch_once()

    with SessionLocal() as session:
        rec_b = session.get(RecruitmentApplicationRecord, app_b_id)
        assert rec_b is not None and rec_b.status == "needs_review"
        rec_a = session.get(RecruitmentApplicationRecord, app_a_id)
        assert rec_a.status == "needs_attention"

    processed_b = CandidateApplicationService.get_application(
        app_b_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )
    reviewed_b = client.post(
        f"/api/v1/recruitment-applications/{app_b_id}/review",
        json={
            "criteria": [
                {"criterion_id": item["criterion_id"], "status": "confirmed"}
                for item in processed_b.screening["assessments"]
            ]
        },
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert reviewed_b.status_code == 200, reviewed_b.text

    worker.dispatch_once()
    with SessionLocal() as session:
        comm_b = session.execute(
            select(CandidateCommunicationRecord).where(
                CandidateCommunicationRecord.application_id == app_b_id
            )
        ).scalar_one_or_none()
        assert comm_b is not None
        assert comm_b.status == "draft"


def test_target_reached_prevents_dispatch_and_interview_starts(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    _, headers_1, _, _ = create_candidate_through_invitation(client, tenant)
    _, headers_2, _, _ = create_candidate_through_invitation(client, tenant)
    policy_windows = _future_local_slots(count=2)
    campaign_id = create_approved_campaign(
        client,
        tenant,
        campaign_automation={
            "response_deadline": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "interview_duration_minutes": 30,
            "timezone_name": "Asia/Ho_Chi_Minh",
            "proposed_windows": [
                {
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=30)).isoformat(),
                }
                for start in policy_windows
            ],
            "interviewer_user_id": tenant.recruiter["id"],
            "exclusive_resources": [],
        },
    )

    with SessionLocal.begin() as session:
        camp = session.get(RecruitmentCampaignRecord, campaign_id)
        obj = json.loads(camp.objective_json)
        obj["target_headcount"] = 1
        camp.objective_json = json.dumps(obj)

    RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )

    app_1, _, sched_1 = _prepare_candidate_with_worker(
        client, tenant, campaign_id, headers_1, slot_index=0
    )
    app_2, _, sched_2 = _prepare_candidate_with_worker(
        client, tenant, campaign_id, headers_2, slot_index=1
    )

    interview_1_id, _ = _complete_candidate_interview(
        client, tenant, campaign_id, app_1, sched_1, headers_1
    )
    _record_hire(client, tenant, campaign_id, app_1)

    progress = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}/progress",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    ).json()
    assert progress["objective_status"] == "target_achieved"
    assert progress["qualifying_hires"] == 1
    assert progress["remaining_hires"] == 0

    start_2 = client.post(
        f"/api/v1/interview/campaigns/{campaign_id}/applications/{app_2}/start",
        json={"schedule_id": sched_2["id"], "mode": "text"},
        headers=tenant_headers(headers_2, tenant.organization_id),
    )
    assert start_2.status_code == 409
    assert "Campaign hiring target has already been achieved" in start_2.text

    replay_1 = client.post(
        f"/api/v1/interview/campaigns/{campaign_id}/applications/{app_1}/start",
        json={"schedule_id": sched_1["id"], "mode": "text"},
        headers=tenant_headers(headers_1, tenant.organization_id),
    )
    assert replay_1.status_code == 200
    assert replay_1.json()["interview_id"] == interview_1_id

    assert CandidateApplicationService.discover_eligible(10) == []
    assert CandidateCommunicationService.discover_eligible(10) == []


def test_expired_deadline_and_terminal_outcomes_do_not_busy_loop(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    campaign_id = create_approved_campaign(client, tenant)
    execution = RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )
    assert execution.status == "awaiting_progress"

    past_date = (date.today() - timedelta(days=2)).isoformat()
    with SessionLocal.begin() as session:
        camp = session.get(RecruitmentCampaignRecord, campaign_id)
        obj = json.loads(camp.objective_json)
        obj["deadline"] = past_date
        camp.objective_json = json.dumps(obj)

    execution_id = _wake_execution(campaign_id)
    reconciled = RecruitmentCampaignOrchestrator.run_dispatched(execution_id)
    assert reconciled is not None
    assert reconciled.status == "needs_attention"
    assert reconciled.failure_code == "campaign_deadline_passed"
    assert reconciled.available_at is None

    eligible = RecruitmentCampaignOrchestrator.discover_eligible(10)
    assert execution_id not in eligible

    _, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)
    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx("Candidate for rejection"),
    )
    app_id = uploaded.json()["id"]
    # Outcomes require a human-reviewed screening first.
    processed = CandidateApplicationService.process_dispatched(app_id)
    assert processed is not None and processed.status == "needs_review"
    reviewed = client.post(
        f"/api/v1/recruitment-applications/{app_id}/review",
        json={
            "criteria": [
                {"criterion_id": item["criterion_id"], "status": "confirmed"}
                for item in processed.screening["assessments"]
            ]
        },
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert reviewed.status_code == 200, reviewed.text

    reject_res = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/applications/{app_id}/outcome",
        json={
            "outcome": "rejected",
            "criteria_satisfied": False,
            "idempotency_key": f"reject-{app_id[:20]}",
            "reason": "Does not meet required qualifications.",
        },
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert reject_res.status_code == 201
    assert RecruitmentCampaignOrchestrator.advance_application_once(app_id) is None


def test_non_qualifying_outcomes_do_not_count_toward_headcount_target(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    _, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)
    policy_windows = _future_local_slots(count=2)
    campaign_id = create_approved_campaign(
        client,
        tenant,
        campaign_automation={
            "response_deadline": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "interview_duration_minutes": 30,
            "timezone_name": "Asia/Ho_Chi_Minh",
            "proposed_windows": [
                {
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=30)).isoformat(),
                }
                for start in policy_windows
            ],
            "interviewer_user_id": tenant.recruiter["id"],
            "exclusive_resources": [],
        },
    )
    RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )

    app_id, _, sched = _prepare_candidate_with_worker(
        client, tenant, campaign_id, candidate_headers, slot_index=0
    )
    _complete_candidate_interview(
        client, tenant, campaign_id, app_id, sched, candidate_headers
    )

    progress = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}/progress",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    ).json()
    assert progress["qualifying_hires"] == 0
    assert progress["objective_status"] == "active"

    outcome_res = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/applications/{app_id}/outcome",
        json={
            "outcome": "hired",
            "criteria_satisfied": False,
            "idempotency_key": f"hire-unsatisfied-{app_id[:16]}",
            "reason": "Hired without satisfying criteria.",
        },
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert outcome_res.status_code == 201

    progress2 = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}/progress",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    ).json()
    assert progress2["qualifying_hires"] == 0
    assert progress2["remaining_hires"] == 2

