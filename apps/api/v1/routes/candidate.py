"""Candidate management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from services.auth_service import get_current_user, security
from services.candidate_store import CandidateStore

router = APIRouter()


class CandidatePayload(BaseModel):
    name: str = Field(..., min_length=2)
    email: str = Field(..., min_length=4)
    phone: str | None = None
    resume_url: str | None = None


@router.get("/candidates", tags=["Candidate"], dependencies=[Depends(security)])
async def list_candidates(credentials=Depends(security)):
    get_current_user(credentials)
    return CandidateStore.list()


@router.post("/candidates", tags=["Candidate"], dependencies=[Depends(security)])
async def create_candidate(payload: CandidatePayload, credentials=Depends(security)):
    get_current_user(credentials)
    candidate = CandidateStore.create(payload.model_dump())
    return candidate


@router.get("/candidates/{candidate_id}", tags=["Candidate"], dependencies=[Depends(security)])
async def get_candidate(candidate_id: str, credentials=Depends(security)):
    get_current_user(credentials)
    candidate = CandidateStore.get(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return candidate


@router.patch("/candidates/{candidate_id}", tags=["Candidate"], dependencies=[Depends(security)])
async def update_candidate(candidate_id: str, payload: CandidatePayload, credentials=Depends(security)):
    get_current_user(credentials)
    candidate = CandidateStore.update(candidate_id, payload.model_dump(exclude_unset=True))
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return candidate


@router.delete("/candidates/{candidate_id}", tags=["Candidate"], dependencies=[Depends(security)])
async def delete_candidate(candidate_id: str, credentials=Depends(security)):
    get_current_user(credentials)
    if not CandidateStore.delete(candidate_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return {"deleted": True, "candidate_id": candidate_id}
