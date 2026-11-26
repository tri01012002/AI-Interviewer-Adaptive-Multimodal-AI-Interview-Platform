"""Opt-in real PostgreSQL/Redis 7 API and campaign-worker journey."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_RUNTIME_PROCESS_TESTS") != "1",
    reason="Set RUN_RUNTIME_PROCESS_TESTS=1 for the disposable PostgreSQL/Redis 7 process test",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_process(command: list[str], env: dict[str, str], root: Path):
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.terminate()
    try:
        process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        pytest.fail("A runtime process did not stop within its graceful-shutdown window")


def _wait_for(check, *, description: str, timeout: float = 60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.25)
    pytest.fail(f"Timed out waiting for {description}")


def test_real_api_and_worker_complete_recruitment_journey(  # noqa: C901
    tmp_path: Path,
) -> None:
    import httpx
    import redis
    from sqlalchemy import func, select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import IntegrityError

    from services.database import (
        AccessAuditRecord,
        CandidateAvailabilityRequestRecord,
        CandidateCommunicationRecord,
        CandidateCVDocumentRecord,
        InterviewAssessmentRecord,
        InterviewEvidenceRecord,
        InterviewScheduleRecord,
        InterviewTurnRecord,
        RecruitmentApplicationRecord,
        RecruitmentCampaignExecutionRecord,
        SessionLocal,
        engine,
    )
    from tests.unit.tenant_test_utils import (
        create_candidate_through_invitation,
        create_tenant,
        tenant_headers,
    )
    from tests.unit.test_e2e_recruitment_journey import _automation_policy
    from tests.unit.test_interview_scheduling import _confirm
    from tests.unit.test_recruitment_candidate_intake import (
        create_approved_campaign,
        make_docx,
        submit_cv,
    )

    root = Path(__file__).resolve().parents[2]
    database_url = os.environ["DATABASE_URL"]
    database_target = make_url(database_url)
    assert database_target.host in {"127.0.0.1", "localhost", "::1"}
    assert database_target.database and database_target.database.startswith("test_")
    redis_url = os.environ.get("REDIS_INTEGRATION_URL", "")
    assert redis_url, "Set REDIS_INTEGRATION_URL to a disposable Redis 7 service"
    redis_target = make_url(redis_url)
    assert redis_target.host in {"127.0.0.1", "localhost", "::1"}

    with engine.connect() as connection:
        pg_version = int(connection.execute(text("SHOW server_version_num")).scalar_one())
    assert 160000 <= pg_version < 170000, "Runtime integration requires PostgreSQL 16"

    redis_client = redis.Redis.from_url(redis_url)
    try:
        info = redis_client.info("server")
        assert int(info["redis_version"].split(".")[0]) == 7, (
            "Runtime integration requires Redis 7"
        )
    finally:
        redis_client.close()

    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert migration.returncode == 0, "Disposable PostgreSQL migration failed"
    with pytest.raises(IntegrityError):
        with SessionLocal.begin() as session:
            session.add(
                RecruitmentApplicationRecord(
                    id=f"constraint-{uuid4().hex}",
                    organization_id="missing-organization",
                    campaign_id="missing-campaign",
                    candidate_id="missing-candidate",
                )
            )

    storage_root = tmp_path / "runtime-storage"
    cv_root = storage_root / "private_cv"
    cv_root.mkdir(parents=True)
    base_env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "REDIS_URL": redis_url,
        "ENVIRONMENT": "test",
        "SECRET_KEY": f"test-only-{uuid4().hex}{uuid4().hex}",
        "ENABLE_DEV_ADMIN_BOOTSTRAP": "false",
        "LLM_PROVIDER": "none",
        "EMBEDDING_PROVIDER": "none",
        "EMAIL_PROVIDER": "offline_simulation",
        "VOICE_ENABLED": "true",
        "STORAGE_PATH": str(storage_root),
        "CV_STORAGE_PATH": str(cv_root),
        "CAMPAIGN_WORKER_POLL_SECONDS": "0.25",
        "REDIS_TIMEOUT_SECONDS": "1",
    }

    api_process: subprocess.Popen[bytes] | None = None
    worker_process: subprocess.Popen[bytes] | None = None
    api_port = _free_port()
    api_url = f"http://127.0.0.1:{api_port}"

    def start_api(api_env: dict[str, str]):
        return _start_process(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
            api_env,
            root,
        )

    def wait_api(client: httpx.Client, process: subprocess.Popen[bytes]) -> None:
        def ready():
            if process.poll() is not None:
                pytest.fail("API process exited before becoming ready")
            try:
                response = client.get("/health/ready")
                return response if response.status_code == 200 else None
            except httpx.HTTPError:
                return None

        assert _wait_for(ready, description="API PostgreSQL and Redis readiness").status_code == 200

    def start_worker():
        return _start_process(
            [sys.executable, "-m", "services.recruitment_campaign_worker", "run"],
            base_env,
            root,
        )

    def wait_worker(process: subprocess.Popen[bytes]) -> None:
        def healthy():
            if process.poll() is not None:
                pytest.fail("Campaign worker process exited")
            status = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "services.recruitment_campaign_worker",
                    "status",
                ],
                cwd=root,
                env=base_env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if status.returncode != 0:
                return None
            payload = json.loads(status.stdout)
            return payload if payload.get("healthy") and payload.get("ready") else None

        _wait_for(healthy, description="campaign worker heartbeat")

    try:
        api_process = start_api(base_env)
        with httpx.Client(base_url=api_url, timeout=15) as client:
            wait_api(client, api_process)
            tenant = create_tenant(client, "P151 Runtime Tenant")
            other_tenant = create_tenant(client, "P151 Other Tenant")
            recruiter = tenant_headers(
                tenant.recruiter_headers, tenant.organization_id
            )
            admin = tenant_headers(
                tenant.org_admin_headers, tenant.organization_id
            )
            _candidate, candidate_auth, _profile, _invitation = create_candidate_through_invitation(
                client,
                tenant,
                email=f"runtime-{uuid4().hex}@example.invalid",
                name=f"Synthetic Runtime Candidate {uuid4().hex[:8]}",
            )
            candidate_headers = tenant_headers(
                candidate_auth, tenant.organization_id
            )
            policy = _automation_policy(tenant)
            campaign_id = create_approved_campaign(
                client,
                tenant,
                campaign_automation=policy,
            )
            campaign_read = client.get(
                f"/api/v1/recruitment-campaigns/{campaign_id}",
                headers=tenant_headers(
                    other_tenant.recruiter_headers,
                    other_tenant.organization_id,
                ),
            )
            assert campaign_read.status_code == 404

            cv_bytes = make_docx(
                f"Synthetic backend candidate {uuid4().hex[:8]} with Python "
                "and Kubernetes production experience."
            )
            uploaded = submit_cv(
                client, tenant, candidate_auth, campaign_id, cv_bytes
            )
            replayed = submit_cv(
                client, tenant, candidate_auth, campaign_id, cv_bytes
            )
            assert uploaded.status_code == replayed.status_code == 202
            application_id = uploaded.json()["id"]
            assert replayed.json()["id"] == application_id
            with SessionLocal.begin() as session:
                application = session.get(
                    RecruitmentApplicationRecord, application_id
                )
                assert application is not None
                application.status = "running"
                application.attempt_count = 1
                application.lease_token = f"orphaned-{uuid4().hex}"
                application.lease_expires_at = datetime.now(UTC) - timedelta(
                    seconds=1
                )

            worker_process = start_worker()
            wait_worker(worker_process)

            def application_ready():
                with SessionLocal() as session:
                    application = session.get(
                        RecruitmentApplicationRecord, application_id
                    )
                    document = session.execute(
                        select(CandidateCVDocumentRecord).where(
                            CandidateCVDocumentRecord.application_id == application_id
                        )
                    ).scalar_one_or_none()
                    if (
                        application is not None
                        and application.status == "needs_review"
                        and document is not None
                        and document.parse_status == "parsed"
                    ):
                        return application, document
                return None

            _application, document = _wait_for(
                application_ready,
                description="worker CV parsing and deterministic screening",
            )
            assert document.extracted_text and "Python" in document.extracted_text
            with SessionLocal() as session:
                submitted_audits = session.scalar(
                    select(func.count(AccessAuditRecord.id)).where(
                        AccessAuditRecord.resource_id == application_id,
                        AccessAuditRecord.action == "candidate_cv_submitted",
                    )
                )
                screening_audits = session.scalar(
                    select(func.count(AccessAuditRecord.id)).where(
                        AccessAuditRecord.resource_id == application_id,
                        AccessAuditRecord.action == "candidate_cv_screened",
                    )
                )
            assert submitted_audits == 1
            assert screening_audits == 1

            _stop_process(worker_process)
            worker_process = start_worker()
            wait_worker(worker_process)
            time.sleep(1)
            with SessionLocal() as session:
                assert session.scalar(
                    select(func.count(AccessAuditRecord.id)).where(
                        AccessAuditRecord.resource_id == application_id,
                        AccessAuditRecord.action == "candidate_cv_screened",
                    )
                ) == 1

            application_view = client.get(
                f"/api/v1/recruitment-applications/{application_id}",
                headers=recruiter,
            )
            assert application_view.status_code == 200
            assessments = application_view.json()["screening"]["assessments"]
            reviewed = client.post(
                f"/api/v1/recruitment-applications/{application_id}/review",
                json={
                    "criteria": [
                        {
                            "criterion_id": item["criterion_id"],
                            "status": "confirmed",
                        }
                        for item in assessments
                    ]
                },
                headers=admin,
            )
            assert reviewed.status_code == 200

            def invitation_draft():
                with SessionLocal() as session:
                    return session.execute(
                        select(CandidateCommunicationRecord).where(
                            CandidateCommunicationRecord.application_id
                            == application_id,
                            CandidateCommunicationRecord.template_type
                            == "interview_invitation",
                        )
                    ).scalar_one_or_none()

            communication = _wait_for(
                invitation_draft,
                description="worker-created invitation draft",
            )
            communication_id = communication.id
            assert client.post(
                f"/api/v1/candidate-communications/{communication_id}/submit",
                headers=recruiter,
            ).status_code == 200
            assert client.post(
                f"/api/v1/candidate-communications/{communication_id}/approve",
                headers=admin,
            ).status_code == 200
            _stop_process(worker_process)
            worker_process = start_worker()
            wait_worker(worker_process)

            def delivery_complete():
                with SessionLocal() as session:
                    communication = session.get(
                        CandidateCommunicationRecord, communication_id
                    )
                    return (
                        communication
                        if communication is not None
                        and communication.delivery_status == "simulated"
                        else None
                    )

            communication = _wait_for(
                delivery_complete,
                description="offline worker communication delivery",
            )
            assert communication.attempt_count == 1
            with SessionLocal() as session:
                assert session.scalar(
                    select(func.count(AccessAuditRecord.id)).where(
                        AccessAuditRecord.resource_id == communication_id,
                        AccessAuditRecord.action
                        == "candidate_communication_delivery_simulated",
                    )
                ) == 1

            def availability_request():
                with SessionLocal() as session:
                    return session.execute(
                        select(CandidateAvailabilityRequestRecord).where(
                            CandidateAvailabilityRequestRecord.application_id
                            == application_id
                        )
                    ).scalar_one_or_none()

            availability = _wait_for(
                availability_request,
                description="worker-created availability request",
            )
            window = json.loads(availability.proposed_windows_json)[0]
            zone = ZoneInfo(availability.timezone_name)
            start = datetime.fromisoformat(window["start"]).astimezone(zone)
            end = datetime.fromisoformat(window["end"]).astimezone(zone)
            response = client.post(
                f"/api/v1/candidate-availability-requests/{availability.id}/responses",
                json={
                    "idempotency_key": f"p151-availability-{application_id}",
                    "timezone_name": availability.timezone_name,
                    "windows": [
                        {"start": start.isoformat(), "end": end.isoformat()}
                    ],
                },
                headers=candidate_headers,
            )
            assert response.status_code == 200, response.text
            reviewed_availability = client.post(
                f"/api/v1/candidate-availability-requests/{availability.id}/review",
                json={"preferred_window_indices": [0]},
                headers=recruiter,
            )
            assert reviewed_availability.status_code == 200

            def pending_schedule():
                with SessionLocal() as session:
                    return session.execute(
                        select(InterviewScheduleRecord).where(
                            InterviewScheduleRecord.application_id == application_id,
                            InterviewScheduleRecord.status == "pending_confirmation",
                        )
                    ).scalar_one_or_none()

            schedule = _wait_for(
                pending_schedule,
                description="worker-created interview schedule",
            )
            confirmed = _confirm(
                client,
                tenant,
                {"id": schedule.id, "version": schedule.version},
                headers=recruiter,
            )
            assert confirmed.status_code == 200
            start_endpoint = (
                f"/api/v1/interview/campaigns/{campaign_id}/applications/"
                f"{application_id}/start"
            )
            started = client.post(
                start_endpoint,
                json={"schedule_id": schedule.id, "mode": "text"},
                headers=candidate_headers,
            )
            assert started.status_code == 200, started.text
            interview_id = started.json()["interview_id"]
            state = started.json()
            answer_text = (
                "I built Python and Kubernetes services and improved production "
                "latency by 30 percent."
            )
            turns_created = 0
            for index in range(12):
                if state["status"] == "completed":
                    break
                turn_id = f"p151-runtime-turn-{index}-{uuid4().hex[:8]}"
                answer = client.post(
                    f"/api/v1/interview/{interview_id}/answer",
                    json={"turn_id": turn_id, "answer": answer_text},
                    headers=candidate_headers,
                )
                assert answer.status_code == 200, answer.text
                state = answer.json()
                turns_created += 1
                if index == 0:
                    replay = client.post(
                        f"/api/v1/interview/{interview_id}/answer",
                        json={"turn_id": turn_id, "answer": answer_text},
                        headers=candidate_headers,
                    )
                    assert replay.status_code == 200, replay.text
            assert state["status"] == "completed"

            with SessionLocal() as session:
                assert session.scalar(
                    select(func.count(InterviewTurnRecord.id)).where(
                        InterviewTurnRecord.interview_id == interview_id
                    )
                ) == turns_created
                assert session.scalar(
                    select(func.count(InterviewEvidenceRecord.id)).where(
                        InterviewEvidenceRecord.interview_id == interview_id
                    )
                ) > 0
                assert session.scalar(
                    select(func.count(InterviewAssessmentRecord.id)).where(
                        InterviewAssessmentRecord.interview_id == interview_id
                    )
                ) > 0

            report_response = client.post(
                f"/api/v1/interview/{interview_id}/report?format=json",
                headers=recruiter,
            )
            assert report_response.status_code == 200, report_response.text
            report_payload = report_response.json()
            report_path = storage_root / "reports" / tenant.organization_id / interview_id
            assert report_path.is_dir()
            assert report_payload["report"]["campaign"]["campaign_id"] == campaign_id

            outcome_payload = {
                "outcome": "hired",
                "criteria_satisfied": True,
                "idempotency_key": f"p151-hire-{application_id}",
                "reason": "Synthetic integration acceptance decision.",
            }
            outcome = client.post(
                f"/api/v1/recruitment-campaigns/{campaign_id}/applications/"
                f"{application_id}/outcome",
                json=outcome_payload,
                headers=admin,
            )
            assert outcome.status_code == 201, outcome.text
            outcome_replay = client.post(
                f"/api/v1/recruitment-campaigns/{campaign_id}/applications/"
                f"{application_id}/outcome",
                json=outcome_payload,
                headers=admin,
            )
            assert outcome_replay.status_code in {200, 201}

            with SessionLocal.begin() as session:
                execution = session.execute(
                    select(RecruitmentCampaignExecutionRecord).where(
                        RecruitmentCampaignExecutionRecord.campaign_id
                        == campaign_id
                    )
                ).scalar_one()
                execution.available_at = datetime.now(UTC) - timedelta(seconds=1)

            def target_progress():
                progress = client.get(
                    f"/api/v1/recruitment-campaigns/{campaign_id}/progress",
                    headers=recruiter,
                )
                if progress.status_code != 200:
                    return None
                data = progress.json()
                return (
                    data
                    if data["qualifying_hires"] == 1 and data["remaining_hires"] == 1
                    else None
                )

            progress = _wait_for(
                target_progress,
                description="worker-reconciled campaign target progress",
            )
            assert progress["target_headcount"] == 2

        _stop_process(api_process)
        api_process = None

        for fail_open, expected_status in ((True, 401), (False, 503)):
            unavailable_port = _free_port()
            unavailable_env = {
                **base_env,
                "REDIS_URL": f"redis://127.0.0.1:{unavailable_port}/0",
                "RATE_LIMIT_FAIL_OPEN": str(fail_open).lower(),
            }
            api_process = start_api(unavailable_env)
            try:
                with httpx.Client(base_url=api_url, timeout=15) as outage_client:
                    def live(process=api_process):
                        if process is None or process.poll() is not None:
                            pytest.fail("API failed to start without Redis")
                        try:
                            response = outage_client.get("/health/live")
                        except httpx.HTTPError:
                            return None
                        return response if response.status_code == 200 else None

                    _wait_for(
                        live,
                        description="API liveness with unavailable Redis",
                    )
                    readiness = outage_client.get("/health/ready")
                    assert readiness.status_code == 503
                    assert readiness.json()["checks"] == {
                        "database": True,
                        "redis": False,
                    }
                    login = outage_client.post(
                        "/api/v1/auth/login",
                        json={
                            "email": f"absent-{uuid4().hex}@example.invalid",
                            "password": "synthetic-invalid-password",
                        },
                    )
                    assert login.status_code == expected_status
            finally:
                _stop_process(api_process)
                api_process = None
    finally:
        if worker_process is not None:
            _stop_process(worker_process)
        if api_process is not None:
            _stop_process(api_process)
