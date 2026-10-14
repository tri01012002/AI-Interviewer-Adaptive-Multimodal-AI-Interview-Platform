import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from tests.unit.tenant_test_utils import create_candidate_through_invitation, create_tenant


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_db_backed_auth_and_invitation_created_candidate_store(client: TestClient):
    register = client.post(
        "/api/v1/auth/register",
        json={"email": "admin-db@example.com", "password": "secret123", "role": "admin"},
    )
    assert register.status_code == 200

    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin-db@example.com", "password": "secret123"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    create_candidate = client.post(
        "/api/v1/candidates",
        json={
            "name": "Alice Candidate",
            "email": "admin-db@example.com",
            "phone": "0900",
            "resume_url": "https://example.com/a.pdf",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_candidate.status_code == 409

    tenant = create_tenant(client)
    candidate, candidate_headers, profile, _invitation = (
        create_candidate_through_invitation(client, tenant)
    )
    candidate_id = profile["candidate_id"]
    candidate_fetch = client.get(
        f"/api/v1/candidates/{candidate_id}",
        headers={
            **candidate_headers,
            "X-Organization-ID": tenant.organization_id,
        },
    )
    assert candidate_fetch.status_code == 200
    assert candidate_fetch.json()["email"] == candidate["email"]

    fetch = client.get(f"/api/v1/candidates/{candidate_id}", headers={"Authorization": f"Bearer {token}"})
    assert fetch.status_code == 403
