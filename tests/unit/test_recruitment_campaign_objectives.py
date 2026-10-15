from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from services.database import (
    RecruitmentApplicationRecord,
    RecruitmentCampaignApprovalRecord,
    SessionLocal,
)
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    tenant_headers,
)

SCREENING_RUBRIC = [
    {
        "criterion_id": "java",
        "competency": "Java",
        "evidence_terms": ["Java"],
        "score_scale": {
            "minimum": 0,
            "maximum": 5,
            "score_when_evidenced": 4,
        },
    }
]


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _complete_plan(client: TestClient, tenant, *, target_headcount: int = 2) -> str:
    response = client.post(
        "/api/v1/recruitment-campaigns",
        json={
            "director_hiring_brief": "Hire senior Java engineers for the platform team.",
            "structured_requirements": {"role": "Senior Java Engineer"},
        },
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 201, response.text
    campaign_id = response.json()["id"]
    headers = tenant_headers(tenant.recruiter_headers, tenant.organization_id)

    steps = (
        (
            "requirements",
            {"role": "Senior Java Engineer", "requirements": ["Java"]},
        ),
        (
            "job-description",
            {"job_description": "Build and maintain Java platform services."},
        ),
        ("rubric", {"competency_rubric": SCREENING_RUBRIC}),
        (
            "application-form",
            {"application_form_schema": {"type": "object", "properties": {}}},
        ),
    )
    for endpoint, payload in steps:
        result = client.post(
            f"/api/v1/recruitment-campaigns/{campaign_id}/{endpoint}",
            json=payload,
            headers=headers,
        )
        assert result.status_code == 200, result.text
    _put_objective(
        client,
        tenant,
        campaign_id,
        objective=_objective(target_headcount=target_headcount),
    )
    return campaign_id


def _objective(
    *,
    target_headcount: int = 2,
    deadline: date | None = None,
    clarifications: list[dict] | None = None,
) -> dict:
    return {
        "requested_role": "Senior Java Engineer",
        "seniority": "Senior",
        "target_headcount": target_headcount,
        "deadline": (deadline or date.today() + timedelta(days=30)).isoformat(),
        "timezone_name": "UTC",
        "required_qualifications": ["Java"],
        "success_criteria": [
            "An authorized administrator confirms the candidate meets the approved hiring criteria."
        ],
        "clarifications": clarifications or [],
    }


def _put_objective(
    client: TestClient,
    tenant,
    campaign_id: str,
    *,
    objective: dict | None = None,
    expected_version: int = 1,
    idempotency_key: str | None = None,
):
    return client.put(
        f"/api/v1/recruitment-campaigns/{campaign_id}/objective",
        json={
            "expected_version": expected_version,
            "idempotency_key": idempotency_key or f"objective-{uuid4()}",
            "objective": objective or _objective(),
        },
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )


def _approve(client: TestClient, tenant, campaign_id: str) -> None:
    headers = tenant_headers(tenant.recruiter_headers, tenant.organization_id)
    submitted = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/submit-for-approval",
        headers=headers,
    )
    assert submitted.status_code == 200, submitted.text
    unauthorized_approval = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/approve",
        json={"approval_reason": "A recruiter cannot approve this plan."},
        headers=headers,
    )
    assert unauthorized_approval.status_code == 403
    approved = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/approve",
        json={"approval_reason": "Approved against the stated objective."},
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert approved.status_code == 200, approved.text


def test_incomplete_objective_blocks_submission_and_validation_rejects_invalid_values(
    client: TestClient,
):
    tenant = create_tenant(client)
    response = client.post(
        "/api/v1/recruitment-campaigns",
        json={"director_hiring_brief": "Hire a senior Java platform engineer."},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    campaign_id = response.json()["id"]
    headers = tenant_headers(tenant.recruiter_headers, tenant.organization_id)
    for endpoint, payload in (
        ("requirements", {"role": "Senior Java Engineer"}),
        ("job-description", {"job_description": "Build Java services."}),
        ("rubric", {"competency_rubric": SCREENING_RUBRIC}),
        ("application-form", {"application_form_schema": {"type": "object"}}),
    ):
        assert client.post(
            f"/api/v1/recruitment-campaigns/{campaign_id}/{endpoint}",
            json=payload,
            headers=headers,
        ).status_code == 200

    submitted = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/submit-for-approval",
        headers=headers,
    )
    assert submitted.status_code == 409
    assert "unresolved_questions" in submitted.json()["detail"]

    invalid = _objective(target_headcount=0)
    invalid["timezone_name"] = "Mars/Olympus"
    rejected = _put_objective(client, tenant, campaign_id, objective=invalid)
    assert rejected.status_code == 422


def test_objective_updates_are_versioned_idempotent_and_clarifications_replay_safely(
    client: TestClient,
):
    tenant = create_tenant(client)
    campaign_id = _complete_plan(client, tenant)
    objective = _objective(
        clarifications=[
            {
                "id": "work-mode",
                "question": "Is this role remote, hybrid, or onsite?",
            }
        ]
    )
    key = f"objective-{uuid4()}"
    first = _put_objective(
        client,
        tenant,
        campaign_id,
        objective=objective,
        expected_version=2,
        idempotency_key=key,
    )
    assert first.status_code == 200, first.text
    blocked = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/submit-for-approval",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert blocked.status_code == 409
    replay = _put_objective(
        client, tenant, campaign_id, objective=objective, idempotency_key=key
    )
    assert replay.status_code == 200
    assert replay.json()["objective_version"] == first.json()["objective_version"]

    resolution = {
        "expected_version": 3,
        "idempotency_key": f"resolve-{uuid4()}",
        "answer": "Hybrid, with two onsite days each week.",
    }
    path = (
        f"/api/v1/recruitment-campaigns/{campaign_id}/objective/"
        "clarifications/work-mode/resolve"
    )
    headers = tenant_headers(tenant.recruiter_headers, tenant.organization_id)
    resolved = client.post(path, json=resolution, headers=headers)
    assert resolved.status_code == 200, resolved.text
    replayed_resolution = client.post(path, json=resolution, headers=headers)
    assert replayed_resolution.status_code == 200, replayed_resolution.text
    assert replayed_resolution.json()["objective_version"] == 4

    stale = _put_objective(
        client,
        tenant,
        campaign_id,
        expected_version=3,
        idempotency_key=f"stale-{uuid4()}",
    )
    assert stale.status_code == 409
    assert replayed_resolution.json()["objective_complete"] is True


def test_approval_snapshots_objective_and_material_change_requires_reapproval(
    client: TestClient,
):
    tenant = create_tenant(client)
    campaign_id = _complete_plan(client, tenant)
    _approve(client, tenant, campaign_id)
    headers = tenant_headers(tenant.recruiter_headers, tenant.organization_id)

    changed = _objective()
    changed["requested_role"] = "Staff Java Engineer"
    response = _put_objective(
        client,
        tenant,
        campaign_id,
        objective=changed,
        expected_version=2,
        idempotency_key=f"material-change-{uuid4()}",
    )
    assert response.status_code == 200, response.text
    assert response.json()["approval_state"] == "draft"
    assert response.json()["approved_objective_version"] is None

    approvals = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}",
        headers=headers,
    )
    assert approvals.status_code == 200
    with SessionLocal() as session:
        history = session.execute(
            select(RecruitmentCampaignApprovalRecord).where(
                RecruitmentCampaignApprovalRecord.campaign_id == campaign_id
            )
        ).scalars().all()
        assert len(history) == 1
        assert history[0].decision == "approved"
        assert history[0].objective_version == 2


def test_progress_requires_explicit_authorized_hires_and_respects_deadline(
    client: TestClient,
):
    tenant = create_tenant(client)
    campaign_id = _complete_plan(client, tenant, target_headcount=2)
    _approve(client, tenant, campaign_id)
    candidate_ids = []
    for _ in range(2):
        _, _, profile, _ = create_candidate_through_invitation(client, tenant)
        candidate_ids.append(profile["candidate_id"])

    application_ids = [str(uuid4()), str(uuid4())]
    with SessionLocal.begin() as session:
        for application_id, candidate_id in zip(
            application_ids, candidate_ids, strict=True
        ):
            session.add(
                RecruitmentApplicationRecord(
                    id=application_id,
                    organization_id=tenant.organization_id,
                    campaign_id=campaign_id,
                    candidate_id=candidate_id,
                    status="needs_review",
                    review_status="reviewed",
                    reviewed_by_user_id=tenant.recruiter["id"],
                )
            )

    progress_url = f"/api/v1/recruitment-campaigns/{campaign_id}/progress"
    recruiter_headers = tenant_headers(
        tenant.recruiter_headers, tenant.organization_id
    )
    before = client.get(progress_url, headers=recruiter_headers)
    assert before.status_code == 200
    assert before.json()["objective_status"] == "active"
    assert before.json()["qualifying_hires"] == 0
    assert "candidate" not in str(before.json()).lower()

    outcome_url = (
        f"/api/v1/recruitment-campaigns/{campaign_id}/applications/"
        f"{application_ids[0]}/outcome"
    )
    outcome_payload = {
        "outcome": "hired",
        "criteria_satisfied": True,
        "idempotency_key": f"hire-{uuid4()}",
        "reason": "Administrator confirmed the approved hiring criteria.",
    }
    denied = client.post(
        outcome_url, json=outcome_payload, headers=recruiter_headers
    )
    assert denied.status_code == 403

    admin_headers = tenant_headers(
        tenant.org_admin_headers, tenant.organization_id
    )
    accepted = client.post(
        outcome_url, json=outcome_payload, headers=admin_headers
    )
    assert accepted.status_code == 201, accepted.text
    after_one = client.get(progress_url, headers=recruiter_headers).json()
    assert after_one["qualifying_hires"] == 1
    assert after_one["objective_status"] == "active"
    assert after_one["remaining_hires"] == 1

    second = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/applications/"
        f"{application_ids[1]}/outcome",
        json={
            **outcome_payload,
            "idempotency_key": f"hire-{uuid4()}",
        },
        headers=admin_headers,
    )
    assert second.status_code == 201, second.text
    completed = client.get(progress_url, headers=recruiter_headers).json()
    assert completed["objective_status"] == "target_achieved"
    assert completed["qualifying_hires"] == 2


def test_insufficient_hires_after_deadline_is_not_marked_complete(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = _complete_plan(client, tenant)
    current = client.put(
        f"/api/v1/recruitment-campaigns/{campaign_id}/objective",
        json={
            "expected_version": 2,
            "idempotency_key": f"past-deadline-{uuid4()}",
            "objective": _objective(deadline=date.today() - timedelta(days=1)),
        },
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert current.status_code == 200, current.text
    _approve(client, tenant, campaign_id)
    progress = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}/progress",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert progress.status_code == 200
    assert progress.json()["objective_status"] == "deadline_passed"
    assert progress.json()["qualifying_hires"] == 0
