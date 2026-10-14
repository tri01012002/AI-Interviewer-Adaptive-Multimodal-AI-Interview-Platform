from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.v1.schemas.recruitment import GeneratedJobDescription
from services.auth_service import issue_token
from services.job_description_service import JobDescriptionService
from services.user_store import UserStore


def create_user(role: str) -> tuple[dict, dict[str, str]]:
    email = f"{role}-{uuid4()}@example.com"
    user = UserStore.create(email, "secret123", role=role)
    return user, {"Authorization": f"Bearer {issue_token(email, role)}"}


def test_recruiter_can_create_and_read_owned_job() -> None:
    _, headers = create_user("recruiter")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs",
            json={
                "title": "Backend Engineer",
                "description": "Build reliable APIs.",
                "requirements": ["Python", "SQLAlchemy"],
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        job = response.json()
        assert job["status"] == "draft"
        assert job["requirements"] == ["Python", "SQLAlchemy"]

        fetched = client.get(f"/api/v1/jobs/{job['id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["title"] == "Backend Engineer"


def test_candidate_cannot_manage_jobs() -> None:
    _, headers = create_user("candidate")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs",
            json={"title": "Backend Engineer", "description": "Build APIs."},
            headers=headers,
        )
    assert response.status_code == 403


def test_admin_cannot_manage_jobs() -> None:
    _, headers = create_user("admin")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs",
            json={"title": "Backend Engineer", "description": "Build APIs."},
            headers=headers,
        )
    assert response.status_code == 403


def test_supervisor_approves_job_before_downstream_use() -> None:
    _, recruiter_headers = create_user("recruiter")
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/jobs",
            json={"title": "Backend Engineer", "description": "Build APIs."},
            headers=recruiter_headers,
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]
        assert created.json()["status"] == "draft"

        approved = client.post(
            f"/api/v1/jobs/{job_id}/approve",
            headers=recruiter_headers,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"
        assert approved.json()["approved_by"] == "supervisor-agent"

        fetched = client.get(f"/api/v1/jobs/{job_id}", headers=recruiter_headers)
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "approved"

        second_approval = client.post(
            f"/api/v1/jobs/{job_id}/approve",
            headers=recruiter_headers,
        )
        assert second_approval.status_code == 409


def test_candidate_cannot_request_supervisor_approval() -> None:
    _, recruiter_headers = create_user("recruiter")
    _, candidate_headers = create_user("candidate")
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/jobs",
            json={"title": "Backend Engineer", "description": "Build APIs."},
            headers=recruiter_headers,
        )
        job_id = created.json()["id"]
        response = client.post(
            f"/api/v1/jobs/{job_id}/approve",
            headers=candidate_headers,
        )
    assert response.status_code == 403


def test_job_description_service_persists_structured_provider_result(monkeypatch) -> None:
    class FakeProvider:
        def generate_structured(self, system_instruction, user_content, schema):
            assert schema is GeneratedJobDescription
            assert "Backend Engineer" in user_content
            return GeneratedJobDescription(
                title="Backend Engineer",
                description="Build reliable APIs.",
                requirements=["Python"],
            )

    persisted = {}

    def fake_create(payload, owner_user_id):
        persisted.update(payload)
        persisted["owner_user_id"] = owner_user_id
        return persisted

    monkeypatch.setattr("services.job_description_service.JobStore.create", fake_create)
    result = JobDescriptionService(provider=FakeProvider()).generate(
        {
            "title": "Backend Engineer",
            "hiring_request": "Need an API engineer",
            "requirements": ["Python"],
        },
        "recruiter-id",
    )

    assert result["owner_user_id"] == "recruiter-id"
    assert result["requirements"] == ["Python"]