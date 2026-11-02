from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from apps.api.main import app
from services.auth_service import decode_token, issue_token
from services.database import (
    AccessAuditRecord,
    CandidateRecord,
    InterviewRecord,
    SessionLocal,
)
from services.support_access_audit import MandatoryAuditUnavailable
from tests.unit.tenant_test_utils import (
    add_recruiter,
    create_candidate_through_invitation,
    create_tenant,
    create_user,
    start_interview,
    tenant_headers,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_unspecified_user_role_defaults_to_candidate(client: TestClient):
    email = f"default-role-{uuid4()}@example.com"
    from services.user_store import UserStore

    user = UserStore.create(email, "secret123")
    assert user["role"] == "candidate"
    assert decode_token(issue_token(email))["role"] == "candidate"


def test_recruiter_and_candidate_access_is_tenant_and_owner_scoped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "apps.api.v1.routes.interview.report_service.write_report",
        lambda *_args, **_kwargs: "report_synthetic.json",
    )
    tenant = create_tenant(client)
    candidate_a, candidate_headers_a, profile_a, _ = create_candidate_through_invitation(
        client, tenant
    )
    interview_a = start_interview(client, tenant, profile_a["candidate_id"])

    recruiter_b, recruiter_headers_b = create_user("candidate")
    add_recruiter(client, tenant, recruiter_b, recruiter_headers_b)
    candidate_b, candidate_headers_b, profile_b, invitation_b = (
        create_candidate_through_invitation(
            client,
            tenant,
            name="Recruiter B Candidate",
            recruiter_headers=recruiter_headers_b,
        )
    )
    interview_b = start_interview(
        client,
        tenant,
        profile_b["candidate_id"],
        headers=recruiter_headers_b,
    )

    headers_a = tenant_headers(tenant.recruiter_headers, tenant.organization_id)
    headers_b = tenant_headers(recruiter_headers_b, tenant.organization_id)
    assert client.get(
        f"/api/v1/candidates/{profile_a['candidate_id']}", headers=headers_a
    ).status_code == 200
    assert client.get(
        f"/api/v1/candidates/{profile_a['candidate_id']}", headers=headers_b
    ).status_code == 404
    assert client.get(
        f"/api/v1/interview/{interview_a['interview_id']}", headers=headers_a
    ).status_code == 200
    assert client.get(
        f"/api/v1/interview/{interview_a['interview_id']}", headers=headers_b
    ).status_code == 404
    assert {
        item["id"]
        for item in client.get("/api/v1/candidates", headers=headers_b).json()
    } == {profile_b["candidate_id"]}
    assert client.get("/api/v1/interviews", headers=headers_b).json() == [
        interview_b
    ]
    assert client.post(
        f"/api/v1/interview/{interview_a['interview_id']}/report",
        headers=headers_a,
    ).status_code == 200
    assert client.post(
        f"/api/v1/interview/{interview_a['interview_id']}/report",
        headers=headers_b,
    ).status_code == 404
    assert client.patch(
        f"/api/v1/candidates/{profile_a['candidate_id']}",
        json={"name": "Tampered Name"},
        headers=headers_b,
    ).status_code == 404
    assert client.delete(
        f"/api/v1/candidate-invitations/{invitation_b['invitation_id']}",
        headers=headers_a,
    ).status_code == 404
    assert client.post(
        "/api/v1/candidates",
        json={"name": "Direct profile", "email": "direct@example.com"},
        headers=headers_a,
    ).status_code == 409
    assert client.post(
        "/api/v1/candidate-invitations",
        json={
            "candidate_name": "Forged organization",
            "email": "forged@example.com",
            "organization_id": tenant.organization_id,
        },
        headers=headers_a,
    ).status_code == 422

    answer = {"turn_id": f"tenant-turn-{uuid4()}", "answer": "I delivered a Python API."}
    candidate_response = client.post(
        f"/api/v1/interview/{interview_a['interview_id']}/answer",
        json=answer,
        headers=tenant_headers(candidate_headers_a, tenant.organization_id),
    )
    replay = client.post(
        f"/api/v1/interview/{interview_a['interview_id']}/answer",
        json=answer,
        headers=tenant_headers(candidate_headers_a, tenant.organization_id),
    )
    assert candidate_response.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == candidate_response.json()
    assert client.get(
        f"/api/v1/candidates/{profile_a['candidate_id']}",
        headers=tenant_headers(candidate_headers_a, tenant.organization_id),
    ).status_code == 200
    assert client.get(
        f"/api/v1/interview/{interview_b['interview_id']}",
        headers=tenant_headers(candidate_headers_a, tenant.organization_id),
    ).status_code == 404
    assert candidate_b["id"] != candidate_a["id"]
    assert candidate_headers_b["Authorization"]
    assert invitation_b["invitation_id"]


def test_other_tenant_ids_do_not_override_membership_and_admins_have_no_data_access(
    client: TestClient,
):
    tenant_a = create_tenant(client, "Organization A")
    tenant_b = create_tenant(client, "Organization B")
    _, _, candidate_a, _ = create_candidate_through_invitation(client, tenant_a)
    _, _, candidate_b, _ = create_candidate_through_invitation(client, tenant_b)
    interview_b = start_interview(client, tenant_b, candidate_b["candidate_id"])

    forged_selector = tenant_headers(
        tenant_a.recruiter_headers, tenant_b.organization_id
    )
    assert client.get("/api/v1/candidates", headers=forged_selector).status_code == 404
    assert client.get(
        f"/api/v1/interview/{interview_b['interview_id']}",
        headers=tenant_headers(tenant_a.recruiter_headers, tenant_a.organization_id),
    ).status_code == 404
    assert client.post(
        "/api/v1/interview/start",
        json={
            "candidate_id": candidate_b["candidate_id"],
            "position": "Synthetic Engineer",
        },
        headers=tenant_headers(tenant_a.recruiter_headers, tenant_a.organization_id),
    ).status_code == 404

    org_admin_headers = tenant_headers(
        tenant_b.org_admin_headers, tenant_b.organization_id
    )
    assert client.get(
        f"/api/v1/candidates/{candidate_b['candidate_id']}",
        headers=org_admin_headers,
    ).status_code == 404
    assert client.get("/api/v1/interviews", headers=org_admin_headers).json() == []
    assert client.get(
        f"/api/v1/interview/{interview_b['interview_id']}", headers=org_admin_headers
    ).status_code == 404
    escalation = client.post(
        "/api/v1/organization/recruiters",
        json={"email": tenant_b.org_admin["email"], "role": "org_admin"},
        headers=org_admin_headers,
    )
    assert escalation.status_code == 422
    self_grant = client.post(
        "/api/v1/organization/recruiters",
        json={"email": tenant_b.org_admin["email"]},
        headers=org_admin_headers,
    )
    assert self_grant.status_code == 409


def test_multiple_memberships_require_a_selector_and_scope_each_request(
    client: TestClient,
):
    tenant_a = create_tenant(client, "Multi A")
    tenant_b = create_tenant(client, "Multi B")
    user, headers = create_user("candidate")
    add_recruiter(client, tenant_a, user, headers)
    add_recruiter(client, tenant_b, user, headers)

    candidate_a, _, profile_a, _ = create_candidate_through_invitation(
        client, tenant_a, recruiter_headers=headers
    )
    candidate_b, _, profile_b, _ = create_candidate_through_invitation(
        client, tenant_b, recruiter_headers=headers
    )
    assert candidate_a["email"] != candidate_b["email"]

    no_selector = client.get("/api/v1/candidates", headers=headers)
    assert no_selector.status_code == 400
    selected_a = client.get(
        "/api/v1/candidates", headers=tenant_headers(headers, tenant_a.organization_id)
    )
    selected_b = client.get(
        "/api/v1/candidates", headers=tenant_headers(headers, tenant_b.organization_id)
    )
    assert {item["id"] for item in selected_a.json()} == {profile_a["candidate_id"]}
    assert {item["id"] for item in selected_b.json()} == {profile_b["candidate_id"]}
    mine = client.get("/api/v1/organizations/mine", headers=headers)
    assert {item["id"] for item in mine.json()} == {
        tenant_a.organization_id,
        tenant_b.organization_id,
    }


def test_unassigned_records_and_users_without_membership_fail_closed(
    client: TestClient,
):
    tenant = create_tenant(client)
    outsider, outsider_headers = create_user("candidate")
    no_membership = client.get("/api/v1/candidates", headers=outsider_headers)
    assert no_membership.status_code == 403

    candidate_id = f"legacy-{uuid4()}"
    interview_id = f"legacy-interview-{uuid4()}"
    with SessionLocal.begin() as session:
        session.add(
            CandidateRecord(
                id=candidate_id,
                name="Unassigned legacy candidate",
                email=f"legacy-{uuid4()}@example.com",
                organization_id=None,
            )
        )
        session.add(
            InterviewRecord(
                id=interview_id,
                candidate_id=candidate_id,
                organization_id=None,
                owner_user_id=None,
                position="Legacy",
                mode="text",
                current_question="Legacy question",
                state_json=json.dumps(
                    {
                        "interview_id": interview_id,
                        "candidate_id": candidate_id,
                        "position": "Legacy",
                        "current_question": "Legacy question",
                        "status": "active",
                    }
                ),
            )
        )

    scoped = tenant_headers(tenant.recruiter_headers, tenant.organization_id)
    assert client.get(f"/api/v1/candidates/{candidate_id}", headers=scoped).status_code == 404
    assert client.get(f"/api/v1/interview/{interview_id}", headers=scoped).status_code == 404
    assert client.get("/api/v1/candidates", headers=scoped).json() == []
    assert client.get("/api/v1/interviews", headers=scoped).json() == []
    assert outsider["id"] != tenant.recruiter["id"]


def test_platform_support_requires_reason_and_records_attempts(
    client: TestClient,
):
    tenant = create_tenant(client)
    _, _, profile, _ = create_candidate_through_invitation(client, tenant)
    interview = start_interview(client, tenant, profile["candidate_id"])
    url = f"/api/v1/interview/{interview['interview_id']}"

    missing_context = client.get(url, headers=tenant.platform_admin_headers)
    missing_reason = client.get(
        url,
        headers=tenant_headers(
            tenant.platform_admin_headers, tenant.organization_id
        ),
    )
    allowed = client.get(
        url,
        headers={
            **tenant_headers(tenant.platform_admin_headers, tenant.organization_id),
            "X-Support-Reason": "Customer incident CASE-123",
        },
    )
    assert missing_context.status_code == 400
    assert missing_reason.status_code == 400
    assert allowed.status_code == 200
    denied_update = client.patch(
        f"/api/v1/candidates/{profile['candidate_id']}",
        json={"name": "Should not be editable by support"},
        headers={
            **tenant_headers(tenant.platform_admin_headers, tenant.organization_id),
            "X-Support-Reason": "Customer incident CASE-123",
        },
    )
    assert denied_update.status_code == 404

    with SessionLocal() as session:
        events = session.execute(
            select(AccessAuditRecord).where(
                AccessAuditRecord.actor_user_id == tenant.platform_admin["id"],
                AccessAuditRecord.resource_type == "interview",
                AccessAuditRecord.resource_id == interview["interview_id"],
            )
        ).scalars().all()
    assert events
    assert all(event.outcome == "attempted" for event in events)
    assert any(event.support_reason == "Customer incident CASE-123" for event in events)
    assert all(
        event.organization_id in {None, tenant.organization_id} for event in events
    )


def test_support_access_fails_closed_before_resource_read_when_audit_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    tenant = create_tenant(client)
    _, _, profile, _ = create_candidate_through_invitation(client, tenant)
    interview = start_interview(client, tenant, profile["candidate_id"])

    def fail_audit(**_kwargs):
        raise MandatoryAuditUnavailable()

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("interview read must not happen before durable audit")

    monkeypatch.setattr(
        "services.support_access_audit.persist_required_audit", fail_audit
    )
    monkeypatch.setattr(
        "apps.api.v1.routes.interview.service.get_interview", unexpected_read
    )
    response = client.get(
        f"/api/v1/interview/{interview['interview_id']}",
        headers={
            **tenant_headers(
                tenant.platform_admin_headers, tenant.organization_id
            ),
            "X-Support-Reason": "Synthetic audit failure test",
        },
    )
    assert response.status_code == 503


def test_report_export_fails_before_read_when_mandatory_audit_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    tenant = create_tenant(client)
    _, _, profile, _ = create_candidate_through_invitation(client, tenant)
    interview = start_interview(client, tenant, profile["candidate_id"])

    def fail_audit(**_kwargs):
        raise MandatoryAuditUnavailable()

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("interview read must not happen before durable audit")

    monkeypatch.setattr(
        "apps.api.v1.routes.interview.persist_required_audit", fail_audit
    )
    monkeypatch.setattr(
        "apps.api.v1.routes.interview.service.get_interview", unexpected_read
    )
    response = client.post(
        f"/api/v1/interview/{interview['interview_id']}/report",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 503


def test_report_export_idempotency_key_rejects_retry_without_second_export(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    tenant = create_tenant(client)
    _, _, profile, _ = create_candidate_through_invitation(client, tenant)
    interview = start_interview(client, tenant, profile["candidate_id"])
    monkeypatch.setattr(
        "apps.api.v1.routes.interview.report_service.write_report",
        lambda *_args, **_kwargs: "report_synthetic.json",
    )
    headers = {
        **tenant_headers(tenant.recruiter_headers, tenant.organization_id),
        "Idempotency-Key": "synthetic-report-export-retry",
    }
    url = f"/api/v1/interview/{interview['interview_id']}/report"

    first = client.post(url, headers=headers)
    retry = client.post(url, headers=headers)

    assert first.status_code == 200
    assert retry.status_code == 409
    with SessionLocal() as session:
        events = session.execute(
            select(AccessAuditRecord).where(
                AccessAuditRecord.actor_user_id == tenant.recruiter["id"],
                AccessAuditRecord.resource_id == interview["interview_id"],
                AccessAuditRecord.action == "report_export_attempt",
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].request_id
    assert events[0].outcome == "attempted"


def test_non_sensitive_tenant_route_does_not_require_support_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    tenant = create_tenant(client)
    _, _, profile, _ = create_candidate_through_invitation(client, tenant)

    def fail_audit(**_kwargs):
        raise MandatoryAuditUnavailable()

    monkeypatch.setattr(
        "services.support_access_audit.persist_required_audit", fail_audit
    )
    response = client.get(
        f"/api/v1/candidates/{profile['candidate_id']}",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 200


def test_websocket_authorizes_tenant_before_sending_interview_data(client: TestClient):
    tenant_a = create_tenant(client, "Websocket A")
    tenant_b = create_tenant(client, "Websocket B")
    candidate, candidate_headers, profile, _ = create_candidate_through_invitation(
        client, tenant_a
    )
    interview = start_interview(client, tenant_a, profile["candidate_id"])
    token = candidate_headers["Authorization"].removeprefix("Bearer ")

    with client.websocket_connect(
        f"/ws/interview/{interview['interview_id']}?token={token}"
    ) as websocket:
        websocket.send_json(
            {"type": "authorize", "organization_id": tenant_a.organization_id}
        )
        assert websocket.receive_json()["type"] == "connected"

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/ws/interview/{interview['interview_id']}?token={token}"
        ) as websocket:
            websocket.send_json(
                {"type": "authorize", "organization_id": tenant_b.organization_id}
            )
            websocket.receive_json()

    admin_token = tenant_a.platform_admin_headers["Authorization"].removeprefix(
        "Bearer "
    )
    with client.websocket_connect(
        f"/ws/interview/{interview['interview_id']}?token={admin_token}"
    ) as websocket:
        websocket.send_json(
            {
                "type": "authorize",
                "organization_id": tenant_a.organization_id,
                "support_reason": "Support test CASE-WS",
            }
        )
        assert websocket.receive_json()["type"] == "connected"


def test_websocket_support_audit_failure_closes_before_interview_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    tenant = create_tenant(client)
    _, _, profile, _ = create_candidate_through_invitation(client, tenant)
    interview = start_interview(client, tenant, profile["candidate_id"])
    admin_token = tenant.platform_admin_headers["Authorization"].removeprefix(
        "Bearer "
    )

    def fail_audit(**_kwargs):
        raise MandatoryAuditUnavailable()

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("interview data must not be read before durable audit")

    monkeypatch.setattr(
        "apps.api.websocket.interview.persist_required_audit", fail_audit
    )
    monkeypatch.setattr(
        "apps.api.websocket.interview.interview_service.get_interview",
        unexpected_read,
    )

    with pytest.raises(WebSocketDisconnect) as disconnect:
        with client.websocket_connect(
            f"/ws/interview/{interview['interview_id']}?token={admin_token}"
        ) as websocket:
            websocket.send_json(
                {
                    "type": "authorize",
                    "organization_id": tenant.organization_id,
                    "support_reason": "Synthetic WebSocket audit failure",
                }
            )
            websocket.receive_json()
    assert disconnect.value.code == 4503
