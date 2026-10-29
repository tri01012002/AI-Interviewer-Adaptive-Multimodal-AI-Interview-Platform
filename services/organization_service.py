"""Organization provisioning and organization-admin membership management."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select, update

from services.database import (
    AccessAuditRecord,
    CandidateInvitationRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    SessionLocal,
    UserRecord,
)
from services.tenant_authorization import TenantContext


def create_organization(
    name: str,
    initial_admin_email: str,
    actor_user_id: str,
) -> dict[str, str]:
    normalized_email = initial_admin_email.strip().lower()
    normalized_name = name.strip()
    if not normalized_name:
        raise HTTPException(status_code=422, detail="Organization name is required")

    with SessionLocal.begin() as session:
        actor = session.get(UserRecord, actor_user_id)
        if actor is None or actor.role != "admin":
            raise HTTPException(status_code=403, detail="Platform administrator required")
        initial_admin = session.execute(
            select(UserRecord).where(func.lower(UserRecord.email) == normalized_email)
        ).scalar_one_or_none()
        if (
            initial_admin is None
            or initial_admin.email_verified_at is None
            or initial_admin.role == "admin"
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Initial organization admin must be an existing verified user",
            )

        organization = OrganizationRecord(
            name=normalized_name,
            created_by_user_id=actor.id,
        )
        session.add(organization)
        session.flush()
        session.add(
            OrganizationMembershipRecord(
                organization_id=organization.id,
                user_id=initial_admin.id,
                role="org_admin",
                status="active",
                granted_by_user_id=actor.id,
            )
        )
        session.add(
            AccessAuditRecord(
                actor_user_id=actor.id,
                organization_id=organization.id,
                requested_organization_id=organization.id,
                action="create",
                resource_type="organization",
                resource_id=organization.id,
                outcome="allowed",
            )
        )
        return {
            "id": organization.id,
            "name": organization.name,
            "status": organization.status,
            "initial_admin_user_id": initial_admin.id,
        }


def list_my_organizations(user_id: str, role: str) -> list[dict[str, str]]:
    if role == "admin":
        return []
    with SessionLocal() as session:
        rows = session.execute(
            select(
                OrganizationRecord.id,
                OrganizationRecord.name,
                OrganizationMembershipRecord.role,
            )
            .join(
                OrganizationMembershipRecord,
                OrganizationMembershipRecord.organization_id == OrganizationRecord.id,
            )
            .where(
                OrganizationMembershipRecord.user_id == user_id,
                OrganizationMembershipRecord.status == "active",
                OrganizationRecord.status == "active",
            )
            .order_by(OrganizationRecord.name, OrganizationRecord.id)
        ).all()
        return [
            {"id": organization_id, "name": name, "role": membership_role}
            for organization_id, name, membership_role in rows
        ]


def add_recruiter(
    context: TenantContext,
    email: str,
) -> dict[str, str]:
    normalized_email = email.strip().lower()
    with SessionLocal.begin() as session:
        _require_org_admin(session, context)
        target = session.execute(
            select(UserRecord).where(func.lower(UserRecord.email) == normalized_email)
        ).scalar_one_or_none()
        if (
            target is None
            or target.email_verified_at is None
            or target.role == "admin"
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Verified user not found",
            )
        membership = session.execute(
            select(OrganizationMembershipRecord).where(
                OrganizationMembershipRecord.organization_id == context.organization_id,
                OrganizationMembershipRecord.user_id == target.id,
            )
        ).scalar_one_or_none()
        if membership is not None:
            if membership.role != "recruiter" or membership.status == "active":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="User already has an organization membership",
                )
            membership.status = "active"
            membership.revoked_at = None
            membership.granted_by_user_id = context.actor_user_id
        else:
            membership = OrganizationMembershipRecord(
                organization_id=context.organization_id,
                user_id=target.id,
                role="recruiter",
                status="active",
                granted_by_user_id=context.actor_user_id,
            )
            session.add(membership)
        session.flush()
        session.add(
            AccessAuditRecord(
                actor_user_id=context.actor_user_id,
                organization_id=context.organization_id,
                requested_organization_id=context.organization_id,
                action="grant",
                resource_type="organization_membership",
                resource_id=membership.id,
                outcome="allowed",
            )
        )
        return {
            "id": membership.id,
            "organization_id": membership.organization_id,
            "user_id": target.id,
            "role": membership.role,
            "status": membership.status,
        }


def revoke_recruiter(
    context: TenantContext,
    membership_id: str,
) -> bool:
    with SessionLocal.begin() as session:
        _require_org_admin(session, context)
        membership = session.execute(
            select(OrganizationMembershipRecord).where(
                OrganizationMembershipRecord.id == membership_id,
                OrganizationMembershipRecord.organization_id == context.organization_id,
                OrganizationMembershipRecord.role == "recruiter",
                OrganizationMembershipRecord.status == "active",
            )
        ).scalar_one_or_none()
        if membership is None:
            return False
        membership.status = "revoked"
        membership.revoked_at = datetime.now(UTC)
        session.execute(
            update(CandidateInvitationRecord)
            .where(
                CandidateInvitationRecord.organization_id == context.organization_id,
                CandidateInvitationRecord.recruiter_user_id == membership.user_id,
                CandidateInvitationRecord.consumed_at.is_(None),
                CandidateInvitationRecord.revoked_at.is_(None),
            )
            .values(revoked_at=membership.revoked_at)
        )
        session.add(
            AccessAuditRecord(
                actor_user_id=context.actor_user_id,
                organization_id=context.organization_id,
                requested_organization_id=context.organization_id,
                action="revoke",
                resource_type="organization_membership",
                resource_id=membership.id,
                outcome="allowed",
            )
        )
        return True


def _require_org_admin(session, context: TenantContext) -> None:
    if context.is_platform_admin or context.role != "org_admin":
        raise HTTPException(status_code=403, detail="Organization admin membership required")
    membership = session.execute(
        select(OrganizationMembershipRecord.id)
        .join(
            OrganizationRecord,
            OrganizationRecord.id == OrganizationMembershipRecord.organization_id,
        )
        .where(
            OrganizationMembershipRecord.organization_id == context.organization_id,
            OrganizationMembershipRecord.user_id == context.actor_user_id,
            OrganizationMembershipRecord.role == "org_admin",
            OrganizationMembershipRecord.status == "active",
            OrganizationRecord.status == "active",
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=403, detail="Organization admin membership required")
