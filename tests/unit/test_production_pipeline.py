from fastapi.testclient import TestClient

from apps.api.main import app


client = TestClient(app)


def test_auth_and_candidate_flow():
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "secret123"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    create_candidate = client.post(
        "/api/v1/candidates",
        json={"name": "Jane Doe", "email": "jane@example.com", "phone": "123456", "resume_url": "https://example.com/resume.pdf"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_candidate.status_code == 200
    candidate_id = create_candidate.json()["id"]

    candidate = client.get(f"/api/v1/candidates/{candidate_id}", headers={"Authorization": f"Bearer {token}"})
    assert candidate.status_code == 200
    assert candidate.json()["email"] == "jane@example.com"


def test_interview_report_export_flow():
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "secret123"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    start = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": "candidate-abc", "position": "AI Engineer", "mode": "text"},
        headers={"Authorization": f"Bearer {token}"},
    )
    interview_id = start.json()["interview_id"]

    client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={"turn_id": "turn-report-1", "answer": "I have built Python and PyTorch models and deployed them to production."},
        headers={"Authorization": f"Bearer {token}"},
    )

    report = client.post(
        f"/api/v1/interview/{interview_id}/report",
        params={"format": "json"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert report.status_code == 200
    payload = report.json()
    assert payload["interview_id"] == interview_id
    assert "report_path" in payload
