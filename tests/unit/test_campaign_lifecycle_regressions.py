"""Regression tests for R5.4 campaign lifecycle defects.

Each test reproduces a defect found while stabilizing the lifecycle suite in
``test_campaign_lifecycle.py`` and reuses that suite's offline helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.main import app
from services.candidate_application_service import CandidateApplicationService
from services.candidate_screening import CandidateScreeningResult
from services.database import (
    CandidateCommunicationRecord,
    CandidateRecord,
    InterviewRecord,
    RecruitmentApplicationRecord,
    SessionLocal,
)
from services.recruitment_campaign_orchestrator import RecruitmentCampaignOrchestrator
from services.recruitment_campaign_worker import RecruitmentCampaignWorker
from services.tenant_authorization import TenantContext
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    tenant_headers,
)
from tests.unit.test_campaign_lifecycle import (
    _complete_candidate_interview,
    _prepare_candidate_with_worker,
    _record_hire,
    _setup_offline_env,
)
from tests.unit.test_interview_scheduling import _future_local_slots
from tests.unit.test_recruitment_candidate_intake import (
    create_approved_campaign,
    make_docx,
    submit_cv,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _automation_policy(tenant) -> dict:
    return {
        "response_deadline": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        "interview_duration_minutes": 30,
        "timezone_name": "Asia/Ho_Chi_Minh",
        "proposed_windows": [
            {
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=30)).isoformat(),
            }
            for start in _future_local_slots(count=2)
        ],
        "interviewer_user_id": tenant.recruiter["id"],
        "exclusive_resources": [],
    }


def _start_execution(campaign_id: str, tenant) -> None:
    execution = RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )
    assert execution.status == "awaiting_progress"


def _reviewed_application(client: TestClient, tenant, campaign_id: str, headers) -> str:
    uploaded = submit_cv(
        client,
        tenant,
        headers,
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
    return application_id


def _start_interview(client: TestClient, tenant, campaign_id, application_id, schedule, headers):
    return client.post(
        f"/api/v1/interview/campaigns/{campaign_id}/applications/{application_id}/start",
        json={"schedule_id": schedule["id"], "mode": "text"},
        headers=tenant_headers(headers, tenant.organization_id),
    )


def test_interview_start_replay_after_outcome_returns_existing_interview(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """F2: a recorded outcome must not turn an idempotent replay into a 409."""
    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    _, hired_headers, _, _ = create_candidate_through_invitation(client, tenant)
    _, rejected_headers, _, _ = create_candidate_through_invitation(client, tenant)
    campaign_id = create_approved_campaign(
        client, tenant, campaign_automation=_automation_policy(tenant)
    )
    _start_execution(campaign_id, tenant)

    hired_app, _, hired_schedule = _prepare_candidate_with_worker(
        client, tenant, campaign_id, hired_headers, slot_index=0
    )
    rejected_app, _, rejected_schedule = _prepare_candidate_with_worker(
        client, tenant, campaign_id, rejected_headers, slot_index=1
    )
    interview_id, _ = _complete_candidate_interview(
        client, tenant, campaign_id, hired_app, hired_schedule, hired_headers
    )
    _record_hire(client, tenant, campaign_id, hired_app)

    # Target is 2, so only the recorded outcome distinguishes these paths.
    replay = _start_interview(client, tenant, campaign_id, hired_app, hired_schedule, hired_headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["interview_id"] == interview_id

    rejected = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/applications/{rejected_app}/outcome",
        json={
            "outcome": "rejected",
            "criteria_satisfied": False,
            "idempotency_key": f"reject-{rejected_app[:20]}",
            "reason": "Does not meet required qualifications.",
        },
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert rejected.status_code == 201, rejected.text
    blocked = _start_interview(
        client, tenant, campaign_id, rejected_app, rejected_schedule, rejected_headers
    )
    assert blocked.status_code == 409
    assert "Application already has a recorded hiring outcome" in blocked.text

    with SessionLocal() as session:
        assert (
            session.scalar(
                select(func.count(InterviewRecord.id)).where(
                    InterviewRecord.campaign_id == campaign_id
                )
            )
            == 1
        )


def test_failing_application_handoff_does_not_block_other_applications(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """F4: one rejected handoff must not stop advancement of later applications."""
    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    _, broken_headers, _, _ = create_candidate_through_invitation(client, tenant)
    _, healthy_headers, _, _ = create_candidate_through_invitation(client, tenant)
    campaign_id = create_approved_campaign(client, tenant)
    _start_execution(campaign_id, tenant)

    broken_app = _reviewed_application(client, tenant, campaign_id, broken_headers)
    healthy_app = _reviewed_application(client, tenant, campaign_id, healthy_headers)
    with SessionLocal.begin() as session:
        application = session.get(RecruitmentApplicationRecord, broken_app)
        candidate = session.get(CandidateRecord, application.candidate_id)
        candidate_id = candidate.id
        original_email = candidate.email
        # Synthetic corrupt address: rejected by communication drafting (422).
        candidate.email = "broken..address@example.com"

    try:
        for _ in range(3):
            RecruitmentCampaignWorker().dispatch_once()

        with SessionLocal() as session:
            drafts = {
                application_id: session.scalar(
                    select(func.count(CandidateCommunicationRecord.id)).where(
                        CandidateCommunicationRecord.application_id == application_id
                    )
                )
                for application_id in (broken_app, healthy_app)
            }
            broken = session.get(RecruitmentApplicationRecord, broken_app)
            assert broken.status == "needs_review"
            assert broken.review_status == "reviewed"
        assert drafts == {broken_app: 0, healthy_app: 1}
    finally:
        # The suite shares one database; do not leave a corrupt row behind.
        with SessionLocal.begin() as session:
            session.get(CandidateRecord, candidate_id).email = original_email


def test_stale_screening_token_cannot_overwrite_live_application_claim(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """F1 companion: fencing holds while another worker owns a live lease."""
    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    _, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)
    campaign_id = create_approved_campaign(client, tenant)
    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx("Python software engineer."),
    )
    assert uploaded.status_code == 202, uploaded.text
    app_id = uploaded.json()["id"]
    live_expiry = datetime.now(UTC) + timedelta(seconds=60)
    with SessionLocal.begin() as session:
        application = session.get(RecruitmentApplicationRecord, app_id)
        application.status = "running"
        application.lease_token = "live-worker-token"
        application.lease_expires_at = live_expiry
        review_status_before = application.review_status

    screening = CandidateScreeningResult(
        rubric_version="stale-claim-rubric",
        rubric_snapshot=[],
        assessments=[],
    )
    stale = CandidateApplicationService._persist_screening(
        app_id,
        tenant.organization_id,
        "stale-worker-token",
        "synthetic text",
        {"parsed": True},
        screening,
    )
    assert stale is None
    with SessionLocal() as session:
        application = session.get(RecruitmentApplicationRecord, app_id)
        assert application.status == "running"
        assert application.lease_token == "live-worker-token"
        assert application.review_status == review_status_before
