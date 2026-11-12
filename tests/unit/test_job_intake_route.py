"""Test route POST /jobs/generate-from-request qua TestClient.

- DB: SQLite test (conftest forces usage of storage/test_*.db).
- Redis: fakeredis
- LLM: fake provider for auto testing; call GROQ only when RUN_LIVE_LLM=1.
"""

import os
from uuid import uuid4

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient
from pydantic import BaseModel

from apps.api.v1.schemas.job import JobDescription, JobRequestFromUser

URL = "/api/v1/jobs/generate-from-request"
FULL_BRIEF = {
    "title": "Python Developer",
    "must_have_skills": ["Python"],
    "location": "Hà Nội",
    "years_of_experience": 2,
    "job_type": "full-time",
    "salary_range": "20-30 triệu",
    "benefits": ["bảo hiểm"],
}


class StubProvider:
    """Trả dữ liệu cố định theo schema được yêu cầu."""

    def generate(self, system_instruction, user_content):
        return ""

    def generate_structured(self, system_instruction, user_content, schema: type[BaseModel]):
        if schema is JobRequestFromUser:
            return JobRequestFromUser(title="Python Developer", must_have_skills=["Python"], location="Hà Nội")
        if schema is JobDescription:
            return JobDescription(title="Python Developer", description="Build APIs.", requirements=["Python"])
        raise AssertionError(f"unexpected schema {schema}")


@pytest.fixture
def client(monkeypatch):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        redis.Redis, "from_url",
        classmethod(lambda cls, *a, **k: fakeredis.FakeRedis(server=server)),
    )
    from apps.api.main import app
    from services.database import init_db

    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def headers():
    from services.auth_service import issue_token
    from services.user_store import UserStore

    email = f"intake-{uuid4()}@example.com"
    UserStore.create(email, "secret123")
    return {"Authorization": f"Bearer {issue_token(email)}"}


@pytest.fixture
def stub_llm(monkeypatch):
    from apps.api.v1.routes import job as job_routes

    monkeypatch.setattr(job_routes.job_request_parser, "provider", StubProvider())
    monkeypatch.setattr(job_routes.jd_generation_service, "provider", StubProvider())


def test_requires_authentication(client):
    assert client.post(URL, json={"raw_text": "x"}).status_code == 401


def test_rejects_both_or_neither_input(client, headers):
    assert client.post(URL, json={}, headers=headers).status_code == 422
    both = {"raw_text": "x", "brief": FULL_BRIEF}
    assert client.post(URL, json=both, headers=headers).status_code == 422


def test_turn1_incomplete_returns_needs_input(client, headers, stub_llm):
    r = client.post(URL, json={"raw_text": "Cần tuyển Python dev ở Hà Nội"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "needs_input"
    assert body["job"] is None
    assert [m["field"] for m in body["missing"]] == [
        "years_of_experience", "job_type", "salary_range", "benefits",
    ]


def test_turn2_complete_brief_creates_draft_job(client, headers, stub_llm):
    r = client.post(URL, json={"brief": FULL_BRIEF}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created"
    assert body["job"]["status"] == "draft"
    fetched = client.get(f"/api/v1/jobs/{body['job']['id']}", headers=headers)
    assert fetched.status_code == 200


def test_additional_notes_accepted_in_brief(client, headers, stub_llm):
    brief = {**FULL_BRIEF, "additional_notes": ["Cần tiếng Anh tốt"]}
    r = client.post(URL, json={"brief": brief}, headers=headers)
    assert r.json()["status"] == "created"
    assert r.json()["brief"]["additional_notes"] == ["Cần tiếng Anh tốt"]


def test_zero_years_of_experience_is_valid(client, headers, stub_llm):
    r = client.post(URL, json={"brief": {**FULL_BRIEF, "years_of_experience": 0}}, headers=headers)
    assert r.json()["status"] == "created"


def test_llm_failure_returns_503(client, headers, monkeypatch):
    from apps.api.v1.routes import job as job_routes
    from integrations.llm_providers.provider import LLMProviderError

    class Broken(StubProvider):
        def generate_structured(self, *a, **k):
            raise LLMProviderError("boom")

    monkeypatch.setattr(job_routes.job_request_parser, "provider", Broken())
    r = client.post(URL, json={"raw_text": "x"}, headers=headers)
    assert r.status_code == 503


@pytest.mark.skipif(os.environ.get("RUN_LIVE_LLM") != "1", reason="set RUN_LIVE_LLM=1 to call Groq")
def test_live_llm_two_turn_flow(client, headers):
    r1 = client.post(URL, json={"raw_text": "Cần tuyển Python dev ở Hà Nội, biết FastAPI"}, headers=headers)
    assert r1.status_code == 200, r1.text
    brief = {**r1.json()["brief"], **{k: v for k, v in FULL_BRIEF.items() if k != "title"}}
    r2 = client.post(URL, json={"brief": brief}, headers=headers)
    assert r2.json()["status"] == "created"
