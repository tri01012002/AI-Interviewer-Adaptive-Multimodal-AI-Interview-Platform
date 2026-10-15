from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from services.database import RecruitmentCampaignRecord, SessionLocal
from tests.unit.tenant_test_utils import (
    create_tenant,
    set_complete_campaign_objective,
    tenant_headers,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_recruitment_campaign_persists_and_resumes(client: TestClient):
    tenant = create_tenant(client)
    response = client.post(
        "/api/v1/recruitment-campaigns",
        json={
            "director_hiring_brief": "We need a senior backend engineer to lead the interview platform.",
            "structured_requirements": {"role": "backend engineer", "seniority": "senior"},
            "job_description": "Lead backend services and platform reliability.",
            "competency_rubric": [
                {
                    "criterion_id": "python",
                    "competency": "Python",
                    "evidence_terms": ["Python"],
                    "score_scale": {
                        "minimum": 0,
                        "maximum": 5,
                        "score_when_evidenced": 4,
                    },
                }
            ],
            "application_form_schema": {"type": "object", "fields": ["portfolio_url"]},
        },
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 201, response.text
    campaign_id = response.json()["id"]

    stored = None
    with SessionLocal() as session:
        stored = session.get(RecruitmentCampaignRecord, campaign_id)
        assert stored is not None
        assert stored.workflow_state == "brief_created"
        assert stored.approval_state == "draft"

    resumed = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["director_hiring_brief"].startswith("We need a senior backend engineer")

    locked = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/requirements",
        json={"role": "backend engineer", "experience_years": 6},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["workflow_state"] == "requirements_defined"

    job = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/job-description",
        json={"job_description": "Build APIs, own reliability, mentor engineers."},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert job.status_code == 200, job.text
    assert job.json()["workflow_state"] == "job_description_ready"

    rubric = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/rubric",
        json={
            "competency_rubric": [
                {
                    "criterion_id": "python",
                    "competency": "Python",
                    "evidence_terms": ["Python"],
                    "score_scale": {
                        "minimum": 0,
                        "maximum": 5,
                        "score_when_evidenced": 4,
                    },
                },
                {
                    "criterion_id": "architecture",
                    "competency": "Architecture",
                    "evidence_terms": ["architecture"],
                    "score_scale": {
                        "minimum": 0,
                        "maximum": 5,
                        "score_when_evidenced": 4,
                    },
                },
            ]
        },
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert rubric.status_code == 200, rubric.text
    assert rubric.json()["workflow_state"] == "rubric_ready"

    form = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/application-form",
        json={"application_form_schema": {"type": "object", "properties": {"portfolio_url": {"type": "string"}}}},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert form.status_code == 200, form.text
    set_complete_campaign_objective(client, tenant, campaign_id)
    assert form.json()["workflow_state"] == "application_form_ready"

    pending = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/submit-for-approval",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["approval_state"] == "pending_review"
    assert pending.json()["workflow_state"] == "pending_approval"


def test_recruitment_campaign_authorization_and_invalid_transitions(client: TestClient):
    tenant = create_tenant(client)
    response = client.post(
        "/api/v1/recruitment-campaigns",
        json={"director_hiring_brief": "A hiring brief for a platform engineer.", "structured_requirements": {"role": "platform engineer"}},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    campaign_id = response.json()["id"]

    bad = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/job-description",
        json={"job_description": "Should fail because requirements are not yet defined."},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert bad.status_code == 409

    other_user = client.post(
        "/api/v1/recruitment-campaigns",
        json={"director_hiring_brief": "Another brief for another org.", "structured_requirements": {"role": "analyst"}},
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    other_id = other_user.json()["id"]
    denied = client.get(
        f"/api/v1/recruitment-campaigns/{other_id}",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert denied.status_code == 200

    # Same-org recruiter should not access another recruiter-owned campaign.
    other_org = create_tenant(client)
    cross = client.post(
        "/api/v1/recruitment-campaigns",
        json={"director_hiring_brief": "Separate org brief.", "structured_requirements": {"role": "analyst"}},
        headers=tenant_headers(other_org.recruiter_headers, other_org.organization_id),
    )
    foreign = client.get(
        f"/api/v1/recruitment-campaigns/{cross.json()['id']}",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert foreign.status_code == 404


def test_recruitment_campaign_approval_and_recovery(client: TestClient):
    tenant = create_tenant(client)
    response = client.post(
        "/api/v1/recruitment-campaigns",
        json={"director_hiring_brief": "Brief for a product-minded data engineer.", "structured_requirements": {"role": "data engineer"}},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    campaign_id = response.json()["id"]

    client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/requirements",
        json={"role": "data engineer", "requirements": ["SQL", "Python"]},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/job-description",
        json={"job_description": "Lead the data platform backlog."},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/rubric",
        json={
            "competency_rubric": [
                {
                    "criterion_id": "sql",
                    "competency": "SQL",
                    "evidence_terms": ["SQL"],
                    "score_scale": {
                        "minimum": 0,
                        "maximum": 5,
                        "score_when_evidenced": 4,
                    },
                }
            ]
        },
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/application-form",
        json={"application_form_schema": {"type": "object", "properties": {"portfolio": {"type": "string"}}}},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    set_complete_campaign_objective(client, tenant, campaign_id)
    submitted = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/submit-for-approval",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert submitted.status_code == 200, submitted.text

    approved = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/approve",
        json={"approval_reason": "Meets the role brief"},
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval_state"] == "approved"
    assert approved.json()["workflow_state"] == "approved"

    recovered = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}",
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["approval_state"] == "approved"


def test_recruitment_campaign_reject_requires_reason_and_invalid_transition_is_blocked(client: TestClient):
    tenant = create_tenant(client)
    response = client.post(
        "/api/v1/recruitment-campaigns",
        json={"director_hiring_brief": "Pipeline planning for a product designer.", "structured_requirements": {"role": "designer"}},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    campaign_id = response.json()["id"]
    client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/requirements",
        json={"role": "designer"},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    rejected = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/reject",
        json={"approval_reason": "Not aligned with the approved scope."},
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert rejected.status_code == 409

    client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/job-description",
        json={"job_description": "Design product experiences."},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/rubric",
        json={
            "competency_rubric": [
                {
                    "criterion_id": "ux",
                    "competency": "User experience",
                    "evidence_terms": ["usability"],
                    "score_scale": {
                        "minimum": 0,
                        "maximum": 5,
                        "score_when_evidenced": 4,
                    },
                }
            ]
        },
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/application-form",
        json={"application_form_schema": {"type": "object", "properties": {"portfolio": {"type": "string"}}}},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    set_complete_campaign_objective(client, tenant, campaign_id)
    client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/submit-for-approval",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    rejected = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/reject",
        json={"approval_reason": "Scope mismatch"},
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["workflow_state"] == "rejected"
    assert rejected.json()["approval_reason"] == "Scope mismatch"
