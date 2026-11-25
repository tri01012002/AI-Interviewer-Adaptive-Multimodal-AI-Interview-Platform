"""Phase E: candidate-facing interview APIs never expose internal retrieval metadata.

Candidates receive an explicit allowlist view; recruiter / platform_admin (support,
audited) keep the full internal state including per-turn ``retrieved_sources``.
Offline: lexical retrieval only, no live provider.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from rag.service import DocumentChunk, SQLAlchemyVectorStore
from services.database import AccessAuditRecord, SessionLocal
from services.interview_views import (
    CANDIDATE_CAMPAIGN_FIELDS,
    CANDIDATE_FIELDS,
    candidate_interview_view,
    interview_view_for_role,
)
from services.recruitment_campaign_orchestrator import RecruitmentCampaignOrchestrator
from services.tenant_authorization import TenantContext
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    start_interview,
    tenant_headers,
)

# Keys that must never appear anywhere (at any depth) in a candidate response.
FORBIDDEN_CANDIDATE_KEYS = frozenset(
    {
        "retrieved_sources",
        "retrieved_context",
        "document_id",
        "chunk_id",
        "scope",
        "follow_up_questions",
        "history",
        "analysis",
        "evaluation",
        "overall_score",
        "skills",
        "rubric",
        "rubric_snapshot",
        "extracted_evidence",
        "agent_decision",
        "identified_gaps",
        "errors",
        "question_source",
        "questions_asked",
        "next_action",
        "recommended_next_action",
        "current_competency",
        "difficulty",
        "interview_plan",
        "organization_id",
        "candidate_id",
        "objective_version",
        "plan_version",
    }
)

ALLOWED_CANDIDATE_KEYS = (
    set(CANDIDATE_FIELDS)
    | set(CANDIDATE_CAMPAIGN_FIELDS)
    | {"questions_answered", "question_budget"}
)


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys |= _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


def _assert_candidate_safe(payload: dict[str, Any], *secrets: str) -> None:
    leaked = _all_keys(payload) & FORBIDDEN_CANDIDATE_KEYS
    assert leaked == set(), f"candidate response leaked {sorted(leaked)}"
    assert set(payload) <= ALLOWED_CANDIDATE_KEYS, sorted(set(payload) - ALLOWED_CANDIDATE_KEYS)
    serialized = json.dumps(payload)
    for secret in secrets:
        assert secret not in serialized


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _seed_tenant_chunk(organization_id: str) -> tuple[str, str]:
    suffix = uuid4().hex[:8]
    token = f"visibilitytrace{suffix}"
    document_id = f"vis-doc-{suffix}"
    SQLAlchemyVectorStore().upsert(
        [
            DocumentChunk(
                document_id,
                "c0",
                f"{token} rollout latency guidance",
                competency=None,
                scope="organization",
                organization_id=organization_id,
                source="phase-e-test",
            )
        ],
        None,
    )
    return token, document_id


def _answered_interview(client: TestClient):
    tenant = create_tenant(client)
    token, document_id = _seed_tenant_chunk(tenant.organization_id)
    _candidate, candidate_headers, profile, _ = create_candidate_through_invitation(
        client, tenant
    )
    interview = start_interview(client, tenant, profile["candidate_id"])
    headers = tenant_headers(candidate_headers, tenant.organization_id)
    answer = client.post(
        f"/api/v1/interview/{interview['interview_id']}/answer",
        json={
            "turn_id": f"vis-turn-{uuid4()}",
            "answer": f"I shipped the {token} rollout and measured latency.",
        },
        headers=headers,
    )
    assert answer.status_code == 200, answer.text
    return tenant, headers, interview["interview_id"], answer.json(), token, document_id


# --- pure contract ---------------------------------------------------------


def test_view_selection_is_role_based_and_fails_closed():
    state = {
        "interview_id": "i-1",
        "status": "active",
        "history": [{"retrieved_sources": [{"document_id": "d", "chunk_id": "c"}]}],
        "retrieved_sources": [{"document_id": "d", "chunk_id": "c", "scope": "global"}],
        "question_budget": 5,
    }
    for role in ("recruiter", "org_admin", "platform_admin"):
        assert interview_view_for_role(state, role) is state
    for role in ("candidate", "unknown", "", None):
        view = interview_view_for_role(state, role)
        _assert_candidate_safe(view)
        assert view["questions_answered"] == 1
        assert view["question_budget"] == 5
    assert "campaign_id" not in candidate_interview_view(state)


# --- HTTP endpoints --------------------------------------------------------


def test_candidate_answer_get_and_list_hide_retrieval_metadata(client: TestClient):
    tenant, headers, interview_id, answered, token, document_id = _answered_interview(client)

    _assert_candidate_safe(answered, token, document_id)
    assert answered["interview_id"] == interview_id
    assert answered["questions_answered"] == 1
    assert answered["current_question"]

    fetched = client.get(f"/api/v1/interview/{interview_id}", headers=headers)
    assert fetched.status_code == 200
    _assert_candidate_safe(fetched.json(), token, document_id)
    assert fetched.json() == answered

    listed = client.get("/api/v1/interviews", headers=headers)
    assert listed.status_code == 200
    assert [item["interview_id"] for item in listed.json()] == [interview_id]
    for item in listed.json():
        _assert_candidate_safe(item, token, document_id)

    # The internal state still records the provenance for staff.
    staff = client.get(
        f"/api/v1/interview/{interview_id}",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    ).json()
    sources = staff["history"][-1]["retrieved_sources"]
    assert document_id in {source["document_id"] for source in sources}
    assert "retrieved_sources" in staff
    assert "evaluation" in staff


def test_candidate_self_start_returns_candidate_view(client: TestClient):
    tenant = create_tenant(client)
    _candidate, candidate_headers, profile, _ = create_candidate_through_invitation(
        client, tenant
    )
    response = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": profile["candidate_id"], "position": "Synthetic Engineer"},
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    )
    assert response.status_code == 200, response.text
    _assert_candidate_safe(response.json())
    assert response.json()["status"] == "active"
    assert response.json()["questions_answered"] == 0


def test_recruiter_start_get_and_list_keep_full_state(client: TestClient):
    tenant, _headers, interview_id, _answered, _token, document_id = _answered_interview(
        client
    )
    recruiter = tenant_headers(tenant.recruiter_headers, tenant.organization_id)

    fetched = client.get(f"/api/v1/interview/{interview_id}", headers=recruiter).json()
    assert {"retrieved_sources", "history", "evaluation", "skills", "question_source"} <= set(
        fetched
    )
    assert document_id in json.dumps(fetched["history"])

    listed = client.get("/api/v1/interviews", headers=recruiter).json()
    assert [item["interview_id"] for item in listed] == [interview_id]
    assert "retrieved_sources" in listed[0]
    assert document_id in json.dumps(listed[0]["history"])

    _candidate, _ch, profile, _ = create_candidate_through_invitation(client, tenant)
    started = start_interview(client, tenant, profile["candidate_id"])
    assert {"question_source", "history", "follow_up_questions", "organization_id"} <= set(
        started
    )


def test_platform_admin_support_read_is_full_and_audited(client: TestClient):
    tenant, _headers, interview_id, _answered, _token, document_id = _answered_interview(
        client
    )
    reason = "Investigating interview evidence trace for ticket"
    support = tenant_headers(tenant.platform_admin_headers, tenant.organization_id)
    support["X-Support-Reason"] = reason

    fetched = client.get(f"/api/v1/interview/{interview_id}", headers=support)
    assert fetched.status_code == 200, fetched.text
    assert document_id in json.dumps(fetched.json()["history"])
    assert "retrieved_sources" in fetched.json()

    listed = client.get("/api/v1/interviews", headers=support)
    assert listed.status_code == 200
    assert any("retrieved_sources" in item for item in listed.json())

    with SessionLocal() as session:
        audits = session.execute(
            select(AccessAuditRecord).where(
                AccessAuditRecord.actor_user_id == tenant.platform_admin["id"],
                AccessAuditRecord.resource_type == "interview",
                AccessAuditRecord.resource_id == interview_id,
            )
        ).scalars().all()
    assert audits
    assert all(audit.support_reason == reason for audit in audits)


def test_org_admin_access_and_other_tenant_isolation_unchanged(client: TestClient):
    tenant, _headers, interview_id, _answered, _token, _document_id = _answered_interview(
        client
    )
    org_admin = tenant_headers(tenant.org_admin_headers, tenant.organization_id)
    assert client.get(f"/api/v1/interview/{interview_id}", headers=org_admin).status_code == 404
    assert client.get("/api/v1/interviews", headers=org_admin).json() == []

    other = create_tenant(client, "Other Tenant")
    _c, other_candidate_headers, _p, _ = create_candidate_through_invitation(client, other)
    for headers in (
        tenant_headers(other.recruiter_headers, other.organization_id),
        tenant_headers(other_candidate_headers, other.organization_id),
    ):
        assert client.get(
            f"/api/v1/interview/{interview_id}", headers=headers
        ).status_code == 404
        assert client.get("/api/v1/interviews", headers=headers).json() == []
        assert client.post(
            f"/api/v1/interview/{interview_id}/answer",
            json={"turn_id": "cross-tenant", "answer": "Synthetic answer."},
            headers=headers,
        ).status_code in {403, 404}


def test_campaign_start_returns_candidate_view(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from tests.unit.test_campaign_lifecycle import (
        _prepare_candidate_with_worker,
        _setup_offline_env,
    )
    from tests.unit.test_interview_scheduling import _future_local_slots
    from tests.unit.test_recruitment_candidate_intake import create_approved_campaign

    _setup_offline_env(monkeypatch, tmp_path)
    tenant = create_tenant(client)
    _, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant)
    windows = _future_local_slots(count=1)
    campaign_id = create_approved_campaign(
        client,
        tenant,
        campaign_automation={
            "response_deadline": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "interview_duration_minutes": 30,
            "timezone_name": "Asia/Ho_Chi_Minh",
            "proposed_windows": [
                {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat()}
                for start in windows
            ],
            "interviewer_user_id": tenant.recruiter["id"],
            "exclusive_resources": [],
        },
    )
    RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant.recruiter["id"],
            organization_id=tenant.organization_id,
            role="recruiter",
        ),
    )
    application_id, _communication_id, schedule = _prepare_candidate_with_worker(
        client, tenant, campaign_id, candidate_headers, slot_index=0
    )
    response = client.post(
        f"/api/v1/interview/campaigns/{campaign_id}/applications/{application_id}/start",
        json={"schedule_id": schedule["id"], "mode": "text"},
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    _assert_candidate_safe(payload)
    assert payload["campaign_id"] == campaign_id
    assert payload["application_id"] == application_id
    assert payload["schedule_id"] == schedule["id"]
