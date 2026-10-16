"""Candidate management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from services.auth_service import current_user
from services.candidate_store import CandidateStore
from services.tenant_authorization import TenantContext, tenant_context

router = APIRouter()
_current_user = Depends(current_user)


class CandidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2)
    email: str = Field(..., min_length=4)
    phone: str | None = None
    resume_url: str | None = None


class CandidateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2)
    phone: str | None = None
    resume_url: str | None = None


@router.get("/candidates", tags=["Candidate"])
async def list_candidates(
    context: TenantContext = Depends(tenant_context),
):
    return CandidateStore.list(
        context.actor_user_id, context.role, context.organization_id
    )


@router.post("/candidates", tags=["Candidate"])
async def create_candidate(payload: CandidatePayload, user=_current_user):
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Candidate profiles are created through candidate-invitation redemption",
    )


@router.get("/candidates/{candidate_id}", tags=["Candidate"])
async def get_candidate(
    candidate_id: str,
    context: TenantContext = Depends(tenant_context),
):
    candidate = CandidateStore.get(
        candidate_id,
        context.actor_user_id,
        context.role,
        context.organization_id,
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return candidate


@router.patch("/candidates/{candidate_id}", tags=["Candidate"])
async def update_candidate(
    candidate_id: str,
    payload: CandidateUpdate,
    context: TenantContext = Depends(tenant_context),
):
    candidate = CandidateStore.update(
        candidate_id,
        payload.model_dump(exclude_unset=True),
        context.actor_user_id,
        context.role,
        context.organization_id,
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return candidate


@router.delete("/candidates/{candidate_id}", tags=["Candidate"])
async def delete_candidate(
    candidate_id: str,
    context: TenantContext = Depends(tenant_context),
):
    if not CandidateStore.delete(
        candidate_id,
        context.actor_user_id,
        context.role,
        context.organization_id,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return {"deleted": True, "candidate_id": candidate_id}
