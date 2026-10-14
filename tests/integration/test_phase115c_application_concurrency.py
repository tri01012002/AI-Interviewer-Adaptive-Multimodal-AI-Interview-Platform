from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from config import settings
from services.auth_service import issue_token
from services.database import (
    Base,
    CandidateRecord,
    InterviewAssessmentRecord,
    InterviewCompetencyStateRecord,
    InterviewEvidenceRecord,
    InterviewQuestionRecord,
    InterviewTurnRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    SessionLocal,
    engine,
)
from services.interview_service import (
    InterviewConflictError,
    InterviewForbiddenError,
    InterviewProcessingError,
    InterviewService,
    InterviewUnavailableError,
)
from services.user_store import UserStore

pytestmark = pytest.mark.integration


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", settings.REDIS_URL)


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", settings.DATABASE_URL)


def _prepare_database() -> None:
    Base.metadata.create_all(bind=engine)


def _stop_api_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _service_worker(
    interview_id: str,
    turn_id: str,
    user_id: str,
    organization_id: str,
    result_queue,
) -> None:
    import redis

    started = time.monotonic()
    process_id = os.getpid()
    client = redis.Redis.from_url(_redis_url(), decode_responses=True)
    try:
        service = InterviewService(redis_client=client)
        state = service.submit_answer(
            interview_id,
            "I deployed a Redis-backed Python service and measured latency.",
            turn_id,
            user_id,
            "candidate",
            organization_id,
        )
        result_queue.put({
            "pid": process_id,
            "outcome": "success",
            "lock_wait_or_rejection_seconds": round(time.monotonic() - started, 3),
            "state_interview_id": state.get("interview_id"),
        })
    except InterviewProcessingError as exc:
        result_queue.put({
            "pid": process_id,
            "outcome": "processing",
            "lock_wait_or_rejection_seconds": round(time.monotonic() - started, 3),
            "reason": str(exc),
        })
    except (InterviewConflictError, InterviewUnavailableError, InterviewForbiddenError) as exc:
        result_queue.put({
            "pid": process_id,
            "outcome": "error",
            "lock_wait_or_rejection_seconds": round(time.monotonic() - started, 3),
            "reason": str(exc),
        })
    except Exception as exc:
        result_queue.put({
            "pid": process_id,
            "outcome": "error",
            "lock_wait_or_rejection_seconds": round(time.monotonic() - started, 3),
            "reason": f"{type(exc).__name__}: {exc}",
        })
    finally:
        client.close()


def _query_counts(interview_id: str) -> dict[str, int]:
    with SessionLocal() as session:
        return {
            "turns": session.scalar(select(func.count(InterviewTurnRecord.id)).where(InterviewTurnRecord.interview_id == interview_id)) or 0,
            "questions": session.scalar(select(func.count(InterviewQuestionRecord.id)).where(InterviewQuestionRecord.interview_id == interview_id)) or 0,
            "assessments": session.scalar(select(func.count(InterviewAssessmentRecord.id)).where(InterviewAssessmentRecord.interview_id == interview_id)) or 0,
            "evidence": session.scalar(select(func.count(InterviewEvidenceRecord.id)).where(InterviewEvidenceRecord.interview_id == interview_id)) or 0,
            "competencies": session.scalar(select(func.count(InterviewCompetencyStateRecord.id)).where(InterviewCompetencyStateRecord.interview_id == interview_id)) or 0,
        }


def _assessment_rows(interview_id: str) -> list[tuple[str, str]]:
    with SessionLocal() as session:
        return list(session.execute(
            select(InterviewAssessmentRecord.turn_id, InterviewAssessmentRecord.competency)
            .where(InterviewAssessmentRecord.interview_id == interview_id)
        ).all())


def _tenant_candidate(prefix: str) -> tuple[dict, dict, str]:
    recruiter = UserStore.create(
        f"{prefix}-recruiter-{uuid4()}@example.com",
        "secret123",
        role="candidate",
    )
    user = UserStore.create(
        f"{prefix}-candidate-{uuid4()}@example.com",
        "secret123",
        role="candidate",
    )
    organization_id = str(uuid4())
    candidate_id = str(uuid4())
    with SessionLocal.begin() as session:
        session.add(
            OrganizationRecord(
                id=organization_id,
                name=f"Integration organization {prefix}",
                created_by_user_id=recruiter["id"],
            )
        )
        session.flush()
        session.add_all(
            [
                OrganizationMembershipRecord(
                    organization_id=organization_id,
                    user_id=recruiter["id"],
                    role="recruiter",
                ),
                OrganizationMembershipRecord(
                    organization_id=organization_id,
                    user_id=user["id"],
                    role="candidate",
                ),
                CandidateRecord(
                    id=candidate_id,
                    name="Synthetic Integration Candidate",
                    email=user["email"],
                    organization_id=organization_id,
                    owner_user_id=recruiter["id"],
                    user_id=user["id"],
                ),
            ]
        )
    return recruiter, user, organization_id


def test_cross_process_interview_service_commits_one_turn_and_replays_idempotently():
    _prepare_database()
    _recruiter, user, organization_id = _tenant_candidate("phase115c")
    with SessionLocal() as session:
        candidate = session.execute(
            select(CandidateRecord).where(CandidateRecord.user_id == user["id"])
        ).scalar_one()
    service = InterviewService()
    interview = service.start_interview(
        candidate.id,
        "backend engineer",
        owner_user_id=user["id"],
        owner_role="candidate",
        organization_id=organization_id,
    )
    interview_id = interview["interview_id"]
    turn_id = f"turn-{uuid4()}"

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_service_worker,
            args=(interview_id, turn_id, user["id"], organization_id, result_queue),
        )
        for _ in range(2)
    ]
    started = time.monotonic()
    process_exit_codes: list[int | None] = []
    try:
        for process in processes:
            process.start()
        results = [result_queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(30)
    finally:
        for process in processes:
            if process.pid is not None and process.is_alive():
                process.terminate()
        for process in processes:
            if process.pid is not None:
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
                process_exit_codes.append(process.exitcode)
                process.close()
        result_queue.cancel_join_thread()
        result_queue.close()
    elapsed = time.monotonic() - started

    assert all(exit_code == 0 for exit_code in process_exit_codes), results
    outcomes = [result["outcome"] for result in results]
    assert all(outcome in {"success", "processing"} for outcome in outcomes)
    assert outcomes.count("success") >= 1
    assert _query_counts(interview_id)["turns"] == 1
    assert _query_counts(interview_id)["questions"] == 2
    assessment_rows = _assessment_rows(interview_id)
    assert len(assessment_rows) == len(set(assessment_rows))
    assert len(assessment_rows) >= 1
    assert _query_counts(interview_id)["evidence"] >= 1
    assert _query_counts(interview_id)["competencies"] >= 1

    replay = service.submit_answer(
        interview_id,
        "I deployed a Redis-backed Python service and measured latency.",
        turn_id,
        user["id"],
        "candidate",
        organization_id,
    )
    assert replay["interview_id"] == interview_id
    assert _query_counts(interview_id)["turns"] == 1
    assert _query_counts(interview_id)["questions"] == 2
    assert _assessment_rows(interview_id) == assessment_rows

    next_turn = f"turn-{uuid4()}"
    service.submit_answer(
        interview_id,
        "I used PostgreSQL transactions and unique constraints.",
        next_turn,
        user["id"],
        "candidate",
        organization_id,
    )
    assert _query_counts(interview_id)["turns"] == 2
    print({"interview_id": interview_id, "turn_id": turn_id, "workers": results, "total_seconds": round(elapsed, 3)})


def test_interview_service_fails_closed_when_redis_is_unavailable():
    _prepare_database()
    _recruiter, user, organization_id = _tenant_candidate("phase115c-down")
    with SessionLocal() as session:
        candidate = session.execute(
            select(CandidateRecord).where(CandidateRecord.user_id == user["id"])
        ).scalar_one()
    service = InterviewService()
    interview = service.start_interview(
        candidate.id,
        "backend engineer",
        owner_user_id=user["id"],
        owner_role="candidate",
        organization_id=organization_id,
    )
    broken = InterviewService(redis_client=__import__("redis").Redis(host="127.0.0.1", port=6399, socket_connect_timeout=0.2))

    with pytest.raises(InterviewUnavailableError, match="distributed locking is unavailable"):
        broken.submit_answer(
            interview["interview_id"],
            "answer",
            f"turn-{uuid4()}",
            user["id"],
            "candidate",
            organization_id,
        )


@pytest.fixture
def running_api_processes():
    _prepare_database()
    ports = [8011, 8012]
    processes = []
    log_handles = []
    env = os.environ.copy()
    env["DATABASE_URL"] = _database_url()
    env["REDIS_URL"] = _redis_url()
    env["ENVIRONMENT"] = "test"
    for port in ports:
        log_path = os.path.join(tempfile.gettempdir(), f"phase115c-api-{port}.log")
        log_handle = open(log_path, "w", encoding="utf-8")
        log_handles.append(log_handle)
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        processes.append(process)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            exited = [process for process in processes if process.poll() is not None]
            if exited:
                logs = [handle.name for handle in log_handles]
                raise RuntimeError(f"API worker exited: {[process.returncode for process in exited]}; logs={logs}")
            if all(process.poll() is None for process in processes):
                try:
                    if all(httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=1).status_code == 200 for port in ports):
                        yield ports
                        return
                except httpx.HTTPError:
                    pass
            time.sleep(0.2)
        raise RuntimeError("API processes did not become healthy")
    finally:
        for process in processes:
            _stop_api_process(process)
        for handle in log_handles:
            handle.close()


def test_two_api_processes_coordinate_duplicate_answer(running_api_processes):
    _recruiter, user, organization_id = _tenant_candidate("phase115c-api")
    with SessionLocal() as session:
        candidate = session.execute(
            select(CandidateRecord).where(CandidateRecord.user_id == user["id"])
        ).scalar_one()
    token = issue_token(user["email"], user["role"])
    headers = {"Authorization": f"Bearer {token}"}
    headers["X-Organization-ID"] = organization_id
    start_payload = {"candidate_id": candidate.id, "position": "backend engineer", "mode": "text"}
    start = httpx.post(f"http://127.0.0.1:{running_api_processes[0]}/api/v1/interview/start", json=start_payload, headers=headers, timeout=30)
    assert start.status_code == 200, start.text
    interview_id = start.json()["interview_id"]
    turn_id = f"turn-{uuid4()}"
    payload = {"turn_id": turn_id, "answer": "I built a Python service with Redis coordination and measured latency."}

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda port: httpx.post(f"http://127.0.0.1:{port}/api/v1/interview/{interview_id}/answer", json=payload, headers=headers, timeout=60),
            running_api_processes,
        ))
    elapsed = time.monotonic() - started

    statuses = sorted(response.status_code for response in responses)
    assert statuses in ([200, 200], [200, 409])
    if 409 in statuses:
        conflict = next(response for response in responses if response.status_code == 409)
        assert "still processing" in conflict.json()["detail"]
    assert _query_counts(interview_id)["turns"] == 1
    assessment_rows = _assessment_rows(interview_id)
    assert len(assessment_rows) == len(set(assessment_rows))
    assert len(assessment_rows) >= 1
    assert _query_counts(interview_id)["evidence"] >= 1
    replay = httpx.post(
        f"http://127.0.0.1:{running_api_processes[0]}/api/v1/interview/{interview_id}/answer",
        json=payload,
        headers=headers,
        timeout=10,
    )
    assert replay.status_code == 200
    assert _query_counts(interview_id)["turns"] == 1
    print({"interview_id": interview_id, "turn_id": turn_id, "api_statuses": [response.status_code for response in responses], "total_seconds": round(elapsed, 3)})
