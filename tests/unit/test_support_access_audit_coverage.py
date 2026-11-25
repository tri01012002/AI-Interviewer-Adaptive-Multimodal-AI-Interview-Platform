"""Platform-admin support access must be audited on every tenant-scoped route family."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from services.database import AccessAuditRecord, SessionLocal
from services.support_access_audit import (
    MandatoryAuditUnavailable,
    _is_audited_path,
    _resource_from_path,
)
from services.tenant_authorization import tenant_context
from tests.unit.tenant_test_utils import create_tenant, tenant_headers

# Tenant-context routes deliberately outside the preflight, with the reason.
# Platform admins are rejected outright by organization_service (is_platform_admin
# check), so there is no support access to audit.
_PREFLIGHT_EXEMPT_PATHS = {
    "/api/v1/organization/recruiters",
    "/api/v1/organization/recruiters/{membership_id}",
}

# Route families that previously bypassed the preflight.
_NEWLY_AUDITED = (
    ("recruitment-applications", "recruitment_application"),
    ("candidate-communications", "candidate_communication"),
    ("candidate-availability-requests", "candidate_availability_request"),
    ("interview-schedules", "interview_schedule"),
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _support_headers(tenant, reason: str) -> dict[str, str]:
    headers = tenant_headers(tenant.platform_admin_headers, tenant.organization_id)
    headers["X-Support-Reason"] = reason
    return headers


def _uses_tenant_context(route: APIRoute) -> bool:
    pending = list(route.dependant.dependencies)
    while pending:
        dependency = pending.pop()
        if dependency.call is tenant_context:
            return True
        pending.extend(dependency.dependencies)
    return False


def test_every_tenant_scoped_http_route_is_support_audited():
    tenant_paths = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and _uses_tenant_context(route)
    }
    assert tenant_paths, "expected tenant-scoped routes to be registered"
    unaudited = sorted(
        path
        for path in tenant_paths - _PREFLIGHT_EXEMPT_PATHS
        if not _is_audited_path(path)
    )
    assert unaudited == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/recruitment-applications/app-1", ("recruitment_application", "app-1")),
        (
            "/api/v1/recruitment-applications/app-1/communications",
            ("recruitment_application", "app-1"),
        ),
        ("/api/v1/candidate-communications/c-1/approve", ("candidate_communication", "c-1")),
        (
            "/api/v1/candidate-availability-requests/r-1/review",
            ("candidate_availability_request", "r-1"),
        ),
        ("/api/v1/interview-schedules/s-1/confirm", ("interview_schedule", "s-1")),
        # Existing mappings are unchanged, including nested campaign paths.
        (
            "/api/v1/recruitment-campaigns/camp-1/applications/a-1/interview-schedules",
            ("recruitment_campaign", "camp-1"),
        ),
        ("/api/v1/interview/i-1/report", ("interview", "i-1")),
        ("/api/v1/candidate-invitations/inv-1", ("candidate_invitation", "inv-1")),
    ],
)
def test_resource_mapping(path: str, expected: tuple[str, str]):
    assert _is_audited_path(path)
    assert _resource_from_path(path) == expected


def test_similar_prefixes_do_not_match():
    assert not _is_audited_path("/api/v1/interview-schedulesX/s-1")
    assert not _is_audited_path("/api/v1/organizations/mine")


@pytest.mark.parametrize(("collection", "resource_type"), _NEWLY_AUDITED)
def test_platform_support_access_writes_required_audit(
    client: TestClient, collection: str, resource_type: str
):
    tenant = create_tenant(client)
    resource_id = f"synthetic-{uuid4()}"
    reason = f"Investigating {collection} for tenant ticket"
    response = client.get(
        f"/api/v1/{collection}/{resource_id}",
        headers=_support_headers(tenant, reason),
    )
    # The resource does not exist; the audit preflight must still be recorded.
    assert response.status_code == 404, response.text
    with SessionLocal() as session:
        audit = session.execute(
            select(AccessAuditRecord).where(
                AccessAuditRecord.resource_type == resource_type,
                AccessAuditRecord.resource_id == resource_id,
            )
        ).scalar_one()
    assert audit.action == "support_access_attempt"
    assert audit.outcome == "attempted"
    assert audit.actor_user_id == tenant.platform_admin["id"]
    assert audit.requested_organization_id == tenant.organization_id
    assert audit.organization_id == tenant.organization_id
    assert audit.support_reason == reason


@pytest.mark.parametrize(("collection", "resource_type"), _NEWLY_AUDITED)
def test_platform_support_access_fails_closed_when_audit_unavailable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    collection: str,
    resource_type: str,
):
    tenant = create_tenant(client)

    def fail_audit(**_kwargs):
        raise MandatoryAuditUnavailable()

    monkeypatch.setattr(
        "services.support_access_audit.persist_required_audit", fail_audit
    )
    response = client.get(
        f"/api/v1/{collection}/synthetic-{uuid4()}",
        headers=_support_headers(tenant, "Synthetic audit failure test"),
    )
    assert response.status_code == 503, response.text
    assert response.json() == {
        "detail": "Required access audit is temporarily unavailable"
    }


@pytest.mark.parametrize(("collection", "resource_type"), _NEWLY_AUDITED)
def test_tenant_member_access_is_not_support_audited(
    client: TestClient, collection: str, resource_type: str
):
    tenant = create_tenant(client)
    resource_id = f"synthetic-{uuid4()}"
    response = client.get(
        f"/api/v1/{collection}/{resource_id}",
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert response.status_code == 404, response.text
    with SessionLocal() as session:
        audits = session.execute(
            select(AccessAuditRecord).where(
                AccessAuditRecord.action == "support_access_attempt",
                AccessAuditRecord.resource_id == resource_id,
            )
        ).scalars().all()
    assert audits == []
