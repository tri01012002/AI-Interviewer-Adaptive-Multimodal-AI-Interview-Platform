from fastapi.testclient import TestClient

from apps.api.main import app


client = TestClient(app)


def test_db_backed_auth_and_candidate_store():
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
        json={"name": "Alice Candidate", "email": "alice@example.com", "phone": "0900", "resume_url": "https://example.com/a.pdf"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_candidate.status_code == 200
    candidate_id = create_candidate.json()["id"]

    fetch = client.get(f"/api/v1/candidates/{candidate_id}", headers={"Authorization": f"Bearer {token}"})
    assert fetch.status_code == 200
    assert fetch.json()["email"] == "alice@example.com"
