"""Resolve server-verified organization membership for tenant-scoped requests."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select

from services.auth_service import current_user
from services.database import OrganizationMembershipRecord, OrganizationRecord, SessionLocal


@dataclass(frozen=True)
class TenantContext:
    actor_user_id: str
    organization_id: str
    role: str
    is_platform_admin: bool = False
    support_reason: str | None = None
    is_automated: bool = False


def resolve_tenant_context(
    user: dict,
    organization_id: str | None,
    support_reason: str | None,
) -> TenantContext:
    requested_org = organization_id.strip() if organization_id else ""
    reason = support_reason.strip() if support_reason else ""

    if user["role"] == "admin":
        if not requested_org or not reason:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Platform support requires an organization and support reason",
            )
        if len(reason) > 500 or any(character in reason for character in "\r\n"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Support reason must be a single line of at most 500 characters",
            )
        with SessionLocal() as session:
            organization = session.execute(
                select(OrganizationRecord).where(
                    OrganizationRecord.id == requested_org,
                    OrganizationRecord.status == "active",
                )
            ).scalar_one_or_none()
        if organization is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )
        return TenantContext(
            actor_user_id=user["id"],
            organization_id=organization.id,
            role="platform_admin",
            is_platform_admin=True,
            support_reason=reason,
        )

    with SessionLocal() as session:
        memberships = list(
            session.execute(
                select(OrganizationMembershipRecord, OrganizationRecord)
                .join(
                    OrganizationRecord,
                    OrganizationRecord.id == OrganizationMembershipRecord.organization_id,
                )
                .where(
                    OrganizationMembershipRecord.user_id == user["id"],
                    OrganizationMembershipRecord.status == "active",
                    OrganizationRecord.status == "active",
                )
            ).all()
        )

    if requested_org:
        matches = [
            (membership, organization)
            for membership, organization in memberships
            if organization.id == requested_org
        ]
        if len(matches) != 1:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )
        membership, organization = matches[0]
    elif len(memberships) == 1:
        membership, organization = memberships[0]
    elif len(memberships) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization context is required",
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Active organization membership required",
        )

    return TenantContext(
        actor_user_id=user["id"],
        organization_id=organization.id,
        role=membership.role,
    )


def tenant_context(
    organization_id: str | None = Header(default=None, alias="X-Organization-ID"),
    support_reason: str | None = Header(default=None, alias="X-Support-Reason"),
    user: dict = Depends(current_user),
) -> TenantContext:
    return resolve_tenant_context(user, organization_id, support_reason)
