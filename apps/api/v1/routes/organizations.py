"""Organization provisioning and recruiter membership routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from services.auth_service import current_user
from services.organization_service import (
    add_recruiter,
    create_organization,
    list_my_organizations,
    revoke_recruiter,
)
from services.tenant_authorization import TenantContext, tenant_context

router = APIRouter()
_current_user = Depends(current_user)


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    initial_admin_email: str = Field(min_length=4, max_length=320)


class RecruiterCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=4, max_length=320)


@router.get("/organizations/mine", tags=["Organization"])
async def list_my_organizations_route(user=_current_user):
    return list_my_organizations(user["id"], user["role"])


@router.post("/organizations", status_code=status.HTTP_201_CREATED, tags=["Organization"])
async def create_organization_route(
    payload: OrganizationCreate,
    user: dict = Depends(current_user),
):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Platform administrator required")
    return create_organization(
        payload.name,
        payload.initial_admin_email,
        user["id"],
    )


@router.post(
    "/organization/recruiters",
    status_code=status.HTTP_201_CREATED,
    tags=["Organization"],
)
async def add_recruiter_route(
    payload: RecruiterCreate,
    context: TenantContext = Depends(tenant_context),
):
    return add_recruiter(context, payload.email)


@router.delete(
    "/organization/recruiters/{membership_id}",
    tags=["Organization"],
)
async def revoke_recruiter_route(
    membership_id: str,
    context: TenantContext = Depends(tenant_context),
):
    if not revoke_recruiter(context, membership_id):
        raise HTTPException(status_code=404, detail="Recruiter membership not found")
    return {"revoked": True, "membership_id": membership_id}
