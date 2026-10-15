from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agents.supervisor_agent import SupervisorAgent
from apps.api.main import app
from apps.api.v1.schemas.recruitment import JobDescription
from services.auth_service import issue_token
from services.jd_generation_service import JDGenerationService
from services.user_store import UserStore


def create_prompting_user() -> tuple[dict, dict[str, str]]:
    email = f"prompting-user-{uuid4()}@example.com"
    user = UserStore.create(email, "secret123")
    return user, {"Authorization": f"Bearer {issue_token(email)}"}


def test_prompting_user_can_create_and_read_owned_job() -> None:
    with TestClient(app) as client:
        _, headers = create_prompting_user()
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


def test_supervisor_approves_job_before_downstream_use() -> None:
    with TestClient(app) as client:
        _, user_headers = create_prompting_user()
        created = client.post(
            "/api/v1/jobs",
            json={"title": "Backend Engineer", "description": "Build APIs."},
            headers=user_headers,
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]
        assert created.json()["status"] == "draft"

        approved = client.post(
            f"/api/v1/jobs/{job_id}/approve",
            headers=user_headers,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"
        assert approved.json()["approved_by"] == "supervisor-agent"

        fetched = client.get(f"/api/v1/jobs/{job_id}", headers=user_headers)
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "approved"

        second_approval = client.post(
            f"/api/v1/jobs/{job_id}/approve",
            headers=user_headers,
        )
        assert second_approval.status_code == 409


def test_unauthenticated_user_cannot_request_supervisor_approval() -> None:
    with TestClient(app) as client:
        _, user_headers = create_prompting_user()
        created = client.post(
            "/api/v1/jobs",
            json={"title": "Backend Engineer", "description": "Build APIs."},
            headers=user_headers,
        )
        job_id = created.json()["id"]
        response = client.post(
            f"/api/v1/jobs/{job_id}/approve",
        )
    assert response.status_code == 401


def test_supervisor_graph_routes_subagent_task() -> None:
    calls = []
    agent = SupervisorAgent(
        agent_handlers={
            "browser-agent": lambda payload: calls.append(("browser", payload)) or "published",
        }
    )

    result = agent.route_task("publish_job", {"job_id": "job-1"})

    print("Result:", result)
    print("Handler calls:", calls)

    assert result == "published"
    assert calls == [("browser", {"job_id": "job-1"})]


def test_supervisor_graph_rejects_unknown_task() -> None:
    with pytest.raises(ValueError, match="Unsupported supervisor task"):
        SupervisorAgent().route_task("unknown", {})


def test_jd_generation_service_persists_structured_provider_result(monkeypatch) -> None:
    class FakeProvider:
        def generate_structured(self, system_instruction, user_content, schema):
            assert schema is JobDescription
            assert "Backend Engineer" in user_content
            return JobDescription(
                title="Backend Engineer",
                description="Build reliable APIs.",
                requirements=["Python"],
            )

    persisted = {}

    def fake_create(payload, owner_user_id):
        persisted.update(payload)
        persisted["owner_user_id"] = owner_user_id
        return persisted

    monkeypatch.setattr("services.jd_generation_service.JobStore.create", fake_create)
    result = JDGenerationService(provider=FakeProvider()).generate(
        {
            "title": "Backend Engineer",
            "hiring_request": "Need an API engineer",
            "requirements": ["Python"],
        },
        "prompting-user-id",
    )

    assert result["owner_user_id"] == "prompting-user-id"
    assert result["requirements"] == ["Python"]