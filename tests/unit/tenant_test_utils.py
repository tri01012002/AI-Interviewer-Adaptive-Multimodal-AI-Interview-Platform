"""Test helpers for flows that require verified tenant membership."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from services.auth_service import issue_token
from services.database import OrganizationMembershipRecord, SessionLocal, UserRecord
from services.user_store import UserStore


@dataclass(frozen=True)
class TenantFixture:
    organization_id: str
    platform_admin: dict
    platform_admin_headers: dict[str, str]
    org_admin: dict
    org_admin_headers: dict[str, str]
    recruiter: dict
    recruiter_headers: dict[str, str]


def create_user(
    role: str = "candidate", email: str | None = None
) -> tuple[dict, dict[str, str]]:
    email = email or f"{role}-{uuid4()}@example.com"
    user = UserStore.create(email, "secret123", role=role)
    token = issue_token(email, role)
    return user, {"Authorization": f"Bearer {token}"}


def mark_email_verified(user_id: str) -> None:
    with SessionLocal.begin() as session:
        user = session.get(UserRecord, user_id)
        assert user is not None
        user.email_verified_at = datetime.now(UTC)


def tenant_headers(headers: dict[str, str], organization_id: str) -> dict[str, str]:
    return {**headers, "X-Organization-ID": organization_id}


def create_tenant(client: TestClient, name: str = "Synthetic Organization") -> TenantFixture:
    platform_admin, platform_admin_headers = create_user("admin")
    org_admin, org_admin_headers = create_user("candidate")
    mark_email_verified(org_admin["id"])
    response = client.post(
        "/api/v1/organizations",
        json={"name": f"{name} {uuid4()}", "initial_admin_email": org_admin["email"]},
        headers=platform_admin_headers,
    )
    assert response.status_code == 201, response.text
    organization_id = response.json()["id"]

    recruiter, recruiter_headers = create_user("candidate")
    mark_email_verified(recruiter["id"])
    response = client.post(
        "/api/v1/organization/recruiters",
        json={"email": recruiter["email"]},
        headers=tenant_headers(org_admin_headers, organization_id),
    )
    assert response.status_code == 201, response.text

    return TenantFixture(
        organization_id=organization_id,
        platform_admin=platform_admin,
        platform_admin_headers=platform_admin_headers,
        org_admin=org_admin,
        org_admin_headers=org_admin_headers,
        recruiter=recruiter,
        recruiter_headers=recruiter_headers,
    )


def add_recruiter(
    client: TestClient,
    tenant: TenantFixture,
    user: dict,
    headers: dict[str, str],
) -> dict:
    mark_email_verified(user["id"])
    response = client.post(
        "/api/v1/organization/recruiters",
        json={"email": user["email"]},
        headers=tenant_headers(tenant.org_admin_headers, tenant.organization_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


def issue_candidate_invitation(
    client: TestClient,
    tenant: TenantFixture,
    email: str | None = None,
    name: str = "Synthetic Candidate",
    recruiter_headers: dict[str, str] | None = None,
) -> tuple[dict, dict[str, str], dict]:
    candidate, candidate_headers = create_user("candidate", email)
    mark_email_verified(candidate["id"])
    response = client.post(
        "/api/v1/candidate-invitations",
        json={"candidate_name": name, "email": candidate["email"]},
        headers=tenant_headers(
            recruiter_headers or tenant.recruiter_headers, tenant.organization_id
        ),
    )
    assert response.status_code == 201, response.text
    return candidate, candidate_headers, response.json()


def redeem_invitation(
    client: TestClient,
    tenant: TenantFixture,
    candidate_headers: dict[str, str],
    invitation: dict,
    *,
    organization_id: str | None = None,
):
    headers = dict(candidate_headers)
    selected_organization = organization_id or tenant.organization_id
    if selected_organization is not None:
        headers["X-Organization-ID"] = selected_organization
    return client.post(
        "/api/v1/candidate-invitations/redeem",
        json={"token": invitation["token"]},
        headers=headers,
    )


def create_candidate_through_invitation(
    client: TestClient,
    tenant: TenantFixture,
    email: str | None = None,
    name: str = "Synthetic Candidate",
    recruiter_headers: dict[str, str] | None = None,
) -> tuple[dict, dict[str, str], dict, dict]:
    candidate, candidate_headers, invitation = issue_candidate_invitation(
        client, tenant, email, name, recruiter_headers
    )
    response = redeem_invitation(
        client, tenant, candidate_headers, invitation
    )
    assert response.status_code == 200, response.text
    return candidate, candidate_headers, response.json(), invitation


def start_interview(
    client: TestClient,
    tenant: TenantFixture,
    candidate_id: str,
    *,
    headers: dict[str, str] | None = None,
):
    response = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": candidate_id, "position": "Synthetic Engineer"},
        headers=tenant_headers(
            headers or tenant.recruiter_headers, tenant.organization_id
        ),
    )
    assert response.status_code == 200, response.text
    return response.json()


def active_membership(user_id: str, organization_id: str):
    with SessionLocal() as session:
        return session.execute(
            select(OrganizationMembershipRecord).where(
                OrganizationMembershipRecord.user_id == user_id,
                OrganizationMembershipRecord.organization_id == organization_id,
                OrganizationMembershipRecord.status == "active",
            )
        ).scalar_one_or_none()
