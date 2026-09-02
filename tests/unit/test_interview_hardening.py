from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from services.auth_service import issue_token
from services.database import InterviewQuestionRecord, InterviewTurnRecord, SessionLocal
from services.interview_service import InterviewService
from services.user_store import UserStore


client = TestClient(app)


def register_and_login(email: str) -> dict[str, str]:
    client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_registration_cannot_escalate_to_admin():
    client.post(
        "/api/v1/auth/register",
        json={"email": "not-admin@example.com", "password": "password123", "role": "admin"},
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "not-admin@example.com", "password": "password123"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "candidate"


def test_recruiter_cannot_submit_answers():
    owner_headers = register_and_login("recruiter-owner@example.com")
    UserStore.create("recruiter@example.com", "password123", role="recruiter")
    recruiter = UserStore.get_by_email("recruiter@example.com")
    recruiter_headers = {"Authorization": f"Bearer {issue_token(recruiter['email'], 'recruiter')}"}
    start = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": "candidate-recruiter", "position": "AI Engineer"},
        headers=owner_headers,
    )
    response = client.post(
        f"/api/v1/interview/{start.json()['interview_id']}/answer",
        json={"turn_id": "recruiter-turn", "answer": "I used Python."},
        headers=recruiter_headers,
    )
    assert response.status_code == 403


def test_user_cannot_access_another_users_interview():
    owner_headers = register_and_login("owner@example.com")
    other_headers = register_and_login("other@example.com")
    start = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": "candidate-owned", "position": "AI Engineer"},
        headers=owner_headers,
    )
    interview_id = start.json()["interview_id"]

    response = client.get(f"/api/v1/interview/{interview_id}", headers=other_headers)
    assert response.status_code == 404


def test_concurrent_duplicate_turns_create_one_record():
    headers = register_and_login("concurrent@example.com")
    start = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": "candidate-concurrent", "position": "AI Engineer"},
        headers=headers,
    )
    interview_id = start.json()["interview_id"]
    request = {"turn_id": "concurrent-turn", "answer": "I deployed Python to production."}

    def submit() -> int:
        return client.post(
            f"/api/v1/interview/{interview_id}/answer", json=request, headers=headers
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: submit(), range(2)))

    assert statuses == [200, 200]
    with SessionLocal() as session:
        turns = session.execute(
            select(InterviewTurnRecord).where(InterviewTurnRecord.interview_id == interview_id)
        ).scalars().all()
        questions = session.execute(
            select(InterviewQuestionRecord).where(InterviewQuestionRecord.interview_id == interview_id)
        ).scalars().all()
    assert len(turns) == 1
    assert len(questions) == 2
    assert len({question.question_text for question in questions}) == 2


def test_stale_processing_turn_can_be_retried():
    headers = register_and_login("recovery@example.com")
    turn_id = f"recovery-turn-{uuid4()}"
    start = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": "candidate-recovery", "position": "AI Engineer"},
        headers=headers,
    )
    interview_id = start.json()["interview_id"]
    first = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={"turn_id": turn_id, "answer": "I used Python."},
        headers=headers,
    )
    assert first.status_code == 200
    with SessionLocal.begin() as session:
        turn = session.execute(
            select(InterviewTurnRecord).where(InterviewTurnRecord.interview_id == interview_id)
        ).scalar_one()
        turn.status = "processing"
        turn.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)

    retry = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={"turn_id": turn_id, "answer": "I used Python."},
        headers=headers,
    )
    assert retry.status_code == 200


def test_processing_failure_is_marked_retryable():
    service = InterviewService()
    turn_id = f"failed-turn-{uuid4()}"
    state = service.start_interview(f"candidate-failure-{uuid4()}", "AI Engineer", owner_user_id="missing-user")
    original = service.evaluator.evaluate_answer
    service.evaluator.evaluate_answer = lambda *args: (_ for _ in ()).throw(RuntimeError("provider down"))
    try:
        try:
            service.submit_answer(state["interview_id"], "I used Python.", turn_id, "missing-user")
        except RuntimeError:
            pass
    finally:
        service.evaluator.evaluate_answer = original

    with SessionLocal() as session:
        turn = session.execute(
            select(InterviewTurnRecord).where(InterviewTurnRecord.turn_id == turn_id)
        ).scalar_one()
    assert turn.status == "failed_retryable"