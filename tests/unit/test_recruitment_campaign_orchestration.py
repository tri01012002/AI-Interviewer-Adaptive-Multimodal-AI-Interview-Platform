from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from apps.api.main import app
from config.settings import Settings
from integrations.llm_providers.provider import (
    DeterministicMockProvider,
    LLMTimeoutError,
)
from services.database import (
    AccessAuditRecord,
    RecruitmentCampaignExecutionRecord,
    RecruitmentCampaignExecutionStepRecord,
    RecruitmentCampaignRecord,
    RecruitmentCampaignWorkerControlRecord,
    SessionLocal,
)
from services.recruitment_campaign_orchestrator import (
    JobDesignResult,
    RecruitmentCampaignOrchestrator,
)
from services.recruitment_campaign_worker import (
    RecruitmentCampaignWorker,
    set_worker_paused,
    worker_status,
)
from services.recruitment_campaign_worker import (
    main as worker_main,
)
from services.tenant_authorization import TenantContext
from tests.unit.tenant_test_utils import (
    create_tenant,
    set_complete_campaign_objective,
    tenant_headers,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def create_campaign(
    client: TestClient,
    tenant,
    *,
    job_description: str = "Build reliable platform services.",
) -> str:
    headers = tenant_headers(tenant.recruiter_headers, tenant.organization_id)
    created = client.post(
        "/api/v1/recruitment-campaigns",
        json={
            "director_hiring_brief": "Hire an engineer to improve platform reliability.",
            "structured_requirements": {"role": "platform engineer"},
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]
    set_complete_campaign_objective(client, tenant, campaign_id)
    steps = (
        (
            "requirements",
            {"role": "platform engineer", "skills": ["Python", "SQL"]},
        ),
        ("job-description", {"job_description": job_description}),
        (
            "rubric",
            {
                "competency_rubric": [
                    {
                        "criterion_id": "reliability",
                        "competency": "reliability",
                        "evidence_terms": ["reliable"],
                        "score_scale": {
                            "minimum": 0,
                            "maximum": 5,
                            "score_when_evidenced": 4,
                        },
                    }
                ]
            },
        ),
        (
            "application-form",
            {
                "application_form_schema": {
                    "type": "object",
                    "properties": {"portfolio": {"type": "string"}},
                }
            },
        ),
    )
    for endpoint, payload in steps:
        response = client.post(
            f"/api/v1/recruitment-campaigns/{campaign_id}/{endpoint}",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 200, response.text
    submitted = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/submit-for-approval",
        headers=headers,
    )
    assert submitted.status_code == 200, submitted.text
    return campaign_id


def approve_campaign(client: TestClient, tenant, campaign_id: str) -> dict[str, Any]:
    approved = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/approve",
        json={"approval_reason": "Plan reviewed by organization administrator."},
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert approved.status_code == 200, approved.text
    return approved.json()


def recruiter_context(tenant) -> TenantContext:
    return TenantContext(
        actor_user_id=tenant.recruiter["id"],
        organization_id=tenant.organization_id,
        role="recruiter",
    )


def dispatch_campaign(campaign_id: str, provider=None):
    execution_ids = RecruitmentCampaignOrchestrator.discover_eligible(100)
    with SessionLocal() as session:
        execution = (
            session.query(RecruitmentCampaignExecutionRecord)
            .filter_by(campaign_id=campaign_id)
            .one_or_none()
        )
        assert execution is not None
        assert execution.id in execution_ids
        execution_id = execution.id
    return RecruitmentCampaignOrchestrator.run_dispatched(
        execution_id,
        provider=provider,
    )


class FixtureProvider:
    provider_name = "test-fixture"
    model = "test-fixture"
    prompt_version = "test-fixture-v1"

    def __init__(self, *, block: bool = False, fail_once: bool = False) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.fail_once = fail_once
        self.calls = 0

    def generate(self, system_instruction: str, user_content: str) -> str:
        raise AssertionError("Job design must use the typed structured provider API")

    def generate_structured(self, system_instruction: str, user_content: str, schema):
        self.calls += 1
        self.entered.set()
        if self.block:
            assert self.release.wait(timeout=10)
        if self.fail_once and self.calls == 1:
            raise LLMTimeoutError("synthetic provider timeout")
        return schema.model_validate(
            {
                "job_description": (
                    "Test provider fixture: reviewed job description required."
                ),
            }
        )


class SlowFirstCallProvider(FixtureProvider):
    def __init__(self) -> None:
        super().__init__()
        self._call_lock = threading.Lock()

    def generate_structured(self, system_instruction: str, user_content: str, schema):
        with self._call_lock:
            self.calls += 1
            call_number = self.calls
        if call_number == 1:
            self.entered.set()
            assert self.release.wait(timeout=10)
        return schema.model_validate(
            {"job_description": f"Persisted result from provider call {call_number}."}
        )


def test_unapproved_campaign_cannot_start_execution(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    response = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/executions",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 409
    with SessionLocal() as session:
        assert (
            session.query(RecruitmentCampaignExecutionRecord)
            .filter_by(campaign_id=campaign_id)
            .count()
            == 0
        )


def test_approval_is_authorized_persisted_and_audited(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    headers = tenant_headers(tenant.recruiter_headers, tenant.organization_id)
    denied = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/approve",
        json={"approval_reason": "Not an administrator."},
        headers=headers,
    )
    assert denied.status_code == 403

    approved = approve_campaign(client, tenant, campaign_id)
    assert approved["approval_decided_by_user_id"] == tenant.org_admin["id"]
    assert approved["approval_decided_at"]
    with SessionLocal() as session:
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        audits = (
            session.query(AccessAuditRecord)
            .filter_by(
                action="campaign_approved",
                resource_type="recruitment_campaign",
                resource_id=campaign_id,
            )
            .all()
        )
        assert campaign is not None
        assert campaign.approval_decided_by_user_id == tenant.org_admin["id"]
        assert campaign.approval_decided_at is not None
        assert len(audits) == 1
        assert audits[0].actor_user_id == tenant.org_admin["id"]


def test_platform_support_campaign_read_uses_required_audit_preflight(
    client: TestClient,
):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    headers = tenant_headers(
        tenant.platform_admin_headers, tenant.organization_id
    )
    headers["X-Support-Reason"] = "Reviewing campaign setup at tenant request."
    response = client.get(
        f"/api/v1/recruitment-campaigns/{campaign_id}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        audit = (
            session.query(AccessAuditRecord)
            .filter_by(
                action="support_access_attempt",
                resource_type="recruitment_campaign",
                resource_id=campaign_id,
            )
            .one()
        )
        assert audit.actor_user_id == tenant.platform_admin["id"]
        assert audit.requested_organization_id == tenant.organization_id


def test_execution_reuses_plan_is_idempotent_and_tenant_scoped(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    approve_campaign(client, tenant, campaign_id)
    start_url = f"/api/v1/recruitment-campaigns/{campaign_id}/executions"
    headers = tenant_headers(tenant.recruiter_headers, tenant.organization_id)
    first = client.post(start_url, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "awaiting_progress"
    assert first.json()["steps"][0]["output_source"] == "existing"

    second = client.post(start_url, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]

    with SessionLocal() as session:
        run = session.get(RecruitmentCampaignExecutionRecord, first.json()["id"])
        steps = (
            session.query(RecruitmentCampaignExecutionStepRecord)
            .filter_by(execution_id=first.json()["id"])
            .all()
        )
        assert run is not None and run.status == "awaiting_progress"
        assert len(steps) == 2
        assert next(step for step in steps if step.step_key == "job_design").status == "completed"
        execution_audits = (
            session.query(AccessAuditRecord)
            .filter_by(
                resource_type="recruitment_campaign_execution",
                resource_id=first.json()["id"],
            )
            .all()
        )
        assert {
            audit.action for audit in execution_audits
        } >= {
            "campaign_execution_requested",
            "campaign_execution_started",
            "campaign_execution_awaiting_progress",
        }

    other_tenant = create_tenant(client)
    foreign = client.get(
        f"/api/v1/recruitment-campaign-executions/{first.json()['id']}",
        headers=tenant_headers(
            other_tenant.recruiter_headers, other_tenant.organization_id
        ),
    )
    assert foreign.status_code == 404


def test_restart_recovers_expired_persisted_step(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    approve_campaign(client, tenant, campaign_id)
    expired = datetime.now(UTC) - timedelta(minutes=5)
    with SessionLocal.begin() as session:
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        assert campaign is not None
        execution = RecruitmentCampaignExecutionRecord(
            campaign_id=campaign.id,
            organization_id=campaign.organization_id,
            requested_by_user_id=tenant.recruiter["id"],
            status="running",
            attempt_count=1,
            max_attempts=3,
            lease_token="lost-worker-lease",
            lease_expires_at=expired,
            created_at=expired,
            updated_at=expired,
        )
        session.add(execution)
        session.flush()
        session.add(
            RecruitmentCampaignExecutionStepRecord(
                execution_id=execution.id,
                step_key="job_design",
                status="running",
                attempt_count=1,
                input_json=RecruitmentCampaignOrchestrator._campaign_input(
                    campaign
                ).model_dump_json(),
                started_at=expired,
                created_at=expired,
                updated_at=expired,
            )
        )
        execution_id = execution.id

    recovered = dispatch_campaign(campaign_id)
    assert recovered is not None
    assert recovered.status == "awaiting_progress"
    assert recovered.attempt_count == 2
    assert recovered.steps[0].status == "completed"
    assert recovered.steps[0].attempt_count == 2

    persisted = RecruitmentCampaignOrchestrator.get(
        execution_id, recruiter_context(tenant)
    )
    assert persisted.status == "awaiting_progress"
    assert persisted.steps[0].output_source == "existing"


def test_worker_discovers_approved_campaign_without_resume_api(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    approve_campaign(client, tenant, campaign_id)

    execution = dispatch_campaign(campaign_id)
    assert execution is not None
    assert execution.campaign_id == campaign_id
    assert execution.status == "awaiting_progress"
    assert execution.steps[0].output_source == "existing"
    with SessionLocal() as session:
        assert (
            session.query(RecruitmentCampaignExecutionRecord)
            .filter_by(campaign_id=campaign_id)
            .count()
            == 1
        )
        assert (
            session.query(AccessAuditRecord)
            .filter_by(
                action="campaign_execution_dispatched",
                resource_id=execution.id,
            )
            .count()
            == 1
        )


def test_running_worker_loop_autonomously_dispatches_and_shuts_down(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    approve_campaign(client, tenant, campaign_id)

    def target_execution_ids(limit: int) -> list[str]:
        with SessionLocal() as session:
            execution_id = session.execute(
                select(RecruitmentCampaignExecutionRecord.id).where(
                    RecruitmentCampaignExecutionRecord.campaign_id == campaign_id,
                    RecruitmentCampaignExecutionRecord.status.in_(
                        ("pending", "retryable_failure", "awaiting_progress")
                    ),
                )
            ).scalar_one_or_none()
        return [] if execution_id is None or limit < 1 else [execution_id]

    monkeypatch.setattr(
        RecruitmentCampaignOrchestrator,
        "_eligible_execution_ids",
        staticmethod(target_execution_ids),
    )
    stop_event = threading.Event()
    worker = RecruitmentCampaignWorker(
        concurrency=1,
        poll_seconds=0.05,
        stop_event=stop_event,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    deadline = datetime.now(UTC) + timedelta(seconds=15)
    try:
        while datetime.now(UTC) < deadline:
            with SessionLocal() as session:
                execution = session.execute(
                    select(RecruitmentCampaignExecutionRecord).where(
                        RecruitmentCampaignExecutionRecord.campaign_id
                        == campaign_id
                    )
                ).scalar_one_or_none()
                if execution is not None and execution.status == "awaiting_progress":
                    break
            thread.join(timeout=0.05)
    finally:
        stop_event.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    with SessionLocal() as session:
        execution = session.execute(
            select(RecruitmentCampaignExecutionRecord).where(
                RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
            )
        ).scalar_one()
        assert execution.status == "awaiting_progress"


def test_recovery_after_persisted_result_does_not_repeat_provider(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    approve_campaign(client, tenant, campaign_id)
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        assert campaign is not None
        execution = RecruitmentCampaignExecutionRecord(
            campaign_id=campaign.id,
            organization_id=campaign.organization_id,
            requested_by_user_id=tenant.recruiter["id"],
            status="running",
            attempt_count=1,
            max_attempts=3,
            lease_token="expired-result-lease",
            lease_expires_at=now - timedelta(minutes=5),
            created_at=now - timedelta(minutes=5),
            updated_at=now - timedelta(minutes=5),
        )
        session.add(execution)
        session.flush()
        result = JobDesignResult(
            structured_requirements={"role": "platform engineer"},
            job_description="Build reliable platform services.",
            competency_rubric=[{"competency": "reliability", "weight": 1.0}],
            application_form_schema={
                "type": "object",
                "properties": {"portfolio": {"type": "string"}},
            },
            output_source="existing",
            changed=False,
        )
        session.add(
            RecruitmentCampaignExecutionStepRecord(
                execution_id=execution.id,
                step_key="job_design",
                status="completed",
                attempt_count=1,
                input_json=RecruitmentCampaignOrchestrator._campaign_input(
                    campaign
                ).model_dump_json(),
                output_json=result.model_dump_json(),
                output_source="existing",
                completed_at=now - timedelta(minutes=5),
                created_at=now - timedelta(minutes=5),
                updated_at=now - timedelta(minutes=5),
            )
        )
        execution_id = execution.id

    provider = FixtureProvider()
    recovered = RecruitmentCampaignOrchestrator.run_dispatched(
        execution_id,
        provider=provider,
    )

    assert recovered is not None
    assert recovered.status == "awaiting_progress"
    assert recovered.steps[0].output_source == "existing"
    assert provider.calls == 0


def test_expired_worker_cannot_commit_after_another_worker_reclaims(
    client: TestClient,
):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    provider = SlowFirstCallProvider()
    context = recruiter_context(tenant)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stale_future = pool.submit(
            RecruitmentCampaignOrchestrator.start,
            campaign_id,
            context,
            provider,
        )
        assert provider.entered.wait(timeout=10)
        with SessionLocal.begin() as session:
            execution = session.execute(
                select(RecruitmentCampaignExecutionRecord).where(
                    RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
                )
            ).scalar_one()
            old_token = execution.lease_token
            execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

        with SessionLocal() as session:
            execution_id = session.execute(
                select(RecruitmentCampaignExecutionRecord.id).where(
                    RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
                )
            ).scalar_one()
        reclaimed = RecruitmentCampaignOrchestrator.run_dispatched(
            execution_id,
            provider=provider,
        )
        assert reclaimed is not None
        assert reclaimed.status == "waiting_for_approval"
        assert reclaimed.steps[0].output["job_description"].endswith("call 2.")

        provider.release.set()
        stale_result = stale_future.result(timeout=10)

    assert stale_result.status == "waiting_for_approval"
    assert provider.calls == 2
    with SessionLocal() as session:
        execution = session.get(RecruitmentCampaignExecutionRecord, execution_id)
        step = session.execute(
            select(RecruitmentCampaignExecutionStepRecord).where(
                RecruitmentCampaignExecutionStepRecord.execution_id == execution_id
            )
        ).scalar_one()
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        assert execution is not None and execution.lease_token is None
        assert step.attempt_count == 2
        assert step.output_source == "test"
        assert campaign is not None
        assert campaign.job_description.endswith("call 2.")
        assert old_token is not None


def test_expired_lease_without_reclaimer_cannot_commit_old_output(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    provider = FixtureProvider(block=True)
    context = recruiter_context(tenant)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            RecruitmentCampaignOrchestrator.start,
            campaign_id,
            context,
            provider,
        )
        assert provider.entered.wait(timeout=10)
        with SessionLocal.begin() as session:
            execution = session.execute(
                select(RecruitmentCampaignExecutionRecord).where(
                    RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
                )
            ).scalar_one()
            execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        provider.release.set()
        returned = future.result(timeout=10)

    assert returned.status == "running"
    with SessionLocal() as session:
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        execution = session.execute(
            select(RecruitmentCampaignExecutionRecord).where(
                RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
            )
        ).scalar_one()
        assert campaign is not None and campaign.job_description == ""
        assert execution.status == "running"
        assert execution.lease_expires_at is not None


def test_approval_revocation_while_running_discards_result_and_audits(
    client: TestClient,
):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    provider = FixtureProvider(block=True)
    context = recruiter_context(tenant)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            RecruitmentCampaignOrchestrator.start,
            campaign_id,
            context,
            provider,
        )
        assert provider.entered.wait(timeout=10)
        with SessionLocal.begin() as session:
            campaign = session.get(RecruitmentCampaignRecord, campaign_id)
            assert campaign is not None
            campaign.approval_state = "rejected"
            campaign.workflow_state = "rejected"
        provider.release.set()
        result = future.result(timeout=10)

    assert result.status == "needs_attention"
    assert result.failure_code == "campaign_approval_revoked"
    with SessionLocal() as session:
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        audit = session.execute(
            select(AccessAuditRecord).where(
                AccessAuditRecord.action == "campaign_execution_needs_attention",
                AccessAuditRecord.resource_id == result.id,
            )
        ).scalar_one()
        assert campaign is not None and campaign.job_description == ""
        assert audit.actor_user_id == tenant.recruiter["id"]


def test_revoked_approval_stops_pending_execution_before_provider(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    approve_campaign(client, tenant, campaign_id)
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        assert campaign is not None
        execution = RecruitmentCampaignExecutionRecord(
            campaign_id=campaign_id,
            organization_id=campaign.organization_id,
            requested_by_user_id=tenant.recruiter["id"],
            status="pending",
            attempt_count=0,
            max_attempts=3,
            created_at=now,
            updated_at=now,
        )
        session.add(execution)
        session.flush()
        session.add(
            RecruitmentCampaignExecutionStepRecord(
                execution_id=execution.id,
                step_key="job_design",
                status="pending",
                attempt_count=0,
                input_json=RecruitmentCampaignOrchestrator._campaign_input(
                    campaign
                ).model_dump_json(),
                created_at=now,
                updated_at=now,
            )
        )
        campaign.approval_state = "rejected"
        execution_id = execution.id

    provider = FixtureProvider()
    result = RecruitmentCampaignOrchestrator.run_dispatched(
        execution_id,
        provider=provider,
    )

    assert result is not None
    assert result.status == "needs_attention"
    assert result.failure_code == "campaign_approval_revoked"
    assert provider.calls == 0


def test_worker_does_not_dispatch_unapproved_campaign(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)

    RecruitmentCampaignOrchestrator.discover_eligible(100)
    with SessionLocal() as session:
        assert (
            session.query(RecruitmentCampaignExecutionRecord)
            .filter_by(campaign_id=campaign_id)
            .count()
            == 0
        )


def test_two_workers_competing_for_pending_step_claim_only_once(
    client: TestClient,
):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    provider = FixtureProvider(block=True)
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        assert campaign is not None
        execution = RecruitmentCampaignExecutionRecord(
            campaign_id=campaign.id,
            organization_id=campaign.organization_id,
            requested_by_user_id=tenant.recruiter["id"],
            status="pending",
            attempt_count=0,
            max_attempts=3,
            created_at=now,
            updated_at=now,
        )
        session.add(execution)
        session.flush()
        session.add(
            RecruitmentCampaignExecutionStepRecord(
                execution_id=execution.id,
                step_key="job_design",
                status="pending",
                attempt_count=0,
                input_json=RecruitmentCampaignOrchestrator._campaign_input(
                    campaign
                ).model_dump_json(),
                created_at=now,
                updated_at=now,
            )
        )
        execution_id = execution.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            RecruitmentCampaignOrchestrator.run_dispatched,
            execution_id,
            provider=provider,
        )
        assert provider.entered.wait(timeout=10)
        second = executor.submit(
            RecruitmentCampaignOrchestrator.run_dispatched,
            execution_id,
            provider=provider,
        )
        duplicate_result = second.result(timeout=10)
        assert duplicate_result is not None
        assert duplicate_result.status == "running"
        provider.release.set()
        completed = first.result(timeout=10)

    assert completed is not None
    assert completed.status == "waiting_for_approval"
    assert provider.calls == 1
    assert completed.steps[0].attempt_count == 1


def test_worker_retry_backoff_is_persisted_and_bounded(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    provider = FixtureProvider(fail_once=True)

    failed = dispatch_campaign(campaign_id, provider)
    assert failed is not None
    assert failed.status == "retryable_failure"
    assert failed.available_at is not None
    available_at = (
        failed.available_at.replace(tzinfo=UTC)
        if failed.available_at.tzinfo is None
        else failed.available_at
    )
    assert available_at > datetime.now(UTC)
    with SessionLocal() as session:
        execution = (
            session.query(RecruitmentCampaignExecutionRecord)
            .filter_by(campaign_id=campaign_id)
            .one()
        )
        execution_id = execution.id
    assert execution_id not in RecruitmentCampaignOrchestrator.discover_eligible(100)
    assert provider.calls == 1

    with SessionLocal.begin() as session:
        execution = session.get(RecruitmentCampaignExecutionRecord, failed.id)
        assert execution is not None
        execution.available_at = datetime.now(UTC) - timedelta(seconds=1)

    retried = dispatch_campaign(campaign_id, provider)
    assert retried is not None
    assert retried.status == "waiting_for_approval"
    assert retried.attempt_count == 2
    assert provider.calls == 2


def test_worker_pause_resume_and_shutdown_are_safe(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    approve_campaign(client, tenant, campaign_id)
    worker = RecruitmentCampaignWorker()

    set_worker_paused(True)
    assert worker.dispatch_once() == []
    start = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/executions",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert start.status_code == 200, start.text
    assert start.json()["status"] == "pending"
    with SessionLocal() as session:
        assert session.query(RecruitmentCampaignExecutionRecord).filter_by(
            campaign_id=campaign_id
        ).count() == 1
        control = session.get(
            RecruitmentCampaignWorkerControlRecord, "recruitment_campaigns"
        )
        assert control is not None and control.paused

    set_worker_paused(False)
    execution = dispatch_campaign(campaign_id)
    assert execution is not None and execution.status == "awaiting_progress"
    stop_event = threading.Event()
    stop_event.set()
    RecruitmentCampaignWorker(stop_event=stop_event).run()


def test_pause_during_active_execution_allows_current_claim_to_finish(
    client: TestClient,
):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    provider = FixtureProvider(block=True)
    context = recruiter_context(tenant)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            RecruitmentCampaignOrchestrator.start,
            campaign_id,
            context,
            provider,
        )
        assert provider.entered.wait(timeout=10)
        with SessionLocal() as session:
            execution_id = session.execute(
                select(RecruitmentCampaignExecutionRecord.id).where(
                    RecruitmentCampaignExecutionRecord.campaign_id == campaign_id
                )
            ).scalar_one()
        set_worker_paused(True)
        paused_read = RecruitmentCampaignOrchestrator.resume(
            execution_id,
            context,
            provider,
        )
        assert paused_read.status == "running"
        assert provider.calls == 1
        provider.release.set()
        completed = future.result(timeout=10)
        assert completed.status == "waiting_for_approval"
    set_worker_paused(False)


def test_worker_status_command_fails_clearly_when_database_is_unavailable(
    monkeypatch,
):
    from sqlalchemy.exc import SQLAlchemyError

    def unavailable_session():
        raise SQLAlchemyError("synthetic database outage")

    monkeypatch.setattr(
        "services.recruitment_campaign_worker.SessionLocal",
        unavailable_session,
    )
    assert worker_main(["status"]) == 1


def test_worker_status_distinguishes_live_paused_and_stale_heartbeat():
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        control = session.get(
            RecruitmentCampaignWorkerControlRecord, "recruitment_campaigns"
        )
        assert control is not None
        control.heartbeat_at = now
        control.paused = False
    live = worker_status()
    assert live["healthy"] is True
    assert live["ready"] is True
    assert live["heartbeat_stale"] is False

    set_worker_paused(True)
    paused = worker_status()
    assert paused["healthy"] is True
    assert paused["ready"] is False
    assert paused["paused"] is True

    with SessionLocal.begin() as session:
        control = session.get(
            RecruitmentCampaignWorkerControlRecord, "recruitment_campaigns"
        )
        assert control is not None
        control.heartbeat_at = now - timedelta(minutes=1)
        control.paused = False
    stale = worker_status()
    assert stale["healthy"] is False
    assert stale["ready"] is False
    assert stale["heartbeat_stale"] is True


def test_worker_settings_reject_out_of_bounds_concurrency_and_lease():
    with pytest.raises(ValueError):
        RecruitmentCampaignWorker(concurrency=33)
    with pytest.raises(ValueError):
        RecruitmentCampaignWorker(poll_seconds=0)
    with pytest.raises(ValidationError):
        Settings(CAMPAIGN_WORKER_LEASE_SECONDS=9)


def test_concurrent_start_claims_the_logical_step_once(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    provider = FixtureProvider(block=True)
    context = recruiter_context(tenant)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            RecruitmentCampaignOrchestrator.start,
            campaign_id,
            context,
            provider,
        )
        assert provider.entered.wait(timeout=10)
        duplicate = RecruitmentCampaignOrchestrator.start(
            campaign_id, context, provider
        )
        assert duplicate.status == "running"
        provider.release.set()
        first = first_future.result(timeout=10)

    assert provider.calls == 1
    assert first.status == "waiting_for_approval"
    assert len(first.steps) == 1
    assert first.steps[0].attempt_count == 1


def test_retryable_provider_error_can_resume_and_complete_step(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    provider = FixtureProvider(fail_once=True)
    context = recruiter_context(tenant)
    failed = RecruitmentCampaignOrchestrator.start(campaign_id, context, provider)
    assert failed.status == "retryable_failure"
    assert failed.failure_code == "LLMTimeoutError"

    resumed = RecruitmentCampaignOrchestrator.resume(
        failed.id, context, provider
    )
    assert resumed.status == "waiting_for_approval"
    assert resumed.attempt_count == 2
    assert resumed.steps[0].attempt_count == 2
    assert resumed.steps[0].status == "completed"


def test_retry_limit_stops_and_records_actionable_failure(client: TestClient):
    class AlwaysFailProvider(FixtureProvider):
        def generate_structured(self, system_instruction: str, user_content: str, schema):
            self.calls += 1
            raise LLMTimeoutError("synthetic provider timeout")

    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    provider = AlwaysFailProvider()
    context = recruiter_context(tenant)
    execution = RecruitmentCampaignOrchestrator.start(
        campaign_id, context, provider
    )
    for _ in range(2):
        execution = RecruitmentCampaignOrchestrator.resume(
            execution.id, context, provider
        )

    assert execution.status == "needs_attention"
    assert execution.attempt_count == 3
    assert execution.failure_code == "LLMTimeoutError"
    assert "synthetic provider timeout" in execution.failure_message
    assert provider.calls == 3
    unchanged = RecruitmentCampaignOrchestrator.resume(
        execution.id, context, provider
    )
    assert unchanged.status == "needs_attention"
    assert provider.calls == 3


def test_missing_artifacts_without_provider_need_attention_not_mock_content(
    client: TestClient,
):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    response = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/executions",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_attention"
    assert response.json()["failure_code"] == "llm_provider_disabled"
    assert "configure a provider" in response.json()["failure_message"]


def test_mock_agent_output_is_explicit_and_requires_plan_reapproval(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant, job_description="")
    approve_campaign(client, tenant, campaign_id)
    context = recruiter_context(tenant)
    result = RecruitmentCampaignOrchestrator.start(
        campaign_id, context, DeterministicMockProvider()
    )
    assert result.status == "waiting_for_approval"
    assert result.steps[0].output_source == "mock"
    assert "[MOCK - NOT AI-GENERATED]" in result.steps[0].output["job_description"]

    approved = approve_campaign(client, tenant, campaign_id)
    assert approved["approval_state"] == "approved"
    resumed = RecruitmentCampaignOrchestrator.resume(result.id, context)
    assert resumed.status == "awaiting_progress"
    with SessionLocal() as session:
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        assert campaign is not None
        assert campaign.job_description.startswith("[MOCK - NOT AI-GENERATED]")


def test_campaign_plan_cannot_be_changed_after_submission(client: TestClient):
    tenant = create_tenant(client)
    campaign_id = create_campaign(client, tenant)
    approve_campaign(client, tenant, campaign_id)
    response = client.patch(
        f"/api/v1/recruitment-campaigns/{campaign_id}",
        json={"job_description": "Unreviewed replacement."},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 409
