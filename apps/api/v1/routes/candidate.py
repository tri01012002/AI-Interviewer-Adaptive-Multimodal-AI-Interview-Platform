"""Candidate management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.auth_service import current_user
from services.candidate_data_deletion import CandidateDataDeletionService
from services.candidate_store import CandidateStore
from services.retention_policy import CandidateLegalHoldService
from services.tenant_authorization import TenantContext, tenant_context

router = APIRouter()
_current_user = Depends(current_user)
_tenant_context = Depends(tenant_context)


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


@router.delete("/candidates/{candidate_id}/personal-data", tags=["Candidate"])
def erase_candidate_personal_data(
    candidate_id: str,
    context: TenantContext = _tenant_context,
):
    """Erase a candidate's personal data in the caller's organization.

    Organization administrators only. Returns 200 with ``status: completed``
    when every CV file and report export is gone and the database is redacted,
    or 202 with ``status: deletion_pending`` when a file could not be removed yet;
    repeating the request is safe and finishes the pending work.
    """
    result = CandidateDataDeletionService.erase_personal_data(candidate_id, context)
    return JSONResponse(
        status_code=status.HTTP_200_OK if result.complete else status.HTTP_202_ACCEPTED,
        content=result.as_response(),
    )


class LegalHoldPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=500)


@router.put("/candidates/{candidate_id}/legal-hold", tags=["Candidate"])
def set_candidate_legal_hold(
    candidate_id: str,
    payload: LegalHoldPayload,
    context: TenantContext = _tenant_context,
):
    """Place a candidate under legal hold (organization administrators only).

    While held, personal-data erasure is refused (409, reason ``legal_hold``)
    and retention rules never select the candidate.
    """
    return CandidateLegalHoldService.set_hold(candidate_id, context, payload.reason)


@router.delete("/candidates/{candidate_id}/legal-hold", tags=["Candidate"])
def clear_candidate_legal_hold(
    candidate_id: str,
    context: TenantContext = _tenant_context,
):
    """Release a candidate's legal hold (organization administrators only)."""
    return CandidateLegalHoldService.clear_hold(candidate_id, context)
