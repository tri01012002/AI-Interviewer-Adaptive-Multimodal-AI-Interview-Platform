from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from config import settings
from services import email_provider
from services.candidate_application_service import CandidateApplicationService
from services.candidate_communication_service import CandidateCommunicationService
from services.database import (
    CandidateCommunicationRecord,
    InterviewRecord,
    RecruitmentCampaignExecutionRecord,
    SessionLocal,
)
from services.recruitment_campaign_orchestrator import RecruitmentCampaignOrchestrator
from services.report_service import ReportService
from services.tenant_authorization import TenantContext
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    tenant_headers,
)
from tests.unit.test_candidate_communications import draft_payload
from tests.unit.test_interview_scheduling import (
    _candidate_response,
    _confirm,
    _future_local_slots,
    _proposal,
    _submit,
)
from tests.unit.test_recruitment_candidate_intake import (
    create_candidate_and_campaign,
    make_docx,
    submit_cv,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _prepare_candidate(
    client: TestClient,
    tenant,
    campaign_id: str,
    candidate_headers: dict[str, str],
    *,
    slot_offset_days: int = 0,
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
    communication = client.post(
        f"/api/v1/recruitment-applications/{application_id}/communications",
        json=draft_payload(f"campaign-email-{application_id[:12]}"),
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert communication.status_code == 201, communication.text
    communication_id = communication.json()["id"]
    for action, headers in (
        ("submit", tenant.recruiter_headers),
        ("approve", tenant.org_admin_headers),
    ):
        response = client.post(
            f"/api/v1/candidate-communications/{communication_id}/{action}",
            headers=tenant_headers(headers, tenant.organization_id),
        )
        assert response.status_code == 200, response.text

    delivered = CandidateCommunicationService.process_dispatched(communication_id)
    assert delivered is not None
    assert delivered.status == "simulated"
    assert delivered.delivery_status == "simulated"
    assert delivered.sent_at is None

    request_id = _candidate_response(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        application_id,
        starts=[
            item + timedelta(days=slot_offset_days)
            for item in _future_local_slots()
        ],
    )
    proposal = _proposal(
        client,
        tenant,
        campaign_id,
        application_id,
        request_id,
        key=f"campaign-schedule-{application_id[:12]}",
    )
    assert proposal.status_code == 201, proposal.text
    pending = _submit(client, tenant, proposal.json())
    assert pending.status_code == 200, pending.text

    not_yet_confirmed = client.post(
        f"/api/v1/interview/campaigns/{campaign_id}/applications/{application_id}/start",
        json={"schedule_id": pending.json()["id"], "mode": "text"},
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    )
    assert not_yet_confirmed.status_code == 409

    confirmed = _confirm(client, tenant, pending.json())
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed_internal"
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


def test_approved_campaign_recovers_across_intake_to_explicit_hiring_outcomes(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
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

    tenant, _, first_headers, _, campaign_id = (
        create_candidate_and_campaign(client)
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

    first_application, first_communication, first_schedule = _prepare_candidate(
        client,
        tenant,
        campaign_id,
        first_headers,
    )
    first_interview, first_report = _complete_candidate_interview(
        client,
        tenant,
        campaign_id,
        first_application,
        first_schedule,
        first_headers,
    )
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
    second_application, second_communication, second_schedule = _prepare_candidate(
        client,
        tenant,
        campaign_id,
        other_headers,
        slot_offset_days=1,
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
