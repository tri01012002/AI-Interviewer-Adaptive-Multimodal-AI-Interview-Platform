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
from services.database import (
    AccessAuditRecord,
    InterviewScheduleEventRecord,
    InterviewScheduleRecord,
    InterviewScheduleReservationRecord,
    SessionLocal,
)
from services.recruitment_campaign_worker import RecruitmentCampaignWorker
from tests.unit.tenant_test_utils import (
    add_recruiter,
    create_candidate_through_invitation,
    create_tenant,
    create_user,
)
from tests.unit.test_candidate_availability import _create_request, _headers, _screened_application
from tests.unit.test_recruitment_candidate_intake import make_docx, submit_cv


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _candidate_response(client, tenant, candidate_headers, campaign_id, application_id, *, starts):
    request = _create_request(
        client,
        tenant,
        campaign_id,
        application_id,
        key=f"availability-schedule-{application_id[:12]}",
    )
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]
    response_payload = {
        "idempotency_key": f"candidate-schedule-{application_id[:12]}",
        "timezone_name": "Asia/Ho_Chi_Minh",
        "windows": [
            {
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=30)).isoformat(),
            }
            for start in starts
        ],
    }
    submitted = client.post(
        f"/api/v1/candidate-availability-requests/{request_id}/responses",
        json=response_payload,
        headers=_headers(tenant, candidate_headers),
    )
    assert submitted.status_code == 200, submitted.text
    reviewed = client.post(
        f"/api/v1/candidate-availability-requests/{request_id}/review",
        json={"preferred_window_indices": list(range(len(starts)))},
        headers=_headers(tenant, tenant.recruiter_headers),
    )
    assert reviewed.status_code == 200, reviewed.text
    return request_id


def _future_local_slots(count=1):
    now = datetime.now(UTC)
    start = (now + timedelta(days=5)).astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    start = start.replace(hour=9, minute=0, second=0, microsecond=0)
    return [start + timedelta(minutes=30 * index) for index in range(count)]


def _proposal(
    client,
    tenant,
    campaign_id,
    application_id,
    request_id,
    *,
    window_index=0,
    key="schedule-proposal-001",
    interviewer_id=None,
    resources=None,
    headers=None,
):
    payload = {
        "availability_request_id": request_id,
        "window_index": window_index,
        "interviewer_user_id": interviewer_id or tenant.recruiter["id"],
        "exclusive_resources": resources or [],
        "idempotency_key": key,
    }
    return client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/applications/{application_id}/interview-schedules",
        json=payload,
        headers=headers or _headers(tenant, tenant.recruiter_headers),
    )


def _submit(client, tenant, schedule):
    return client.post(
        f"/api/v1/interview-schedules/{schedule['id']}/submit",
        json={"expected_version": schedule["version"]},
        headers=_headers(tenant, tenant.recruiter_headers),
    )


def _confirm(client, tenant, schedule, *, headers=None):
    return client.post(
        f"/api/v1/interview-schedules/{schedule['id']}/confirm",
        json={"expected_version": schedule["version"]},
        headers=headers or _headers(tenant, tenant.recruiter_headers),
    )


def _second_application(client, tenant, campaign_id):
    candidate, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)
    upload = submit_cv(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        make_docx("Senior software engineer using Python."),
    )
    assert upload.status_code == 202, upload.text
    processed = CandidateApplicationService.process_dispatched(upload.json()["id"])
    assert processed is not None and processed.status == "needs_review"
    return candidate, candidate_headers, upload.json()["id"]


def _full_schedule_flow(client, tenant, candidate_headers, campaign_id, application_id, *, starts=None, key="schedule-proposal-001", resources=None, interviewer_id=None):
    request_id = _candidate_response(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        application_id,
        starts=starts or _future_local_slots(),
    )
    proposal = _proposal(
        client,
        tenant,
        campaign_id,
        application_id,
        request_id,
        key=key,
        resources=resources,
        interviewer_id=interviewer_id,
    )
    assert proposal.status_code == 201, proposal.text
    submitted = _submit(client, tenant, proposal.json())
    assert submitted.status_code == 200, submitted.text
    return request_id, submitted.json()


def test_schedule_requires_reviewed_availability_and_confirms_internal_reservation(
    client, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(settings, "CV_STORAGE_PATH", str(tmp_path))
    tenant, _, candidate_headers, campaign_id, application_id = _screened_application(
        client, tmp_path, monkeypatch
    )
    request_id, pending = _full_schedule_flow(
        client, tenant, candidate_headers, campaign_id, application_id
    )
    assert pending["status"] == "pending_confirmation"
    assert pending["timezone_name"] == "Asia/Ho_Chi_Minh"
    assert datetime.fromisoformat(pending["start_at"]).utcoffset() == timedelta(0)

    confirmed = _confirm(client, tenant, pending)
    assert confirmed.status_code == 200, confirmed.text
    data = confirmed.json()
    assert data["status"] == "confirmed_internal"
    assert data["reservation_token"]
    assert data["timezone_name"] == "Asia/Ho_Chi_Minh"
    assert data["duration_minutes"] == 30
    replayed_confirmation = _confirm(client, tenant, pending)
    assert replayed_confirmation.status_code == 200
    assert replayed_confirmation.json()["reservation_token"] == data["reservation_token"]

    slots = client.get(
        f"/api/v1/candidate-availability-requests/{request_id}/schedule-slots",
        headers=_headers(tenant, candidate_headers),
    )
    assert slots.status_code == 200, slots.text
    assert slots.json()["windows"][0]["window_index"] == 0

    candidate_view = client.get(
        f"/api/v1/interview-schedules/{data['id']}",
        headers=_headers(tenant, candidate_headers),
    )
    assert candidate_view.status_code == 200, candidate_view.text
    event_types = {event["event_type"] for event in candidate_view.json()["events"]}
    assert {
        "proposal_created",
        "submitted_for_confirmation",
        "reservation_confirmed_internal",
    } <= event_types
    with SessionLocal() as session:
        claims = session.execute(
            select(InterviewScheduleReservationRecord).where(
                InterviewScheduleReservationRecord.schedule_id == data["id"]
            )
        ).scalars().all()
        audits = session.execute(
            select(AccessAuditRecord).where(
                AccessAuditRecord.resource_id == data["id"],
                AccessAuditRecord.resource_type == "interview_schedule",
            )
        ).scalars().all()
    assert {claim.resource_type for claim in claims} == {"candidate", "interviewer"}
    assert {audit.action for audit in audits} >= {
        "interview_schedule_proposed",
        "interview_schedule_submitted",
        "interview_schedule_confirmed_internal",
    }


def test_overlapping_reservations_are_serialized_and_idempotent(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CV_STORAGE_PATH", str(tmp_path))
    tenant, _, candidate_headers, campaign_id, application_id = _screened_application(
        client, tmp_path, monkeypatch
    )
    request_id = _candidate_response(
        client,
        tenant,
        candidate_headers,
        campaign_id,
        application_id,
        starts=_future_local_slots(),
    )
    first = _proposal(
        client, tenant, campaign_id, application_id, request_id, key="schedule-first-001"
    )
    replay = _proposal(
        client, tenant, campaign_id, application_id, request_id, key="schedule-first-001"
    )
    duplicate_slot = _proposal(
        client, tenant, campaign_id, application_id, request_id, key="schedule-second-001"
    )
    assert first.status_code == replay.status_code == duplicate_slot.status_code == 201
    assert first.json()["id"] == replay.json()["id"]
    first_pending = _submit(client, tenant, first.json()).json()
    second_pending = _submit(client, tenant, duplicate_slot.json()).json()

    # The requests race for the same candidate and interviewer; the mutex serializes both.
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda schedule: _confirm(client, tenant, schedule),
                [first_pending, second_pending],
            )
        )
    assert sorted(response.status_code for response in outcomes) == [200, 409]
    with SessionLocal() as session:
        states = session.execute(
            select(InterviewScheduleRecord.status).where(
                InterviewScheduleRecord.id.in_(
                    [first_pending["id"], second_pending["id"]]
                )
            )
        ).scalars().all()
        rejected_events = session.execute(
            select(InterviewScheduleEventRecord).where(
                InterviewScheduleEventRecord.event_type == "reservation_conflict"
            )
        ).scalars().all()
    assert states.count("confirmed_internal") == 1
    assert len(rejected_events) == 1


def test_interviewer_and_exclusive_resource_conflicts_and_adjacent_slots(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "CV_STORAGE_PATH", str(tmp_path))
    tenant, _, first_candidate_headers, campaign_id, first_app = _screened_application(
        client, tmp_path, monkeypatch
    )
    second_candidate, second_candidate_headers, second_app = _second_application(
        client, tenant, campaign_id
    )
    start = _future_local_slots()[0]
    first_request, first_pending = _full_schedule_flow(
        client,
        tenant,
        first_candidate_headers,
        campaign_id,
        first_app,
        starts=[start],
        key="interviewer-conflict-one",
        resources=["room-7"],
    )
    first_confirmed = _confirm(client, tenant, first_pending)
    assert first_confirmed.status_code == 200, first_confirmed.text

    second_request = _candidate_response(
        client,
        tenant,
        second_candidate_headers,
        campaign_id,
        second_app,
        starts=[start, start + timedelta(minutes=30)],
    )
    same_interviewer = _proposal(
        client,
        tenant,
        campaign_id,
        second_app,
        second_request,
        key="interviewer-conflict-two",
    )
    second_pending = _submit(client, tenant, same_interviewer.json()).json()
    rejected_interviewer = _confirm(client, tenant, second_pending)
    assert rejected_interviewer.status_code == 409

    alternate_user, alternate_headers = create_user("candidate")
    alternate_membership = add_recruiter(
        client, tenant, alternate_user, alternate_headers
    )
    assert alternate_membership
    second_resource = _proposal(
        client,
        tenant,
        campaign_id,
        second_app,
        second_request,
        key="resource-conflict-two",
        resources=["room-7"],
        interviewer_id=alternate_user["id"],
    )
    assert second_resource.status_code == 201, second_resource.text
    second_resource_pending = _submit(client, tenant, second_resource.json())
    assert second_resource_pending.status_code == 200, second_resource_pending.text
    alternate_context_headers = _headers(tenant, alternate_headers)
    wrong_interviewer = _confirm(client, tenant, second_resource_pending.json())
    assert wrong_interviewer.status_code == 403
    refreshed = client.get(
        f"/api/v1/interview-schedules/{second_resource_pending.json()['id']}",
        headers=_headers(tenant, tenant.recruiter_headers),
    )
    assert refreshed.status_code == 200
    conflict = _confirm(
        client,
        tenant,
        second_resource_pending.json(),
        headers=alternate_context_headers,
    )
    assert conflict.status_code == 409

    adjacent = _proposal(
        client,
        tenant,
        campaign_id,
        second_app,
        second_request,
        window_index=1,
        key="adjacent-slot",
        resources=["room-7"],
    )
    assert adjacent.status_code == 201, adjacent.text
    adjacent_pending = _submit(client, tenant, adjacent.json())
    assert adjacent_pending.status_code == 200, adjacent_pending.text
    adjacent_confirmed = _confirm(client, tenant, adjacent_pending.json())
    assert adjacent_confirmed.status_code == 200, adjacent_confirmed.text

    assert first_request and first_confirmed
    assert first_pending["status"] == "pending_confirmation"


def test_reschedule_preserves_old_reservation_until_replacement_commits(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "CV_STORAGE_PATH", str(tmp_path))
    tenant, _, candidate_headers, campaign_id, application_id = _screened_application(
        client, tmp_path, monkeypatch
    )
    slots = _future_local_slots(2)
    request_id = _candidate_response(
        client, tenant, candidate_headers, campaign_id, application_id, starts=slots
    )
    original_proposal = _proposal(
        client,
        tenant,
        campaign_id,
        application_id,
        request_id,
        window_index=0,
        key="reschedule-original",
    ).json()
    original_pending = _submit(client, tenant, original_proposal).json()
    original = _confirm(client, tenant, original_pending)
    assert original.status_code == 200, original.text
    old = original.json()

    replacement_payload = {
        "expected_version": old["version"],
        "availability_request_id": request_id,
        "window_index": 1,
        "interviewer_user_id": tenant.recruiter["id"],
        "exclusive_resources": [],
        "idempotency_key": "reschedule-stale",
    }
    stale_replacement = client.post(
        f"/api/v1/interview-schedules/{old['id']}/reschedule",
        json={**replacement_payload, "expected_version": old["version"] - 1},
        headers=_headers(tenant, tenant.recruiter_headers),
    )
    assert stale_replacement.status_code == 409
    replacement_payload["idempotency_key"] = "reschedule-replacement"
    replacement = client.post(
        f"/api/v1/interview-schedules/{old['id']}/reschedule",
        json=replacement_payload,
        headers=_headers(tenant, tenant.recruiter_headers),
    )
    assert replacement.status_code == 201, replacement.text
    pending = replacement.json()
    assert pending["status"] == "reschedule_pending"
    with SessionLocal() as session:
        retained = session.execute(
            select(InterviewScheduleReservationRecord).where(
                InterviewScheduleReservationRecord.schedule_id == old["id"]
            )
        ).scalars().all()
    assert len(retained) == 2

    _, blocker_headers, blocker_app = _second_application(
        client, tenant, campaign_id
    )
    blocker_request = _candidate_response(
        client,
        tenant,
        blocker_headers,
        campaign_id,
        blocker_app,
        starts=[slots[1]],
    )
    blocker_proposal = _proposal(
        client,
        tenant,
        campaign_id,
        blocker_app,
        blocker_request,
        key="reschedule-blocker",
    )
    assert blocker_proposal.status_code == 201, blocker_proposal.text
    blocker_pending = _submit(client, tenant, blocker_proposal.json()).json()
    blocker_confirmed = _confirm(client, tenant, blocker_pending)
    assert blocker_confirmed.status_code == 200, blocker_confirmed.text

    rejected_replacement = _confirm(client, tenant, pending)
    assert rejected_replacement.status_code == 409
    with SessionLocal() as session:
        preserved = session.get(InterviewScheduleRecord, old["id"])
        preserved_claim_count = len(
            session.execute(
            select(InterviewScheduleReservationRecord.id).where(
                InterviewScheduleReservationRecord.schedule_id == old["id"]
            )
            ).scalars().all()
        )
    assert preserved is not None and preserved.status == "confirmed_internal"
    assert preserved_claim_count == 2
    cancel_blocker = client.post(
        f"/api/v1/interview-schedules/{blocker_confirmed.json()['id']}/cancel",
        json={
            "expected_version": blocker_confirmed.json()["version"],
            "reason": "Release test blocker",
        },
        headers=_headers(tenant, tenant.recruiter_headers),
    )
    assert cancel_blocker.status_code == 200, cancel_blocker.text

    confirmed = _confirm(client, tenant, pending)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed_internal"
    with SessionLocal() as session:
        old_record = session.get(InterviewScheduleRecord, old["id"])
        new_claims = session.execute(
            select(InterviewScheduleReservationRecord).where(
                InterviewScheduleReservationRecord.schedule_id == pending["id"]
            )
        ).scalars().all()
        old_claims = session.execute(
            select(InterviewScheduleReservationRecord).where(
                InterviewScheduleReservationRecord.schedule_id == old["id"]
            )
        ).scalars().all()
    assert old_record is not None and old_record.status == "cancelled"
    assert len(old_claims) == 0 and len(new_claims) == 2


def test_cancellation_expiration_tenant_isolation_and_paused_worker_recovery(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "CV_STORAGE_PATH", str(tmp_path))
    tenant, _, candidate_headers, campaign_id, application_id = _screened_application(
        client, tmp_path, monkeypatch
    )
    request_id, pending = _full_schedule_flow(
        client, tenant, candidate_headers, campaign_id, application_id
    )
    other_tenant = create_tenant(client, name="Scheduling outsider")
    outsider_view = client.get(
        f"/api/v1/interview-schedules/{pending['id']}",
        headers=_headers(other_tenant, other_tenant.org_admin_headers),
    )
    assert outsider_view.status_code == 404

    paused_at = datetime.now(UTC)
    with SessionLocal.begin() as session:
        schedule = session.get(InterviewScheduleRecord, pending["id"])
        assert schedule is not None
        schedule.expires_at = paused_at - timedelta(seconds=1)
    from services.recruitment_campaign_worker import set_worker_paused

    set_worker_paused(True)
    try:
        assert RecruitmentCampaignWorker(concurrency=1).dispatch_once() == []
        with SessionLocal() as session:
            schedule = session.get(InterviewScheduleRecord, pending["id"])
            assert schedule is not None and schedule.status == "pending_confirmation"
    finally:
        set_worker_paused(False)
    RecruitmentCampaignWorker(concurrency=1).dispatch_once()
    with SessionLocal() as session:
        schedule = session.get(InterviewScheduleRecord, pending["id"])
        assert schedule is not None and schedule.status == "expired"
        assert session.scalar(
            select(InterviewScheduleReservationRecord.id).where(
                InterviewScheduleReservationRecord.schedule_id == pending["id"]
            )
        ) is None
    expired_confirmation = _confirm(client, tenant, pending)
    assert expired_confirmation.status_code == 409

    new_proposal = _proposal(
        client,
        tenant,
        campaign_id,
        application_id,
        request_id,
        key="schedule-cancel-release",
    )
    assert new_proposal.status_code == 201, new_proposal.text
    new_pending = _submit(client, tenant, new_proposal.json()).json()
    confirmed = _confirm(client, tenant, new_pending)
    assert confirmed.status_code == 200, confirmed.text
    cancellation = client.post(
        f"/api/v1/interview-schedules/{new_pending['id']}/cancel",
        json={"expected_version": confirmed.json()["version"], "reason": "Candidate requested cancellation"},
        headers=_headers(tenant, candidate_headers),
    )
    assert cancellation.status_code == 200, cancellation.text
    assert cancellation.json()["status"] == "cancelled"
    with SessionLocal() as session:
        assert session.scalar(
            select(InterviewScheduleReservationRecord.id).where(
                InterviewScheduleReservationRecord.schedule_id == new_pending["id"]
            )
        ) is None
