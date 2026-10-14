from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    start_interview,
    tenant_headers,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_auth_and_candidate_invitation_flow(client: TestClient):
    tenant = create_tenant(client)
    candidate, candidate_headers, profile, _invitation = (
        create_candidate_through_invitation(client, tenant)
    )

    fetched = client.get(
        f"/api/v1/candidates/{profile['candidate_id']}",
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    )
    assert fetched.status_code == 200
    assert fetched.json()["email"] == candidate["email"]


def test_interview_report_export_flow(client: TestClient):
    tenant = create_tenant(client)
    candidate, candidate_headers, profile, _invitation = (
        create_candidate_through_invitation(client, tenant)
    )
    start = start_interview(client, tenant, profile["candidate_id"])
    interview_id = start["interview_id"]

    answer = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={
            "turn_id": f"turn-report-{uuid4()}",
            "answer": (
                "I have built Python and PyTorch models and deployed them to production."
            ),
        },
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    )
    assert answer.status_code == 200

    report = client.post(
        f"/api/v1/interview/{interview_id}/report",
        params={"format": "json"},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert report.status_code == 200
    payload = report.json()
    assert payload["interview_id"] == interview_id
    assert payload["report_file"].endswith(".json")
    assert payload["report"]["evaluation_status"] == "complete"
    assert "recruiter's independent hiring decision" in payload["report"]["decision_disclaimer"]
