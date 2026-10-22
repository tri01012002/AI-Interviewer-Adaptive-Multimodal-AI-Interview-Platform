"""Multi-process worker reliability on a disposable PostgreSQL database.

Workers run as separate spawned processes (see ``worker_harness``) and start
together behind a barrier, so claims really race on PostgreSQL row locks.
Assertions count side effects from the autocommit ledger, not only final
states. The module is skipped unless DATABASE_URL points at PostgreSQL; see
docs/TASK2_POSTGRES_MULTI_WORKER_VERIFICATION.md for how to run it.
"""

from __future__ import annotations

import json
import multiprocessing
import queue
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from apps.api.main import app
from config import settings
from services.candidate_application_service import CandidateApplicationService
from services.database import (
    AccessAuditRecord,
    CandidateCommunicationEventRecord,
    CandidateCommunicationRecord,
    CandidateScreeningRecord,
    InterviewScheduleRecord,
    InterviewScheduleReservationRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignExecutionRecord,
    RecruitmentCampaignExecutionStepRecord,
    RecruitmentCampaignRecord,
    SessionLocal,
    engine,
)
from services.recruitment_campaign_orchestrator import RecruitmentCampaignOrchestrator
from tests.postgres.worker_harness import CRASH_EXIT_CODE, LEDGER_TABLE, worker_main
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    tenant_headers,
)
from tests.unit.test_candidate_communications import (
    approve_communication,
    create_screened_application,
)
from tests.unit.test_interview_scheduling import (
    _full_schedule_flow,
    _future_local_slots,
    _second_application,
)
from tests.unit.test_recruitment_campaign_orchestration import (
    approve_campaign,
    create_campaign,
)
from tests.unit.test_recruitment_candidate_intake import (
    create_approved_campaign,
    make_docx,
    submit_cv,
)

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="multi-worker reliability tests need a disposable PostgreSQL DATABASE_URL",
)

SHORT_LEASE_SECONDS = 10


@pytest.fixture(scope="module")
def client():
    # No lifespan: these flows do not use Redis, so none is required.
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated_worker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Spawned workers read settings from the environment at import time.
    monkeypatch.setenv("CV_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("EMAIL_PROVIDER", "offline_simulation")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setattr(settings, "CV_STORAGE_PATH", str(tmp_path))
    with engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS reliability_probe"))
        connection.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} ("
                "id bigserial PRIMARY KEY, kind text NOT NULL, reference text NOT NULL, "
                "pid integer NOT NULL, created_at timestamptz NOT NULL DEFAULT now())"
            )
        )
        connection.execute(text(f"TRUNCATE {LEDGER_TABLE}"))


def side_effects(kind: str, reference: str) -> list[int]:
    """PIDs of the processes that performed the side effect, one per occurrence."""
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    f"SELECT pid FROM {LEDGER_TABLE} "
                    "WHERE kind = :kind AND reference = :reference ORDER BY id"
                ),
                {"kind": kind, "reference": reference},
            ).scalars()
        )


def run_workers(*tasks: tuple[str, dict], timeout: float = 120) -> list[dict]:
    """Start one spawned process per task behind a barrier and collect outcomes."""
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(tasks))
    results = context.Queue()
    processes = [
        context.Process(target=worker_main, args=(name, kwargs, barrier, results))
        for name, kwargs in tasks
    ]
    for process in processes:
        process.start()
    deadline = time.monotonic() + timeout
    for process in processes:
        process.join(max(0.0, deadline - time.monotonic()))
    for process in processes:
        if process.is_alive():
            process.kill()
            pytest.fail("worker process did not finish in time")
    reported: dict[int, tuple[str, object]] = {}
    while True:
        try:
            pid, state, outcome = results.get(timeout=1)
        except queue.Empty:
            break
        reported[pid] = (state, outcome)
    outcomes = []
    for process in processes:
        state, outcome = reported.get(process.pid, ("no_report", None))
        outcomes.append(
            {"pid": process.pid, "exitcode": process.exitcode, "state": state, "outcome": outcome}
        )
    errors = [item for item in outcomes if item["state"] == "error"]
    assert not errors, errors
    return outcomes


def after(task: tuple[str, dict], seconds: float) -> tuple[str, dict]:
    name, kwargs = task
    return name, {**kwargs, "start_after": seconds}


def wait_until(deadline: datetime) -> None:
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining > 0:
        time.sleep(remaining + 0.5)


def unique_llm_campaign(client: TestClient, tenant) -> tuple[str, str]:
    """Approved campaign whose missing job description requires one LLM call."""
    campaign_id = create_campaign(client, tenant, job_description="")
    brief = f"Hire a platform engineer (reliability probe {uuid4().hex[:12]})."
    with SessionLocal.begin() as session:
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        campaign.director_hiring_brief = brief
    approve_campaign(client, tenant, campaign_id)
    return campaign_id, brief


def communication_events(communication_id: str, event_type: str) -> int:
    with SessionLocal() as session:
        return session.scalar(
            select(func.count(CandidateCommunicationEventRecord.id)).where(
                CandidateCommunicationEventRecord.communication_id == communication_id,
                CandidateCommunicationEventRecord.event_type == event_type,
            )
        )


# ---------------------------------------------------------------------------
# Campaign execution and step claims
# ---------------------------------------------------------------------------


def test_competing_workers_create_and_claim_one_execution_and_call_llm_once(client):
    tenant = create_tenant(client)
    campaign_id, brief = unique_llm_campaign(client, tenant)

    run_workers(*[("dispatch_campaign", {"llm_delay": 1.0})] * 4)

    with SessionLocal() as session:
        executions = session.execute(
            select(RecruitmentCampaignExecutionRecord).where(
                RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
            )
        ).scalars().all()
        assert len(executions) == 1
        execution = executions[0]
        steps = session.execute(
            select(RecruitmentCampaignExecutionStepRecord).where(
                RecruitmentCampaignExecutionStepRecord.execution_id == execution.id
            )
        ).scalars().all()
        started_audits = session.scalar(
            select(func.count(AccessAuditRecord.id)).where(
                AccessAuditRecord.resource_id == execution.id,
                AccessAuditRecord.action == "campaign_execution_started",
            )
        )
    assert len(side_effects("llm_job_design", brief)) == 1
    assert execution.status == "waiting_for_approval"
    assert execution.attempt_count == 1
    assert len(steps) == 1 and steps[0].attempt_count == 1
    assert started_audits == 1


def test_expired_execution_lease_is_reclaimed_and_stale_worker_cannot_commit(
    client, monkeypatch
):
    monkeypatch.setenv("CAMPAIGN_WORKER_LEASE_SECONDS", str(SHORT_LEASE_SECONDS))
    tenant = create_tenant(client)
    campaign_id, brief = unique_llm_campaign(client, tenant)
    RecruitmentCampaignOrchestrator.discover_eligible(100)
    with SessionLocal() as session:
        execution_id = session.execute(
            select(RecruitmentCampaignExecutionRecord.id).where(
                RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
            )
        ).scalar_one()
        approved_plan_version = session.get(
            RecruitmentCampaignRecord, campaign_id
        ).plan_version

    # The first worker's provider call outlives its lease (no lease renewal
    # exists); the second worker reclaims once the lease has expired.
    stale, fresh = run_workers(
        ("run_execution", {"execution_id": execution_id, "llm_delay": SHORT_LEASE_SECONDS + 4}),
        after(("run_execution", {"execution_id": execution_id}), SHORT_LEASE_SECONDS + 1),
    )

    with SessionLocal() as session:
        execution = session.get(RecruitmentCampaignExecutionRecord, execution_id)
        step = session.execute(
            select(RecruitmentCampaignExecutionStepRecord).where(
                RecruitmentCampaignExecutionStepRecord.execution_id == execution_id
            )
        ).scalar_one()
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
    calls = side_effects("llm_job_design", brief)
    # Duplicate provider work is expected without lease renewal, but only the
    # reclaiming worker's result is committed.
    assert calls == [stale["pid"], fresh["pid"]]
    assert execution.attempt_count == 2 and step.attempt_count == 2
    assert campaign.job_description == f"Generated by worker process {fresh['pid']}."
    assert json.loads(step.output_json)["job_description"] == campaign.job_description
    assert campaign.plan_version == approved_plan_version + 1
    assert execution.status == "waiting_for_approval"


# ---------------------------------------------------------------------------
# Candidate application (screening) claims
# ---------------------------------------------------------------------------


def test_competing_workers_screen_one_application_once(client):
    tenant = create_tenant(client)
    _, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)
    campaign_id = create_approved_campaign(client, tenant)
    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx("Senior engineer built Python and Kubernetes services."),
    )
    assert uploaded.status_code == 202, uploaded.text
    application_id = uploaded.json()["id"]

    outcomes = run_workers(
        *[("process_application", {"application_id": application_id})] * 4
    )

    with SessionLocal() as session:
        application = session.get(RecruitmentApplicationRecord, application_id)
        results = session.scalar(
            select(func.count(CandidateScreeningRecord.id)).where(
                CandidateScreeningRecord.application_id == application_id
            )
        )
    assert application.status == "needs_review"
    assert application.attempt_count == 1
    assert results == 1
    assert [item["outcome"] for item in outcomes].count("needs_review") >= 1


# ---------------------------------------------------------------------------
# Candidate communication dispatch
# ---------------------------------------------------------------------------


def test_competing_workers_deliver_one_communication_once(client, tmp_path, monkeypatch):
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    communication_id = approve_communication(client, tenant, application_id, "pg-race-1")

    outcomes = run_workers(
        *[("deliver", {"communication_id": communication_id, "delay": 0.5})] * 4
    )

    with SessionLocal() as session:
        communication = session.get(CandidateCommunicationRecord, communication_id)
    assert len(side_effects("email", communication_id)) == 1
    assert communication.status == "sent" and communication.attempt_count == 1
    assert [item["outcome"] for item in outcomes].count("sent") == 1
    assert communication_events(communication_id, "communication_delivery_attempt_started") == 1
    assert communication_events(communication_id, "communication_delivery_attempt_completed") == 1


def test_worker_killed_between_send_and_commit_is_never_resent_or_reported_sent(
    client, tmp_path, monkeypatch
):
    monkeypatch.setenv("CAMPAIGN_WORKER_LEASE_SECONDS", str(SHORT_LEASE_SECONDS))
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    communication_id = approve_communication(client, tenant, application_id, "pg-crash-1")

    (crashed,) = run_workers(
        ("deliver", {"communication_id": communication_id, "crash_after_send": True})
    )
    assert crashed["exitcode"] == CRASH_EXIT_CODE
    with SessionLocal() as session:
        orphaned = session.get(CandidateCommunicationRecord, communication_id)
    assert orphaned.status == "sending"

    # While the dead worker's lease is still live nobody may touch the row.
    run_workers(*[("discover_and_deliver", {})] * 2)
    assert len(side_effects("email", communication_id)) == 1
    wait_until(orphaned.lease_expires_at)

    # After expiry, recovery reports an unknown outcome instead of resending.
    run_workers(*[("discover_and_deliver", {})] * 2)
    with SessionLocal() as session:
        recovered = session.get(CandidateCommunicationRecord, communication_id)
    assert len(side_effects("email", communication_id)) == 1
    assert recovered.status == "needs_attention"
    assert recovered.delivery_status == "unknown"
    assert recovered.failure_code == "delivery_outcome_unknown"
    assert recovered.sent_at is None
    assert communication_events(communication_id, "communication_lease_expired") == 1


def test_delivery_slower_than_lease_is_not_resent_and_stale_result_is_discarded(
    client, tmp_path, monkeypatch
):
    monkeypatch.setenv("CAMPAIGN_WORKER_LEASE_SECONDS", str(SHORT_LEASE_SECONDS))
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    communication_id = approve_communication(client, tenant, application_id, "pg-slow-1")

    slow, sweeper = run_workers(
        ("deliver", {"communication_id": communication_id, "delay": SHORT_LEASE_SECONDS + 3}),
        after(("discover_and_deliver", {}), SHORT_LEASE_SECONDS + 1),
    )

    with SessionLocal() as session:
        communication = session.get(CandidateCommunicationRecord, communication_id)
    assert len(side_effects("email", communication_id)) == 1
    # The provider accepted the message, but the stale lease holder may not
    # record it; the row stays an explicit unknown for human reconciliation.
    assert slow["outcome"] is None
    assert communication.status == "needs_attention"
    assert communication.delivery_status == "unknown"
    assert communication_events(communication_id, "communication_delivery_attempt_completed") == 0


def test_retry_after_retryable_failure_is_claimed_once(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPAIGN_WORKER_RETRY_BASE_SECONDS", "1")
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    communication_id = approve_communication(client, tenant, application_id, "pg-retry-1")

    (first,) = run_workers(
        (
            "deliver",
            {"communication_id": communication_id, "outcome": "retryable_not_accepted"},
        )
    )
    assert first["outcome"] == "retryable_failure"
    with SessionLocal() as session:
        waiting = session.get(CandidateCommunicationRecord, communication_id)
    wait_until(waiting.available_at)

    outcomes = run_workers(*[("deliver", {"communication_id": communication_id})] * 4)

    with SessionLocal() as session:
        communication = session.get(CandidateCommunicationRecord, communication_id)
    assert len(side_effects("email", communication_id)) == 2
    assert communication.status == "sent" and communication.attempt_count == 2
    assert [item["outcome"] for item in outcomes].count("sent") == 1


# ---------------------------------------------------------------------------
# Reservation and scheduling claims
# ---------------------------------------------------------------------------


def test_competing_confirmations_commit_one_reservation_set(client, tmp_path, monkeypatch):
    tenant, _, candidate_headers, campaign_id, first_application = (
        create_screened_application(client, tmp_path, monkeypatch)
    )
    slot = _future_local_slots()
    applications = [(candidate_headers, first_application)]
    for _ in range(2):
        _, headers, application_id = _second_application(client, tenant, campaign_id)
        applications.append((headers, application_id))
    schedules = [
        _full_schedule_flow(
            client,
            tenant,
            headers,
            campaign_id,
            application_id,
            starts=slot,
            key=f"pg-slot-{index}",
        )[1]
        for index, (headers, application_id) in enumerate(applications)
    ]

    # Three schedules want the same interviewer at the same time, and each
    # schedule is confirmed by two workers at once.
    outcomes = run_workers(
        *[
            (
                "confirm_schedule",
                {
                    "schedule_id": schedule["id"],
                    "version": schedule["version"],
                    "organization_id": tenant.organization_id,
                    "actor_user_id": tenant.recruiter["id"],
                },
            )
            for schedule in schedules
            for _ in range(2)
        ]
    )

    schedule_ids = [schedule["id"] for schedule in schedules]
    with SessionLocal() as session:
        statuses = session.execute(
            select(InterviewScheduleRecord.status).where(
                InterviewScheduleRecord.id.in_(schedule_ids)
            )
        ).scalars().all()
        reservations = session.scalar(
            select(func.count(InterviewScheduleReservationRecord.id)).where(
                InterviewScheduleReservationRecord.schedule_id.in_(schedule_ids)
            )
        )
        overlapping = session.execute(
            text(
                "SELECT count(*) FROM interview_schedule_reservations a "
                "JOIN interview_schedule_reservations b "
                "ON a.organization_id = b.organization_id "
                "AND a.resource_type = b.resource_type "
                "AND a.resource_key = b.resource_key "
                "AND a.schedule_id < b.schedule_id "
                "AND a.start_at < b.end_at AND b.start_at < a.end_at "
                "WHERE a.organization_id = :organization_id"
            ),
            {"organization_id": tenant.organization_id},
        ).scalar_one()
    assert statuses.count("confirmed_internal") == 1
    assert reservations == 2  # candidate + interviewer of the winning schedule
    assert overlapping == 0
    results = sorted(item["outcome"] for item in outcomes)
    assert results.count("confirmed_internal") == 2  # the winner and its replay
    assert results.count("http_409") == 4


# ---------------------------------------------------------------------------
# System-owned application handoffs
# ---------------------------------------------------------------------------


def test_competing_workers_create_one_invitation_draft_per_application(client):
    tenant = create_tenant(client)
    _, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)
    campaign_id = create_approved_campaign(client, tenant)
    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx("Senior engineer built Python and Kubernetes services."),
    )
    application_id = uploaded.json()["id"]
    processed = CandidateApplicationService.process_dispatched(application_id)
    reviewed = client.post(
        f"/api/v1/recruitment-applications/{application_id}/review",
        json={
            "criteria": [
                {"criterion_id": item["criterion_id"], "status": "confirmed"}
                for item in processed.screening["assessments"]
            ]
        },
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert reviewed.status_code == 200, reviewed.text

    outcomes = run_workers(
        *[("advance_application", {"application_id": application_id})] * 4
    )

    with SessionLocal() as session:
        drafts = session.execute(
            select(CandidateCommunicationRecord.id).where(
                CandidateCommunicationRecord.application_id == application_id,
                CandidateCommunicationRecord.template_type == "interview_invitation",
            )
        ).scalars().all()
    assert len(drafts) == 1
    assert communication_events(drafts[0], "communication_draft_created") == 1
    # Workers that arrive after the draft exists correctly find nothing to do.
    assert {item["outcome"] for item in outcomes} <= {"communication_draft_created", None}
    assert "communication_draft_created" in {item["outcome"] for item in outcomes}


# ---------------------------------------------------------------------------
# Real polling loops, restart and recovery
# ---------------------------------------------------------------------------


def _approved_backlog(client, tmp_path, monkeypatch, count: int) -> list[str]:
    tenant, _, _, campaign_id, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    application_ids = [application_id]
    for _ in range(count - 1):
        application_ids.append(_second_application(client, tenant, campaign_id)[2])
    return [
        approve_communication(client, tenant, item, f"pg-backlog-{item[:8]}")
        for item in application_ids
    ]


def test_two_worker_loops_drain_backlog_with_one_side_effect_each(
    client, tmp_path, monkeypatch
):
    communication_ids = _approved_backlog(client, tmp_path, monkeypatch, 4)
    tenant = create_tenant(client)
    campaign_id, brief = unique_llm_campaign(client, tenant)

    run_workers(*[("run_worker_loop", {"seconds": 8})] * 2)

    with SessionLocal() as session:
        statuses = session.execute(
            select(CandidateCommunicationRecord.status).where(
                CandidateCommunicationRecord.id.in_(communication_ids)
            )
        ).scalars().all()
        execution = session.execute(
            select(RecruitmentCampaignExecutionRecord).where(
                RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
            )
        ).scalar_one()
    assert statuses == ["sent"] * 4
    assert [len(side_effects("email", item)) for item in communication_ids] == [1] * 4
    assert len(side_effects("llm_job_design", brief)) == 1
    assert execution.status == "waiting_for_approval" and execution.attempt_count == 1


def test_worker_loop_restart_recovers_without_resending(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPAIGN_WORKER_LEASE_SECONDS", str(SHORT_LEASE_SECONDS))
    communication_ids = _approved_backlog(client, tmp_path, monkeypatch, 3)

    (crashed,) = run_workers(
        ("run_worker_loop", {"seconds": 30, "crash_after_send": True})
    )
    assert crashed["exitcode"] == CRASH_EXIT_CODE
    with SessionLocal() as session:
        orphaned = set(
            session.execute(
                select(CandidateCommunicationRecord.id).where(
                    CandidateCommunicationRecord.id.in_(communication_ids),
                    CandidateCommunicationRecord.status == "sending",
                )
            ).scalars()
        )
    assert orphaned

    # The replacement worker runs past the dead worker's lease.
    run_workers(("run_worker_loop", {"seconds": SHORT_LEASE_SECONDS + 5}))

    with SessionLocal() as session:
        final = dict(
            session.execute(
                select(
                    CandidateCommunicationRecord.id, CandidateCommunicationRecord.status
                ).where(CandidateCommunicationRecord.id.in_(communication_ids))
            ).all()
        )
    for communication_id in communication_ids:
        sends = len(side_effects("email", communication_id))
        if communication_id in orphaned:
            # Claimed by the dead worker (sent or not): reported unknown, never resent.
            assert final[communication_id] == "needs_attention" and sends <= 1
        else:
            assert final[communication_id] == "sent" and sends == 1
