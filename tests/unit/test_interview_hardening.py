from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from services.database import InterviewQuestionRecord, InterviewTurnRecord, SessionLocal
from services.interview_service import InterviewService
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


def candidate_flow(client: TestClient, tenant=None):
    tenant = tenant or create_tenant(client)
    candidate, headers, profile, _invitation = create_candidate_through_invitation(
        client, tenant
    )
    return tenant, candidate, headers, profile["candidate_id"]


def test_registration_cannot_escalate_to_admin(client: TestClient):
    email = f"not-admin-{uuid4()}@example.com"
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123", "role": "admin"},
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "candidate"


def test_recruiter_cannot_submit_answers(client: TestClient):
    tenant, _candidate, _candidate_headers, candidate_id = candidate_flow(client)
    start = start_interview(client, tenant, candidate_id)
    response = client.post(
        f"/api/v1/interview/{start['interview_id']}/answer",
        json={"turn_id": "recruiter-turn", "answer": "I used Python."},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 403


def test_user_cannot_access_another_users_interview(client: TestClient):
    tenant, _owner, _owner_headers, candidate_id = candidate_flow(client)
    _other, other_headers, _other_profile, _invitation = (
        create_candidate_through_invitation(client, tenant)
    )
    start = start_interview(client, tenant, candidate_id)

    response = client.get(
        f"/api/v1/interview/{start['interview_id']}",
        headers=tenant_headers(other_headers, tenant.organization_id),
    )
    assert response.status_code == 404


def test_concurrent_duplicate_turns_create_one_record(client: TestClient):
    tenant, _candidate, candidate_headers, candidate_id = candidate_flow(client)
    headers = tenant_headers(candidate_headers, tenant.organization_id)
    start = start_interview(client, tenant, candidate_id)
    interview_id = start["interview_id"]
    request = {
        "turn_id": f"concurrent-turn-{uuid4()}",
        "answer": "I deployed Python to production.",
    }

    def submit():
        return client.post(
            f"/api/v1/interview/{interview_id}/answer",
            json=request,
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: submit(), range(2)))

    statuses = [response.status_code for response in responses]
    assert sorted(statuses) in ([200, 200], [200, 409])
    if 409 in statuses:
        assert "still processing" in next(
            response.json()["detail"]
            for response in responses
            if response.status_code == 409
        )
        replay = submit()
        assert replay.status_code == 200
    with SessionLocal() as session:
        turns = session.execute(
            select(InterviewTurnRecord).where(
                InterviewTurnRecord.interview_id == interview_id
            )
        ).scalars().all()
        questions = session.execute(
            select(InterviewQuestionRecord).where(
                InterviewQuestionRecord.interview_id == interview_id
            )
        ).scalars().all()
    assert len(turns) == 1
    assert len(questions) == 2
    assert len({question.question_text for question in questions}) == 2


def test_reusing_turn_id_with_different_answer_returns_conflict(client: TestClient):
    tenant, _candidate, candidate_headers, candidate_id = candidate_flow(client)
    headers = tenant_headers(candidate_headers, tenant.organization_id)
    interview_id = start_interview(client, tenant, candidate_id)["interview_id"]
    turn_id = f"turn-conflict-{uuid4()}"
    first = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={"turn_id": turn_id, "answer": "I used Python."},
        headers=headers,
    )
    second = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={"turn_id": turn_id, "answer": "I used Rust instead."},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 409
    with SessionLocal() as session:
        turns = session.execute(
            select(InterviewTurnRecord).where(
                InterviewTurnRecord.interview_id == interview_id
            )
        ).scalars().all()
    assert len(turns) == 1


def test_stale_processing_turn_can_be_retried(client: TestClient):
    tenant, _candidate, candidate_headers, candidate_id = candidate_flow(client)
    headers = tenant_headers(candidate_headers, tenant.organization_id)
    turn_id = f"recovery-turn-{uuid4()}"
    interview_id = start_interview(client, tenant, candidate_id)["interview_id"]
    first = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={"turn_id": turn_id, "answer": "I used Python."},
        headers=headers,
    )
    assert first.status_code == 200
    with SessionLocal.begin() as session:
        turn = session.execute(
            select(InterviewTurnRecord).where(
                InterviewTurnRecord.interview_id == interview_id
            )
        ).scalar_one()
        turn.status = "processing"
        turn.updated_at = datetime.now(UTC) - timedelta(hours=1)

    retry = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={"turn_id": turn_id, "answer": "I used Python."},
        headers=headers,
    )
    assert retry.status_code == 200


def test_processing_failure_is_marked_retryable(client: TestClient):
    tenant, candidate, _candidate_headers, candidate_id = candidate_flow(client)
    service = InterviewService()
    state = service.start_interview(
        candidate_id,
        "AI Engineer",
        owner_user_id=candidate["id"],
        owner_role="candidate",
        organization_id=tenant.organization_id,
    )
    turn_id = f"failed-turn-{uuid4()}"
    original = service.evaluator.evaluate_result
    service.evaluator.evaluate_result = lambda *args: (_ for _ in ()).throw(
        RuntimeError("provider down")
    )
    try:
        with pytest.raises(RuntimeError, match="provider down"):
            service.submit_answer(
                state["interview_id"],
                "I used Python.",
                turn_id,
                candidate["id"],
                "candidate",
                tenant.organization_id,
            )
    finally:
        service.evaluator.evaluate_result = original

    with SessionLocal() as session:
        turn = session.execute(
            select(InterviewTurnRecord).where(
                InterviewTurnRecord.turn_id == turn_id
            )
        ).scalar_one()
    assert turn.status == "failed_retryable"
