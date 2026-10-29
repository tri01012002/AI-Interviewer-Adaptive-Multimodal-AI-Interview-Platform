from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from config import settings
from services.candidate_application_service import CandidateApplicationService
from services.candidate_availability_service import CandidateAvailabilityService
from services.database import (
    AccessAuditRecord,
    CandidateAvailabilityEventRecord,
    CandidateAvailabilityRequestRecord,
    RecruitmentCampaignRecord,
    SessionLocal,
)
from services.recruitment_campaign_worker import RecruitmentCampaignWorker
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    tenant_headers,
)
from tests.unit.test_recruitment_candidate_intake import (
    create_candidate_and_campaign,
    make_docx,
    submit_cv,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _headers(tenant, headers):
    return tenant_headers(headers, tenant.organization_id)


def _create_request(
    client, tenant, campaign_id, application_id, *, key="availability-req-001", now=None
):
    now = now or datetime.now(UTC)
    local = (now + timedelta(days=3)).astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    start = local.replace(hour=9, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    payload = {
        "application_id": application_id,
        "idempotency_key": key,
        "response_deadline": (now + timedelta(days=1)).isoformat(),
        "interview_duration_minutes": 30,
        "timezone_name": "Asia/Ho_Chi_Minh",
        "proposed_windows": [{"start": start.isoformat(), "end": end.isoformat()}],
    }
    return client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/availability-requests",
        json=payload,
        headers=_headers(tenant, tenant.recruiter_headers),
    )


def _screened_application(client, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "CV_STORAGE_PATH", str(tmp_path))
    tenant, candidate, candidate_headers, _, campaign_id = create_candidate_and_campaign(client)
    uploaded = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx("Senior engineer with Python experience."),
    )
    assert uploaded.status_code == 202, uploaded.text
    result = CandidateApplicationService.process_dispatched(uploaded.json()["id"])
    assert result is not None and result.status == "needs_review"
    return tenant, candidate, candidate_headers, campaign_id, uploaded.json()["id"]


def test_availability_response_is_idempotent_reviewable_and_not_a_booking(
    client, tmp_path: Path, monkeypatch
):
    tenant, _, candidate_headers, campaign_id, application_id = _screened_application(
        client, tmp_path, monkeypatch
    )
    request = _create_request(client, tenant, campaign_id, application_id)
    assert request.status_code == 201, request.text
    record = request.json()
    assert record["status"] == "pending_response"
    assert record["response"] is None

    now = datetime.now(UTC)
    local = (now + timedelta(days=4)).astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    start = local.replace(hour=14, minute=0, second=0, microsecond=0)
    response_payload = {
        "idempotency_key": "candidate-response-001",
        "timezone_name": "Asia/Ho_Chi_Minh",
        "windows": [
            {
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=30)).isoformat(),
            }
        ],
    }
    headers = _headers(tenant, candidate_headers)
    response_url = f"/api/v1/candidate-availability-requests/{record['id']}/responses"
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(client.post, response_url, json=response_payload, headers=headers)
            for _ in range(2)
        ]
        submitted, replay = (future.result() for future in futures)
    assert submitted.status_code == replay.status_code == 200
    assert submitted.json()["status"] == "responded"
    assert replay.json()["response"]["version"] == 1
    assert submitted.json()["response"]["windows"][0]["start"].endswith("+00:00")
    assert "booked" not in submitted.text.lower()
    revised_start = start + timedelta(days=1)
    revised_payload = {
        **response_payload,
        "idempotency_key": "candidate-response-002",
        "windows": [
            {
                "start": revised_start.isoformat(),
                "end": (revised_start + timedelta(minutes=30)).isoformat(),
            }
        ],
    }
    revised = client.post(response_url, json=revised_payload, headers=headers)
    old_key_replay = client.post(response_url, json=response_payload, headers=headers)
    assert revised.status_code == old_key_replay.status_code == 200
    assert revised.json()["response"]["version"] == 2
    assert old_key_replay.json()["response"]["version"] == 2

    reviewed = client.post(
        f"/api/v1/candidate-availability-requests/{record['id']}/review",
        json={"preferred_window_indices": [0]},
        headers=_headers(tenant, tenant.recruiter_headers),
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "reviewed"
    assert reviewed.json()["response"]["preferred_window_indices"] == [0]
    rejected_update = client.post(
        response_url,
        json={**response_payload, "idempotency_key": "candidate-response-002"},
        headers=headers,
    )
    assert rejected_update.status_code == 409

    with SessionLocal() as session:
        events = (
            session.execute(
                select(CandidateAvailabilityEventRecord).where(
                    CandidateAvailabilityEventRecord.request_id == record["id"]
                )
            )
            .scalars()
            .all()
        )
        audits = (
            session.execute(
                select(AccessAuditRecord).where(AccessAuditRecord.resource_id == record["id"])
            )
            .scalars()
            .all()
        )
    assert {event.event_type for event in events} >= {
        "request_created",
        "candidate_response_submitted",
        "candidate_response_reviewed",
    }
    assert all("windows" not in event.details_json for event in events)
    assert {audit.action for audit in audits} >= {
        "candidate_availability_request_created",
        "candidate_availability_response_submitted",
        "candidate_availability_response_reviewed",
    }


def test_availability_timezone_size_and_interval_validation(client, tmp_path, monkeypatch):
    tenant, _, _, campaign_id, application_id = _screened_application(client, tmp_path, monkeypatch)
    now = datetime.now(UTC)
    local = (now + timedelta(days=2)).astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    start = local.replace(hour=9, minute=0, second=0, microsecond=0)
    base = {
        "application_id": application_id,
        "idempotency_key": "availability-valid-key",
        "response_deadline": (now + timedelta(days=1)).isoformat(),
        "interview_duration_minutes": 30,
        "timezone_name": "Asia/Ho_Chi_Minh",
        "proposed_windows": [
            {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat()}
        ],
    }
    url = f"/api/v1/recruitment-campaigns/{campaign_id}/availability-requests"
    headers = _headers(tenant, tenant.recruiter_headers)
    naive = {
        **base,
        "idempotency_key": "availability-naive",
        "proposed_windows": [
            {
                "start": start.replace(tzinfo=None).isoformat(),
                "end": (start + timedelta(minutes=30)).replace(tzinfo=None).isoformat(),
            }
        ],
    }
    mismatched = {
        **base,
        "idempotency_key": "availability-zone",
        "proposed_windows": [
            {
                "start": start.astimezone(UTC).isoformat(),
                "end": (start + timedelta(minutes=30)).astimezone(UTC).isoformat(),
            }
        ],
    }
    overlapping = {
        **base,
        "idempotency_key": "availability-overlap",
        "proposed_windows": [
            {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat()},
            {
                "start": (start + timedelta(minutes=15)).isoformat(),
                "end": (start + timedelta(minutes=45)).isoformat(),
            },
        ],
    }
    too_many = {
        **base,
        "idempotency_key": "availability-many",
        "proposed_windows": base["proposed_windows"] * 11,
    }
    assert client.post(url, json=naive, headers=headers).status_code == 422
    assert client.post(url, json=mismatched, headers=headers).status_code == 422
    assert client.post(url, json=overlapping, headers=headers).status_code == 422
    assert client.post(url, json=too_many, headers=headers).status_code == 422
    valid = client.post(url, json=base, headers=headers)
    assert valid.status_code == 201, valid.text
    assert valid.json()["proposed_windows"][0]["start"].endswith("+00:00")


def test_availability_creation_is_idempotent_and_cancellation_is_terminal(
    client, tmp_path, monkeypatch
):
    tenant, _, candidate_headers, campaign_id, application_id = _screened_application(
        client, tmp_path, monkeypatch
    )
    request_time = datetime.now(UTC)
    first = _create_request(client, tenant, campaign_id, application_id, now=request_time)
    replay = _create_request(client, tenant, campaign_id, application_id, now=request_time)
    assert first.status_code == replay.status_code == 201
    request_id = first.json()["id"]
    assert replay.json()["id"] == request_id
    cancel_url = f"/api/v1/candidate-availability-requests/{request_id}/cancel"
    cancelled = client.post(cancel_url, headers=_headers(tenant, tenant.recruiter_headers))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    repeated = client.post(cancel_url, headers=_headers(tenant, tenant.recruiter_headers))
    assert repeated.status_code == 409
    candidate_view = client.get(
        f"/api/v1/candidate-availability-requests/{request_id}",
        headers=_headers(tenant, candidate_headers),
    )
    assert candidate_view.status_code == 200
    assert candidate_view.json()["status"] == "cancelled"


def test_availability_tenant_ownership_and_expiration(client, tmp_path, monkeypatch):
    tenant, _, candidate_headers, campaign_id, application_id = _screened_application(
        client, tmp_path, monkeypatch
    )
    created = _create_request(client, tenant, campaign_id, application_id)
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    outsider_tenant = create_tenant(client, name="Other Organization")
    outsider_get = client.get(
        f"/api/v1/candidate-availability-requests/{request_id}",
        headers=_headers(outsider_tenant, outsider_tenant.org_admin_headers),
    )
    assert outsider_get.status_code == 404
    _, other_candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)
    wrong_owner = client.post(
        f"/api/v1/candidate-availability-requests/{request_id}/responses",
        json={
            "idempotency_key": "wrong-owner-response",
            "timezone_name": "UTC",
            "windows": [
                {
                    "start": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                    "end": (datetime.now(UTC) + timedelta(days=2, minutes=30)).isoformat(),
                }
            ],
        },
        headers=_headers(tenant, other_candidate_headers),
    )
    assert wrong_owner.status_code == 404
    with SessionLocal.begin() as session:
        row = session.get(CandidateAvailabilityRequestRecord, request_id)
        assert row is not None
        deadline = row.response_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
    assert CandidateAvailabilityService.expire_due(now=deadline + timedelta(seconds=1)) >= 1
    expired = client.get(
        f"/api/v1/candidate-availability-requests/{request_id}",
        headers=_headers(tenant, candidate_headers),
    )
    assert expired.status_code == 200
    assert expired.json()["status"] == "expired"
    rejected = client.post(
        f"/api/v1/candidate-availability-requests/{request_id}/responses",
        json={
            "idempotency_key": "late-response-key",
            "timezone_name": "UTC",
            "windows": [
                {
                    "start": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                    "end": (datetime.now(UTC) + timedelta(days=2, minutes=30)).isoformat(),
                }
            ],
        },
        headers=_headers(tenant, candidate_headers),
    )
    assert rejected.status_code == 409


def test_availability_worker_expiration_respects_pause(client, tmp_path, monkeypatch):
    tenant, _, _, campaign_id, application_id = _screened_application(client, tmp_path, monkeypatch)
    created = _create_request(client, tenant, campaign_id, application_id)
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    with SessionLocal.begin() as session:
        row = session.get(CandidateAvailabilityRequestRecord, request_id)
        assert row is not None
        row.response_deadline = datetime.now(UTC) - timedelta(seconds=1)
    from services.recruitment_campaign_worker import set_worker_paused

    set_worker_paused(True)
    try:
        assert RecruitmentCampaignWorker(concurrency=1).dispatch_once() == []
        with SessionLocal() as session:
            row = session.get(CandidateAvailabilityRequestRecord, request_id)
            assert row is not None and row.status == "pending_response"
    finally:
        set_worker_paused(False)
    RecruitmentCampaignWorker(concurrency=1).dispatch_once()
    with SessionLocal() as session:
        row = session.get(CandidateAvailabilityRequestRecord, request_id)
        assert row is not None and row.status == "expired"


def test_availability_request_is_cancelled_when_campaign_loses_approval(
    client, tmp_path, monkeypatch
):
    tenant, _, _, campaign_id, application_id = _screened_application(client, tmp_path, monkeypatch)
    created = _create_request(client, tenant, campaign_id, application_id)
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    with SessionLocal.begin() as session:
        campaign = session.get(RecruitmentCampaignRecord, campaign_id)
        assert campaign is not None
        campaign.approval_state = "rejected"
    assert CandidateAvailabilityService.expire_due() >= 1
    with SessionLocal() as session:
        request = session.get(CandidateAvailabilityRequestRecord, request_id)
        assert request is not None and request.status == "cancelled"
