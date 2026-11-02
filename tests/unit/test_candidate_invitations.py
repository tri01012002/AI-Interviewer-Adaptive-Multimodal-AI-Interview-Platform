from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from apps.api.main import app
from services.database import (
    AccessAuditRecord,
    CandidateInvitationRecord,
    CandidateRecord,
    OrganizationMembershipRecord,
    SessionLocal,
    UserRecord,
)
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    create_user,
    issue_candidate_invitation,
    mark_email_verified,
    redeem_invitation,
    start_interview,
    tenant_headers,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_invite_first_redemption_creates_private_profile_and_is_single_use(
    client: TestClient,
):
    tenant = create_tenant(client)
    candidate, candidate_headers, invitation = issue_candidate_invitation(
        client, tenant
    )
    with SessionLocal() as session:
        stored = session.get(CandidateInvitationRecord, invitation["invitation_id"])
        assert stored is not None
        assert stored.candidate_id is None
        assert stored.organization_id == tenant.organization_id
        assert stored.token_hash == sha256(invitation["token"].encode()).hexdigest()
        assert invitation["token"] != stored.token_hash
        assert stored.email == candidate["email"]

    redeemed = redeem_invitation(client, tenant, candidate_headers, invitation)
    assert redeemed.status_code == 200, redeemed.text
    candidate_id = redeemed.json()["candidate_id"]
    with SessionLocal() as session:
        profile = session.get(CandidateRecord, candidate_id)
        membership = session.execute(
            select(OrganizationMembershipRecord).where(
                OrganizationMembershipRecord.organization_id == tenant.organization_id,
                OrganizationMembershipRecord.user_id == candidate["id"],
            )
        ).scalar_one()
        stored = session.get(CandidateInvitationRecord, invitation["invitation_id"])
        assert profile is not None
        assert profile.organization_id == tenant.organization_id
        assert profile.owner_user_id == tenant.recruiter["id"]
        assert profile.user_id == candidate["id"]
        assert membership.role == "candidate"
        assert membership.status == "active"
        assert stored is not None
        assert stored.consumed_at is not None

    interview = start_interview(client, tenant, candidate_id)
    candidate_scoped = tenant_headers(candidate_headers, tenant.organization_id)
    assert client.get(
        f"/api/v1/candidates/{candidate_id}", headers=candidate_scoped
    ).status_code == 200
    assert client.get(
        f"/api/v1/interview/{interview['interview_id']}",
        headers=candidate_scoped,
    ).status_code == 200
    answer = {"turn_id": f"linked-{uuid4()}", "answer": "I delivered a Python API."}
    first = client.post(
        f"/api/v1/interview/{interview['interview_id']}/answer",
        json=answer,
        headers=candidate_scoped,
    )
    replay = client.post(
        f"/api/v1/interview/{interview['interview_id']}/answer",
        json=answer,
        headers=candidate_scoped,
    )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert client.post(
        f"/api/v1/interview/{interview['interview_id']}/report",
        headers=candidate_scoped,
    ).status_code == 200

    reused = redeem_invitation(client, tenant, candidate_headers, invitation)
    assert reused.status_code == 400
    assert reused.json()["detail"] == "Invitation is invalid or unavailable"
    with SessionLocal() as session:
        audit = session.execute(
            select(AccessAuditRecord).where(
                AccessAuditRecord.actor_user_id == candidate["id"],
                AccessAuditRecord.action == "redeem",
                AccessAuditRecord.resource_id == candidate_id,
            )
        ).scalar_one()
    assert audit.organization_id == tenant.organization_id


def test_candidate_profile_endpoint_rejects_direct_creation(client: TestClient):
    tenant = create_tenant(client)
    _, candidate_headers = create_user("candidate")
    response = client.post(
        "/api/v1/candidates",
        json={"name": "Direct profile", "email": "direct@example.com"},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 409
    assert "invitation redemption" in response.json()["detail"]
    assert client.get(
        "/api/v1/candidates",
        headers=tenant_headers(candidate_headers, tenant.organization_id),
    ).status_code == 404


def test_legacy_candidate_id_invitation_requires_mapped_owned_profile(
    client: TestClient,
):
    tenant = create_tenant(client)
    target, candidate_headers = create_user("candidate")
    mark_email_verified(target["id"])
    candidate_id = f"mapped-{uuid4()}"
    with SessionLocal.begin() as session:
        session.add(
            CandidateRecord(
                id=candidate_id,
                organization_id=tenant.organization_id,
                name="Mapped Candidate",
                email=target["email"],
                owner_user_id=tenant.recruiter["id"],
            )
        )

    response = client.post(
        f"/api/v1/candidates/{candidate_id}/invitations",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 201, response.text
    redeemed = redeem_invitation(
        client, tenant, candidate_headers, response.json()
    )
    assert redeemed.status_code == 200
    assert redeemed.json() == {"candidate_id": candidate_id}

    unassigned_id = f"unassigned-{uuid4()}"
    with SessionLocal.begin() as session:
        session.add(
            CandidateRecord(
                id=unassigned_id,
                organization_id=None,
                name="Unassigned legacy candidate",
                email=f"legacy-{uuid4()}@example.com",
                owner_user_id=None,
            )
        )
    unassigned = client.post(
        f"/api/v1/candidates/{unassigned_id}/invitations",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert unassigned.status_code == 404


def test_cross_account_mismatch_and_expired_invitation_are_rejected(
    client: TestClient,
):
    tenant = create_tenant(client)
    target, target_headers, invitation = issue_candidate_invitation(client, tenant)
    other, other_headers = create_user("candidate")
    mark_email_verified(other["id"])

    mismatch = redeem_invitation(client, tenant, other_headers, invitation)
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"] == "Invitation is invalid or unavailable"
    unverified, unverified_headers = create_user("candidate")
    another = client.post(
        "/api/v1/candidate-invitations",
        json={"candidate_name": "Unverified Candidate", "email": unverified["email"]},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert another.status_code == 201
    unverified_result = redeem_invitation(
        client, tenant, unverified_headers, another.json()
    )
    assert unverified_result.status_code == 400

    mark_email_verified(target["id"])
    with SessionLocal.begin() as session:
        session.execute(
            update(CandidateInvitationRecord)
            .where(CandidateInvitationRecord.id == invitation["invitation_id"])
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    expired = redeem_invitation(client, tenant, target_headers, invitation)
    assert expired.status_code == 400


def test_invitation_revocation_and_recruiter_revocation_prevent_redemption(
    client: TestClient,
):
    tenant = create_tenant(client)
    candidate, candidate_headers, invitation = issue_candidate_invitation(
        client, tenant
    )
    revoked = client.delete(
        f"/api/v1/candidate-invitations/{invitation['invitation_id']}",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert revoked.status_code == 200
    redemption = redeem_invitation(client, tenant, candidate_headers, invitation)
    assert redemption.status_code == 400

    candidate_2, candidate_headers_2, invitation_2 = issue_candidate_invitation(
        client, tenant
    )
    membership_id = client.get(
        "/api/v1/organizations/mine", headers=tenant.recruiter_headers
    ).json()[0]["id"]
    with SessionLocal.begin() as session:
        recruiter_membership = session.execute(
            select(OrganizationMembershipRecord).where(
                OrganizationMembershipRecord.organization_id == membership_id,
                OrganizationMembershipRecord.user_id == tenant.recruiter["id"],
            )
        ).scalar_one()
    response = client.delete(
        f"/api/v1/organization/recruiters/{recruiter_membership.id}",
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert response.status_code == 200
    assert redeem_invitation(
        client, tenant, candidate_headers_2, invitation_2
    ).status_code == 400
    assert candidate["id"] != candidate_2["id"]


def test_token_cannot_be_claimed_by_another_account(client: TestClient):
    tenant = create_tenant(client)
    intended, intended_headers, invitation = issue_candidate_invitation(
        client, tenant
    )
    other, other_headers = create_user("candidate")
    mark_email_verified(other["id"])
    assert redeem_invitation(client, tenant, other_headers, invitation).status_code == 400
    claimed = redeem_invitation(client, tenant, intended_headers, invitation)
    assert claimed.status_code == 200
    assert intended["id"] != other["id"]


def test_simultaneous_redemption_links_exactly_once(client: TestClient):
    tenant = create_tenant(client)
    candidate, candidate_headers, invitation = issue_candidate_invitation(
        client, tenant
    )

    def redeem():
        return redeem_invitation(client, tenant, candidate_headers, invitation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: redeem(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 400]
    profile_id = next(
        response.json()["candidate_id"]
        for response in responses
        if response.status_code == 200
    )
    with SessionLocal() as session:
        profile = session.get(CandidateRecord, profile_id)
        stored = session.get(CandidateInvitationRecord, invitation["invitation_id"])
        assert profile is not None
        assert profile.user_id == candidate["id"]
        assert stored is not None
        assert stored.consumed_at is not None


def test_one_global_candidate_identity_has_private_profiles_per_organization(
    client: TestClient,
):
    tenant_a = create_tenant(client, "Candidate Tenant A")
    tenant_b = create_tenant(client, "Candidate Tenant B")
    candidate, candidate_headers, profile_a, _ = create_candidate_through_invitation(
        client, tenant_a
    )
    response = client.post(
        "/api/v1/candidate-invitations",
        json={"candidate_name": "Private Profile B", "email": candidate["email"]},
        headers=tenant_headers(tenant_b.recruiter_headers, tenant_b.organization_id),
    )
    assert response.status_code == 201, response.text
    implicit = client.post(
        "/api/v1/candidate-invitations/redeem",
        json={"token": response.json()["token"]},
        headers=candidate_headers,
    )
    assert implicit.status_code == 400
    explicit = client.post(
        "/api/v1/candidate-invitations/redeem",
        json={"token": response.json()["token"]},
        headers=tenant_headers(candidate_headers, tenant_b.organization_id),
    )
    assert explicit.status_code == 200
    profile_b = explicit.json()["candidate_id"]
    assert profile_b != profile_a["candidate_id"]
    assert client.get(
        f"/api/v1/candidates/{profile_a['candidate_id']}",
        headers=tenant_headers(candidate_headers, tenant_a.organization_id),
    ).status_code == 200
    assert client.get(
        f"/api/v1/candidates/{profile_b}",
        headers=tenant_headers(candidate_headers, tenant_b.organization_id),
    ).status_code == 200


def test_invitation_and_redemption_rate_limits_remain_active(client: TestClient):
    tenant = create_tenant(client)
    candidate, _, invitation = issue_candidate_invitation(client, tenant)
    duplicate = client.post(
        "/api/v1/candidate-invitations",
        json={"candidate_name": "Repeated", "email": candidate["email"]},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert duplicate.status_code == 429
    assert invitation["invitation_id"]

    _, unverified_headers = create_user("candidate")
    for _ in range(20):
        response = client.post(
            "/api/v1/candidate-invitations/redeem",
            json={"token": token_urlsafe(32)},
            headers=unverified_headers,
        )
        assert response.status_code == 400
    limited = client.post(
        "/api/v1/candidate-invitations/redeem",
        json={"token": token_urlsafe(32)},
        headers=unverified_headers,
    )
    assert limited.status_code == 429


def test_public_signup_cannot_assign_privileged_role(client: TestClient):
    email = f"public-role-{uuid4()}@example.com"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "secret123", "role": "admin"},
    )
    assert response.status_code == 200
    with SessionLocal() as session:
        user = session.execute(
            select(UserRecord).where(UserRecord.email == email)
        ).scalar_one()
        assert user.role == "candidate"
        assert user.email_verified_at is None
