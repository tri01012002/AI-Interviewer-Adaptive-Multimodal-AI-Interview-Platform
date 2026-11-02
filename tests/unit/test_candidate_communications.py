from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from config import settings
from services.candidate_application_service import CandidateApplicationService
from services.candidate_communication_service import (
    CandidateCommunicationService,
    CommunicationDeliveryResult,
)
from services.database import (
    AccessAuditRecord,
    CandidateCommunicationEventRecord,
    CandidateCommunicationRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignRecord,
    SessionLocal,
)
from services.email_provider import EmailDeliveryResult
from services.recruitment_campaign_worker import RecruitmentCampaignWorker
from tests.unit.tenant_test_utils import create_tenant, tenant_headers
from tests.unit.test_recruitment_candidate_intake import (
    create_candidate_and_campaign,
    make_docx,
    submit_cv,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def create_screened_application(client: TestClient, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "CV_STORAGE_PATH", str(tmp_path))
    tenant, candidate, candidate_headers, _, campaign_id = create_candidate_and_campaign(
        client
    )
    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx("Senior engineer with Python experience."),
    )
    assert uploaded.status_code == 202, uploaded.text
    processed = CandidateApplicationService.process_dispatched(uploaded.json()["id"])
    assert processed is not None and processed.status == "needs_review"
    return tenant, candidate, candidate_headers, campaign_id, uploaded.json()["id"]


def draft_payload(key: str = "communication-001", **overrides):
    payload = {
        "template_type": "interview_invitation",
        "idempotency_key": key,
    }
    payload.update(overrides)
    return payload


def create_draft(client, tenant, application_id, payload=None):
    return client.post(
        f"/api/v1/recruitment-applications/{application_id}/communications",
        json=payload or draft_payload(),
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )


def approve_communication(client, tenant, application_id, key: str) -> str:
    draft = create_draft(client, tenant, application_id, draft_payload(key))
    assert draft.status_code == 201, draft.text
    communication_id = draft.json()["id"]
    for suffix, headers in (
        ("submit", tenant.recruiter_headers),
        ("approve", tenant.org_admin_headers),
    ):
        response = client.post(
            f"/api/v1/candidate-communications/{communication_id}/{suffix}",
            headers=tenant_headers(headers, tenant.organization_id),
        )
        assert response.status_code == 200, response.text
    return communication_id


def test_communication_template_idempotency_approval_and_simulation(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from services import email_provider

    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "offline_simulation")
    monkeypatch.setattr(
        email_provider.smtplib,
        "SMTP",
        lambda *args, **kwargs: pytest.fail("test dispatch attempted network I/O"),
    )
    tenant, candidate, candidate_headers, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    payload = draft_payload(
        subject_template="Next steps for ${campaign_reference}",
        body_template="Hello ${candidate_name}, please reply about ${campaign_reference}.",
    )
    response = create_draft(client, tenant, application_id, payload)
    replay = create_draft(client, tenant, application_id, payload)
    assert response.status_code == 201, response.text
    assert replay.status_code == 201
    initial = response.json()
    assert replay.json()["id"] == initial["id"]
    assert initial["status"] == "draft"
    assert initial["template_version"] == "custom-v1"
    assert initial["recipient_email"] == candidate["email"]
    assert initial["subject"] == "Next steps for campaign-" + initial["campaign_id"][:8]
    assert initial["body"].startswith("Hello Synthetic Candidate,")
    assert initial["delivery_status"] is None

    conflicting = create_draft(
        client,
        tenant,
        application_id,
        draft_payload(
            "communication-001",
            template_type="status_update",
        ),
    )
    assert conflicting.status_code == 409

    submitted = client.post(
        f"/api/v1/candidate-communications/{initial['id']}/submit",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "awaiting_approval"
    recruiter_approval = client.post(
        f"/api/v1/candidate-communications/{initial['id']}/approve",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert recruiter_approval.status_code == 403
    approval = client.post(
        f"/api/v1/candidate-communications/{initial['id']}/approve",
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert approval.status_code == 200
    assert approval.json()["status"] == "approved"
    assert approval.json()["approved_by_user_id"] == tenant.org_admin["id"]

    dispatched = RecruitmentCampaignWorker(concurrency=1).dispatch_once()
    completed = next(item for item in dispatched if item.id == initial["id"])
    assert completed.status == "simulated"
    assert completed.delivery_status == "simulated"
    assert completed.provider_name == "offline_simulation"
    assert completed.sent_at is None
    assert CandidateCommunicationService.process_dispatched(initial["id"]) is None

    candidate_read = client.get(
        f"/api/v1/candidate-communications/{initial['id']}",
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    )
    assert candidate_read.status_code == 200
    assert candidate_read.json()["body"] == initial["body"]

    with SessionLocal() as session:
        audit_rows = session.execute(
            select(AccessAuditRecord).where(
                AccessAuditRecord.resource_id == initial["id"]
            )
        ).scalars()
        events = session.execute(
            select(CandidateCommunicationEventRecord).where(
                CandidateCommunicationEventRecord.communication_id == initial["id"]
            )
        ).scalars()
        audit_text = " ".join(
            str((row.action, row.outcome, row.support_reason)) for row in audit_rows
        )
        event_text = " ".join(row.details_json for row in events)
        assert initial["body"] not in audit_text
        assert initial["recipient_email"] not in audit_text
        assert initial["body"] not in event_text
        application = session.get(RecruitmentApplicationRecord, application_id)
        assert application is not None
        assert application.status == "needs_review"
        assert application.review_status == "pending"


def test_communication_authorization_template_validation_and_cancellation(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tenant, _, candidate_headers, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    candidate_create = client.post(
        f"/api/v1/recruitment-applications/{application_id}/communications",
        json=draft_payload(),
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    )
    assert candidate_create.status_code == 403

    bad_template = create_draft(
        client,
        tenant,
        application_id,
        draft_payload(
            "communication-bad",
            subject_template="${__import__('os').system('whoami')}",
            body_template="Do not evaluate ${unknown_value}.",
        ),
    )
    assert bad_template.status_code == 422

    draft = create_draft(
        client, tenant, application_id, draft_payload("communication-cancel")
    )
    assert draft.status_code == 201
    communication_id = draft.json()["id"]
    other_tenant = create_tenant(client, name="Other Organization")
    cross_tenant_read = client.get(
        f"/api/v1/candidate-communications/{communication_id}",
        headers=tenant_headers(
            other_tenant.org_admin_headers, other_tenant.organization_id
        ),
    )
    cross_tenant_cancel = client.post(
        f"/api/v1/candidate-communications/{communication_id}/cancel",
        headers=tenant_headers(
            other_tenant.org_admin_headers, other_tenant.organization_id
        ),
    )
    cross_tenant_approve = client.post(
        f"/api/v1/candidate-communications/{communication_id}/approve",
        headers=tenant_headers(
            other_tenant.org_admin_headers, other_tenant.organization_id
        ),
    )
    cross_tenant_retry = client.post(
        f"/api/v1/candidate-communications/{communication_id}/retry",
        headers=tenant_headers(
            other_tenant.org_admin_headers, other_tenant.organization_id
        ),
    )
    assert cross_tenant_read.status_code == 404
    assert cross_tenant_cancel.status_code == 404
    assert cross_tenant_approve.status_code == 404
    assert cross_tenant_retry.status_code == 404

    cancelled = client.post(
        f"/api/v1/candidate-communications/{communication_id}/cancel",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    resubmit = client.post(
        f"/api/v1/candidate-communications/{communication_id}/submit",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert resubmit.status_code == 409


def test_communication_retries_are_bounded_and_ambiguous_results_require_attention(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    monkeypatch.setattr(settings, "CAMPAIGN_WORKER_RETRY_BASE_SECONDS", 1)
    draft = create_draft(client, tenant, application_id, draft_payload("communication-retry"))
    communication_id = draft.json()["id"]
    for suffix, action in (("submit", "submit"), ("approve", "approve")):
        role_headers = (
            tenant.recruiter_headers if action == "submit" else tenant.org_admin_headers
        )
        result = client.post(
            f"/api/v1/candidate-communications/{communication_id}/{suffix}",
            headers=tenant_headers(role_headers, tenant.organization_id),
        )
        assert result.status_code == 200

    class TemporaryFailure:
        def deliver(self, message):
            return CommunicationDeliveryResult(
                outcome="retryable_failure",
                provider_name="offline_test_double",
                failure_code="temporary_unavailable",
            )

    failed = CandidateCommunicationService.process_dispatched(
        communication_id, TemporaryFailure()
    )
    assert failed is not None
    assert failed.status == "retryable_failure"
    assert failed.attempt_count == 1
    early_retry = client.post(
        f"/api/v1/candidate-communications/{communication_id}/retry",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert early_retry.status_code == 409
    with SessionLocal.begin() as session:
        row = session.get(CandidateCommunicationRecord, communication_id)
        assert row is not None
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
    retry = client.post(
        f"/api/v1/candidate-communications/{communication_id}/retry",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert retry.status_code == 200
    simulated = CandidateCommunicationService.process_dispatched(communication_id)
    assert simulated is not None
    assert simulated.status == "simulated"
    assert simulated.attempt_count == 2

    second = create_draft(
        client, tenant, application_id, draft_payload("communication-ambiguous")
    )
    second_id = second.json()["id"]
    for suffix, role_headers in (
        ("submit", tenant.recruiter_headers),
        ("approve", tenant.org_admin_headers),
    ):
        assert client.post(
            f"/api/v1/candidate-communications/{second_id}/{suffix}",
            headers=tenant_headers(role_headers, tenant.organization_id),
        ).status_code == 200

    class AmbiguousProvider:
        def deliver(self, message):
            return CommunicationDeliveryResult(
                outcome="ambiguous",
                provider_name="offline_test_double",
                failure_code="acceptance_unknown",
            )

    ambiguous = CandidateCommunicationService.process_dispatched(
        second_id, AmbiguousProvider()
    )
    assert ambiguous is not None
    assert ambiguous.status == "needs_attention"
    assert ambiguous.delivery_status == "unknown"
    assert CandidateCommunicationService.discover_eligible(10) == []
    ambiguous_retry = client.post(
        f"/api/v1/candidate-communications/{second_id}/retry",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert ambiguous_retry.status_code == 409

    third = create_draft(
        client, tenant, application_id, draft_payload("communication-exhausted")
    )
    third_id = third.json()["id"]
    for suffix, role_headers in (
        ("submit", tenant.recruiter_headers),
        ("approve", tenant.org_admin_headers),
    ):
        assert client.post(
            f"/api/v1/candidate-communications/{third_id}/{suffix}",
            headers=tenant_headers(role_headers, tenant.organization_id),
        ).status_code == 200
    for expected_attempt in (1, 2, 3):
        with SessionLocal.begin() as session:
            row = session.get(CandidateCommunicationRecord, third_id)
            assert row is not None
            if row.status == "retryable_failure":
                row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        exhausted = CandidateCommunicationService.process_dispatched(
            third_id, TemporaryFailure()
        )
        assert exhausted is not None
        assert exhausted.attempt_count == expected_attempt
        assert exhausted.status == (
            "needs_attention" if expected_attempt == 3 else "retryable_failure"
        )
    assert third_id not in CandidateCommunicationService.discover_eligible(10)


def test_worker_pause_blocks_communication_dispatch(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    draft = create_draft(
        client, tenant, application_id, draft_payload("communication-paused")
    )
    communication_id = draft.json()["id"]
    client.post(
        f"/api/v1/candidate-communications/{communication_id}/submit",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    client.post(
        f"/api/v1/candidate-communications/{communication_id}/approve",
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    worker = RecruitmentCampaignWorker(concurrency=1)
    from services.recruitment_campaign_worker import set_worker_paused

    set_worker_paused(True)
    try:
        assert CandidateCommunicationService.process_dispatched(communication_id) is None
        assert worker.dispatch_once() == []
    finally:
        set_worker_paused(False)


def test_competing_and_expired_communication_claims_are_fenced(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    draft = create_draft(
        client, tenant, application_id, draft_payload("communication-claim-race")
    )
    communication_id = draft.json()["id"]
    for suffix, role_headers in (
        ("submit", tenant.recruiter_headers),
        ("approve", tenant.org_admin_headers),
    ):
        assert client.post(
            f"/api/v1/candidate-communications/{communication_id}/{suffix}",
            headers=tenant_headers(role_headers, tenant.organization_id),
        ).status_code == 200

    calls = 0
    calls_lock = threading.Lock()

    class SlowSimulatedProvider:
        def deliver(self, message):
            nonlocal calls
            with calls_lock:
                calls += 1
            threading.Event().wait(0.05)
            return CommunicationDeliveryResult(
                outcome="simulated",
                provider_name="offline_test_double",
                provider_reference="sim-claim-race",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: CandidateCommunicationService.process_dispatched(
                    communication_id, SlowSimulatedProvider()
                ),
                range(2),
            )
        )
    assert calls == 1
    assert sum(result is not None for result in results) == 1

    second = create_draft(
        client, tenant, application_id, draft_payload("communication-expired-claim")
    )
    second_id = second.json()["id"]
    for suffix, role_headers in (
        ("submit", tenant.recruiter_headers),
        ("approve", tenant.org_admin_headers),
    ):
        assert client.post(
            f"/api/v1/candidate-communications/{second_id}/{suffix}",
            headers=tenant_headers(role_headers, tenant.organization_id),
        ).status_code == 200
    provider_started = threading.Event()
    allow_provider_return = threading.Event()

    class DelayedProvider:
        def deliver(self, message):
            provider_started.set()
            allow_provider_return.wait(timeout=10)
            return CommunicationDeliveryResult(
                outcome="simulated",
                provider_name="offline_test_double",
                provider_reference="sim-expired",
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            CandidateCommunicationService.process_dispatched,
            second_id,
            DelayedProvider(),
        )
        assert provider_started.wait(timeout=5)
        with SessionLocal.begin() as session:
            row = session.get(CandidateCommunicationRecord, second_id)
            assert row is not None
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert second_id not in CandidateCommunicationService.discover_eligible(10)
        allow_provider_return.set()
        assert pending.result(timeout=5) is None
    with SessionLocal() as session:
        row = session.get(CandidateCommunicationRecord, second_id)
        assert row is not None
        assert row.status == "needs_attention"
        assert row.delivery_status == "unknown"
        assert row.failure_code == "delivery_outcome_unknown"


def test_provider_acceptance_and_rejection_are_distinct_and_redacted(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    accepted_id = approve_communication(
        client, tenant, application_id, "communication-accepted-result"
    )

    class AcceptedProvider:
        def deliver(self, request):
            assert request.idempotency_key == accepted_id
            return EmailDeliveryResult(
                outcome="accepted",
                provider_name="offline_test_double",
                provider_message_id="smtp-msg-accepted-1",
                idempotency_supported=True,
                safe_metadata={
                    "smtp_acceptance": True,
                    "provider_password": "TOP_SECRET",
                    "message_body": "PRIVATE_CONTENT",
                },
            )

    accepted = CandidateCommunicationService.process_dispatched(
        accepted_id, AcceptedProvider()
    )
    assert accepted is not None
    assert accepted.status == "sent"
    assert accepted.delivery_status == "sent"
    assert accepted.sent_at is not None
    with SessionLocal() as session:
        events = session.execute(
            select(CandidateCommunicationEventRecord).where(
                CandidateCommunicationEventRecord.communication_id == accepted_id
            )
        ).scalars()
        event_text = " ".join(event.details_json for event in events)
        assert "smtp_acceptance" in event_text
        assert "TOP_SECRET" not in event_text
        assert "PRIVATE_CONTENT" not in event_text

    rejected_id = approve_communication(
        client, tenant, application_id, "communication-rejected-result"
    )

    class RejectedProvider:
        def deliver(self, request):
            return EmailDeliveryResult(
                outcome="definitively_rejected",
                provider_name="offline_test_double",
                error_class="permanent_smtp_rejection",
            )

    rejected = CandidateCommunicationService.process_dispatched(
        rejected_id, RejectedProvider()
    )
    assert rejected is not None
    assert rejected.status == "needs_attention"
    assert rejected.delivery_status is None
    assert rejected.failure_code == "permanent_smtp_rejection"


def test_reconciles_unknown_delivery_only_by_authorized_explicit_decision(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    communication_id = approve_communication(
        client, tenant, application_id, "communication-reconciliation"
    )

    class TimedOutProvider:
        def deliver(self, request):
            return EmailDeliveryResult(
                outcome="unknown",
                provider_name="offline_test_double",
                error_class="transport_outcome_unknown",
            )

    unknown = CandidateCommunicationService.process_dispatched(
        communication_id, TimedOutProvider()
    )
    assert unknown is not None
    assert unknown.status == "needs_attention"
    assert unknown.delivery_status == "unknown"
    assert communication_id not in CandidateCommunicationService.discover_eligible(100)
    cancelled_unknown = client.post(
        f"/api/v1/candidate-communications/{communication_id}/cancel",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert cancelled_unknown.status_code == 409

    denied = client.post(
        f"/api/v1/candidate-communications/{communication_id}/reconcile",
        json={"decision": "not_accepted"},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert denied.status_code == 403

    unresolved = client.post(
        f"/api/v1/candidate-communications/{communication_id}/reconcile",
        json={"decision": "unresolved"},
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert unresolved.status_code == 200
    assert unresolved.json()["status"] == "needs_attention"
    assert unresolved.json()["delivery_status"] == "unknown"

    reconciled = client.post(
        f"/api/v1/candidate-communications/{communication_id}/reconcile",
        json={"decision": "not_accepted"},
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "retryable_failure"
    assert reconciled.json()["delivery_status"] is None
    assert communication_id in CandidateCommunicationService.discover_eligible(10)


def test_stable_provider_key_survives_retry_and_preflight_blocks_delivery(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    monkeypatch.setattr(settings, "CAMPAIGN_WORKER_RETRY_BASE_SECONDS", 1)
    communication_id = approve_communication(
        client, tenant, application_id, "communication-stable-provider-key"
    )
    keys: list[str] = []
    outcomes = iter(("retryable_not_accepted", "accepted"))

    class RetryingProvider:
        def deliver(self, request):
            keys.append(request.idempotency_key)
            return EmailDeliveryResult(
                outcome=next(outcomes),
                provider_name="offline_test_double",
                error_class="temporary_smtp_rejection",
                idempotency_supported=True,
            )

    provider = RetryingProvider()
    first = CandidateCommunicationService.process_dispatched(
        communication_id, provider
    )
    assert first is not None and first.status == "retryable_failure"
    with SessionLocal.begin() as session:
        row = session.get(CandidateCommunicationRecord, communication_id)
        assert row is not None
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
    retry = client.post(
        f"/api/v1/candidate-communications/{communication_id}/retry",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert retry.status_code == 200
    second = CandidateCommunicationService.process_dispatched(
        communication_id, provider
    )
    assert second is not None and second.status == "sent"
    assert keys == [communication_id, communication_id]

    blocked_id = approve_communication(
        client, tenant, application_id, "communication-preflight-blocked"
    )
    calls = 0

    class MustNotDeliver:
        def deliver(self, request):
            nonlocal calls
            calls += 1
            raise AssertionError("preflight must block before provider call")

    monkeypatch.setattr(
        CandidateCommunicationService,
        "_delivery_scope_valid",
        lambda session, communication: False,
    )
    blocked = CandidateCommunicationService.process_dispatched(
        blocked_id, MustNotDeliver()
    )
    assert blocked is not None
    assert blocked.status == "cancelled"
    assert blocked.delivery_status is None
    assert calls == 0


def test_provider_acceptance_is_preserved_if_campaign_approval_is_revoked_mid_call(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tenant, _, _, _, application_id = create_screened_application(
        client, tmp_path, monkeypatch
    )
    communication_id = approve_communication(
        client, tenant, application_id, "communication-accepted-before-revocation"
    )

    class RevokingAcceptedProvider:
        def deliver(self, request):
            with SessionLocal.begin() as session:
                communication = session.get(
                    CandidateCommunicationRecord, communication_id
                )
                assert communication is not None
                campaign = session.get(
                    RecruitmentCampaignRecord, communication.campaign_id
                )
                assert campaign is not None
                campaign.approval_state = "rejected"
            return EmailDeliveryResult(
                outcome="accepted",
                provider_name="offline_test_double",
                safe_metadata={"smtp_acceptance": True},
            )

    delivered = CandidateCommunicationService.process_dispatched(
        communication_id, RevokingAcceptedProvider()
    )
    assert delivered is not None
    assert delivered.status == "sent"
    assert delivered.delivery_status == "sent"
    with SessionLocal() as session:
        audit_actions = session.execute(
            select(AccessAuditRecord.action).where(
                AccessAuditRecord.resource_id == communication_id
            )
        ).scalars()
        assert (
            "candidate_communication_provider_accepted_after_campaign_revocation"
            in audit_actions
        )
