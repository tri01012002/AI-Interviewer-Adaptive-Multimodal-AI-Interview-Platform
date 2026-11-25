"""Candidate personal-data erasure: authorization, isolation, storage and failure handling."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import app
from services.candidate_application_service import CandidateApplicationService
from services.candidate_cv_storage import CandidateCVStorage
from services.candidate_data_deletion import (
    ERASED_TEXT,
    erased_candidate_email,
)
from services.database import (
    AccessAuditRecord,
    Base,
    CandidateCommunicationRecord,
    CandidateCriterionAssessmentRecord,
    CandidateCVDocumentRecord,
    CandidateInvitationRecord,
    CandidateRecord,
    CandidateScreeningRecord,
    InterviewRecord,
    InterviewTurnRecord,
    OrganizationMembershipRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignHiringOutcomeRecord,
    SessionLocal,
)
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    start_interview,
    tenant_headers,
)
from tests.unit.test_campaign_lifecycle import _setup_offline_env
from tests.unit.test_recruitment_candidate_intake import (
    create_approved_campaign,
    make_docx,
    submit_cv,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _setup_offline_env(monkeypatch, tmp_path)
    return tmp_path


@dataclass
class ErasableCandidate:
    tenant: object
    candidate_id: str
    candidate_headers: dict[str, str]
    candidate_user_id: str
    candidate_email: str
    application_id: str
    interview_id: str
    communication_id: str
    storage_key: str
    marker: str
    name: str


def _endpoint(candidate_id: str) -> str:
    return f"/api/v1/candidates/{candidate_id}/personal-data"


def _make_candidate(client: TestClient) -> ErasableCandidate:
    marker = f"PIIMARKER{uuid4().hex}"
    name = f"Synthetic Person {marker[-8:]}"
    tenant = create_tenant(client)
    user, candidate_headers, profile, _ = create_candidate_through_invitation(
        client, tenant, name=name
    )
    candidate_id = profile["candidate_id"]
    campaign_id = create_approved_campaign(client, tenant)
    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx(f"{marker} built Python services and Kubernetes clusters."),
    )
    assert uploaded.status_code == 202, uploaded.text
    application_id = uploaded.json()["id"]
    processed = CandidateApplicationService.process_dispatched(application_id)
    assert processed is not None and processed.status == "needs_review"

    interview_id = start_interview(client, tenant, candidate_id)["interview_id"]
    answered = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={
            "turn_id": f"erasure-{marker[-8:]}",
            "answer": f"{marker} I deployed Python and PyTorch models to production.",
        },
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    )
    assert answered.status_code == 200, answered.text

    now = datetime.now(UTC)
    communication_id = str(uuid4())
    with SessionLocal.begin() as session:
        storage_key = session.execute(
            select(CandidateCVDocumentRecord.storage_key).where(
                CandidateCVDocumentRecord.application_id == application_id
            )
        ).scalar_one()
        session.add(
            CandidateCommunicationRecord(
                id=communication_id,
                organization_id=tenant.organization_id,
                campaign_id=campaign_id,
                application_id=application_id,
                idempotency_key=f"erasure-{communication_id}",
                request_hash="0" * 64,
                template_type="status_update",
                template_version="builtin-v1",
                template_snapshot_json=json.dumps({"body_template": f"Hello {name}"}),
                recipient_email=user["email"],
                subject=f"Update for {name}",
                body=f"Dear {name}, {marker}",
                status="draft",
                created_by_user_id=tenant.recruiter["id"],
                attempt_count=0,
                max_attempts=3,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            RecruitmentCampaignHiringOutcomeRecord(
                organization_id=tenant.organization_id,
                campaign_id=campaign_id,
                application_id=application_id,
                objective_version=1,
                plan_version=1,
                outcome="rejected",
                criteria_satisfied=False,
                idempotency_key=f"erasure-outcome-{application_id}",
                request_hash="0" * 64,
                reason=f"Rejected {name} because {marker}",
                decided_by_user_id=tenant.org_admin["id"],
                decided_at=now,
            )
        )
    return ErasableCandidate(
        tenant=tenant,
        candidate_id=candidate_id,
        candidate_headers=candidate_headers,
        candidate_user_id=user["id"],
        candidate_email=user["email"],
        application_id=application_id,
        interview_id=interview_id,
        communication_id=communication_id,
        storage_key=storage_key,
        marker=marker,
        name=name,
    )


def _database_contains(*needles: str) -> list[str]:
    hits = []
    with SessionLocal() as session:
        for table in Base.metadata.sorted_tables:
            for row in session.execute(table.select()).all():
                text = " ".join("" if value is None else str(value) for value in row)
                for needle in needles:
                    if needle in text:
                        hits.append(f"{table.name}:{needle}")
    return hits


def _audit_rows(candidate_id: str) -> list[AccessAuditRecord]:
    with SessionLocal() as session:
        return list(
            session.execute(
                select(AccessAuditRecord)
                .where(
                    AccessAuditRecord.resource_type == "candidate",
                    AccessAuditRecord.resource_id == candidate_id,
                    AccessAuditRecord.action.like("candidate_personal_data_erasure_%"),
                )
                .order_by(AccessAuditRecord.created_at)
            ).scalars()
        )


def _assert_untouched(data: ErasableCandidate) -> None:
    assert CandidateCVStorage.exists(data.storage_key)
    with SessionLocal() as session:
        candidate = session.get(CandidateRecord, data.candidate_id)
        assert candidate.name == data.name
        assert candidate.user_id == data.candidate_user_id
        document = session.execute(
            select(CandidateCVDocumentRecord).where(
                CandidateCVDocumentRecord.application_id == data.application_id
            )
        ).scalar_one()
        assert data.marker in (document.extracted_text or "")
        turn = session.execute(
            select(InterviewTurnRecord).where(
                InterviewTurnRecord.interview_id == data.interview_id
            )
        ).scalar_one()
        assert data.marker in (turn.candidate_answer or "")
    assert _audit_rows(data.candidate_id) == []


def test_org_admin_erases_personal_data_files_and_free_text(
    client: TestClient, offline: Path
):
    data = _make_candidate(client)
    assert (offline / data.storage_key).is_file()
    tenant = data.tenant

    response = client.delete(
        _endpoint(data.candidate_id),
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["already_erased"] is False
    assert body["retryable"] is False
    assert body["pending_file_count"] == 0
    assert not (offline / data.storage_key).exists()
    assert list(offline.iterdir()) == []

    with SessionLocal() as session:
        candidate = session.get(CandidateRecord, data.candidate_id)
        assert candidate.name == ERASED_TEXT
        assert candidate.email == erased_candidate_email(data.candidate_id)
        assert candidate.phone is None and candidate.user_id is None
        membership = session.execute(
            select(OrganizationMembershipRecord).where(
                OrganizationMembershipRecord.organization_id == tenant.organization_id,
                OrganizationMembershipRecord.user_id == data.candidate_user_id,
            )
        ).scalar_one()
        assert membership.status == "revoked"
        invitation = session.execute(
            select(CandidateInvitationRecord).where(
                CandidateInvitationRecord.candidate_id == data.candidate_id
            )
        ).scalar_one()
        assert invitation.candidate_name is None
        assert invitation.email.endswith("@erased.invalid")
        document = session.execute(
            select(CandidateCVDocumentRecord).where(
                CandidateCVDocumentRecord.application_id == data.application_id
            )
        ).scalar_one()
        assert document.extracted_text is None
        assert document.parsed_output_json is None
        screening_id = session.execute(
            select(CandidateScreeningRecord.id).where(
                CandidateScreeningRecord.application_id == data.application_id
            )
        ).scalar_one()
        assessments = session.execute(
            select(CandidateCriterionAssessmentRecord).where(
                CandidateCriterionAssessmentRecord.screening_id == screening_id
            )
        ).scalars().all()
        assert assessments and all(item.evidence_json == "[]" for item in assessments)
        # Numeric scores are retained pending policy approval.
        assert any(item.score is not None for item in assessments)
        turn = session.execute(
            select(InterviewTurnRecord).where(
                InterviewTurnRecord.interview_id == data.interview_id
            )
        ).scalar_one()
        assert turn.candidate_answer == ERASED_TEXT
        interview = session.get(InterviewRecord, data.interview_id)
        state = json.loads(interview.state_json)
        assert state["personal_data_erased"] is True
        assert state["status"] == "completed"
        assert "history" not in state
        communication = session.get(CandidateCommunicationRecord, data.communication_id)
        assert communication.status == "cancelled"
        assert communication.body == ERASED_TEXT
        assert communication.recipient_email.endswith("@erased.invalid")
        outcome = session.execute(
            select(RecruitmentCampaignHiringOutcomeRecord).where(
                RecruitmentCampaignHiringOutcomeRecord.application_id
                == data.application_id
            )
        ).scalar_one()
        assert outcome.outcome == "rejected"
        assert outcome.reason == ERASED_TEXT

    assert _database_contains(data.marker, data.name) == []

    audits = _audit_rows(data.candidate_id)
    # Both rows can share a created_at tick (coarse clocks), so PostgreSQL may
    # return them in either order: compare the multiset, not the sequence.
    assert sorted((row.action, row.outcome) for row in audits) == [
        ("candidate_personal_data_erasure_completed", "completed"),
        ("candidate_personal_data_erasure_started", "pending"),
    ]
    for row in audits:
        assert row.actor_user_id == tenant.org_admin["id"]
        assert row.organization_id == tenant.organization_id
        values = " ".join(
            str(value)
            for value in (row.action, row.resource_id, row.outcome, row.support_reason)
        )
        assert data.marker not in values
        assert data.name not in values
        assert data.candidate_email not in values


def test_recruiter_candidate_and_platform_support_are_denied(
    client: TestClient, offline: Path
):
    data = _make_candidate(client)
    tenant = data.tenant
    support_headers = tenant_headers(
        tenant.platform_admin_headers, tenant.organization_id
    )
    support_headers["X-Support-Reason"] = "Synthetic support ticket"

    for headers in (
        tenant_headers(tenant.recruiter_headers, tenant.organization_id),
        tenant_headers(data.candidate_headers, tenant.organization_id),
        support_headers,
    ):
        response = client.delete(_endpoint(data.candidate_id), headers=headers)
        assert response.status_code == 403, response.text

    _assert_untouched(data)


def test_other_organization_admin_gets_not_found_and_data_is_untouched(
    client: TestClient, offline: Path
):
    data = _make_candidate(client)
    other = create_tenant(client, name="Other Synthetic Organization")

    own_context = client.delete(
        _endpoint(data.candidate_id),
        headers=tenant_headers(other.org_admin_headers, other.organization_id),
    )
    assert own_context.status_code == 404
    foreign_context = client.delete(
        _endpoint(data.candidate_id),
        headers=tenant_headers(other.org_admin_headers, data.tenant.organization_id),
    )
    assert foreign_context.status_code == 404
    missing = client.delete(
        _endpoint(str(uuid4())),
        headers=tenant_headers(data.tenant.org_admin_headers, data.tenant.organization_id),
    )
    assert missing.status_code == 404

    _assert_untouched(data)


def test_repeated_erasure_is_an_idempotent_success(client: TestClient, offline: Path):
    data = _make_candidate(client)
    headers = tenant_headers(data.tenant.org_admin_headers, data.tenant.organization_id)

    first = client.delete(_endpoint(data.candidate_id), headers=headers)
    second = client.delete(_endpoint(data.candidate_id), headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["already_erased"] is False
    assert second.json()["status"] == "completed"
    assert second.json()["already_erased"] is True
    with SessionLocal() as session:
        candidate = session.get(CandidateRecord, data.candidate_id)
        assert candidate.email == erased_candidate_email(data.candidate_id)
    assert sorted(row.outcome for row in _audit_rows(data.candidate_id)) == [
        "completed", "completed", "pending", "pending",
    ]


def test_file_deletion_failure_is_reported_pending_and_retry_completes(
    client: TestClient, offline: Path, monkeypatch: pytest.MonkeyPatch
):
    data = _make_candidate(client)
    headers = tenant_headers(data.tenant.org_admin_headers, data.tenant.organization_id)

    def failing_delete(storage_key: str) -> None:
        raise OSError("synthetic storage outage")

    with monkeypatch.context() as patch:
        patch.setattr(CandidateCVStorage, "delete", staticmethod(failing_delete))
        failed = client.delete(_endpoint(data.candidate_id), headers=headers)

    assert failed.status_code == 202, failed.text
    assert failed.json()["status"] == "deletion_pending"
    assert failed.json()["retryable"] is True
    assert failed.json()["pending_file_count"] == 1
    assert (offline / data.storage_key).is_file()
    with SessionLocal() as session:
        document = session.execute(
            select(CandidateCVDocumentRecord).where(
                CandidateCVDocumentRecord.application_id == data.application_id
            )
        ).scalar_one()
        # The DB is already redacted; the file pointer is kept for the retry.
        assert document.extracted_text is None
        assert document.storage_key == data.storage_key
        assert session.get(CandidateRecord, data.candidate_id).name == ERASED_TEXT
    outcomes = [(row.action, row.outcome) for row in _audit_rows(data.candidate_id)]
    assert ("candidate_personal_data_erasure_incomplete", "partial") in outcomes
    assert all(outcome != "completed" for _, outcome in outcomes)

    retried = client.delete(_endpoint(data.candidate_id), headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "completed"
    assert retried.json()["already_erased"] is False
    assert not (offline / data.storage_key).exists()
    final_outcomes = [row.outcome for row in _audit_rows(data.candidate_id)]
    assert final_outcomes.count("completed") == 1
    assert final_outcomes.count("partial") == 1


def test_erasure_stops_pending_cv_processing(client: TestClient, offline: Path):
    tenant = create_tenant(client)
    _, candidate_headers, profile, _ = create_candidate_through_invitation(client, tenant)
    campaign_id = create_approved_campaign(client, tenant)
    uploaded = submit_cv(client, tenant, candidate_headers, campaign_id)
    assert uploaded.status_code == 202
    application_id = uploaded.json()["id"]

    response = client.delete(
        _endpoint(profile["candidate_id"]),
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )

    assert response.status_code == 200, response.text
    assert CandidateApplicationService.process_dispatched(application_id) is None
    with SessionLocal() as session:
        application = session.get(RecruitmentApplicationRecord, application_id)
        assert application.status == "failed"
        assert application.failure_code == "candidate_data_erased"
    assert list(offline.iterdir()) == []


@pytest.mark.parametrize("in_flight", ["interview_turn", "communication_send"])
def test_erasure_is_blocked_while_candidate_work_is_in_flight(
    client: TestClient, offline: Path, in_flight: str
):
    data = _make_candidate(client)
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        if in_flight == "interview_turn":
            session.execute(
                update(InterviewTurnRecord)
                .where(InterviewTurnRecord.interview_id == data.interview_id)
                .values(status="processing", updated_at=now)
            )
            expected_reason = "interview_turn_in_progress"
        else:
            session.execute(
                update(CandidateCommunicationRecord)
                .where(CandidateCommunicationRecord.id == data.communication_id)
                .values(
                    status="sending",
                    approved_by_user_id=data.tenant.org_admin["id"],
                    approved_at=now,
                    attempt_count=1,
                    lease_token=str(uuid4()),
                    lease_expires_at=now,
                )
            )
            expected_reason = "communication_send_in_progress"

    response = client.delete(
        _endpoint(data.candidate_id),
        headers=tenant_headers(data.tenant.org_admin_headers, data.tenant.organization_id),
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "candidate_erasure_blocked",
        "reason": expected_reason,
    }
    assert CandidateCVStorage.exists(data.storage_key)
    with SessionLocal() as session:
        assert session.get(CandidateRecord, data.candidate_id).name == data.name
    assert [(row.action, row.outcome) for row in _audit_rows(data.candidate_id)] == [
        ("candidate_personal_data_erasure_blocked", "blocked")
    ]
