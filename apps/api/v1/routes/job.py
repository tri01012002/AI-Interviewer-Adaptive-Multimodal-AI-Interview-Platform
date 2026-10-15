"""Job and job-description routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.v1.schemas.recruitment import (
    GenerateJobRequest,
    JobDescription,
    JobResponse,
)
from integrations.llm_providers.provider import LLMProviderError
from services.auth_service import current_user
from services.jd_generation_service import JDGenerationService
from services.job_store import JobStore
from services.supervisor_service import SupervisorService

router = APIRouter()
_current_user = Depends(current_user)
jd_generation_service = JDGenerationService()

@router.post("/jobs", response_model=JobResponse, tags=["Recruitment"])
async def create_job(payload: JobDescription, user=_current_user) -> dict:
    return JobStore.create(payload.model_dump(), user["id"])


@router.post("/jobs/generate", response_model=JobResponse, tags=["Recruitment"])
async def generate_job_description(payload: GenerateJobRequest, user=_current_user) -> dict:
    try:
        return jd_generation_service.generate(payload.model_dump(), user["id"])
    except LLMProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job description generation is temporarily unavailable",
        ) from exc


@router.post("/jobs/{job_id}/approve", response_model=JobResponse, tags=["Recruitment"])
async def approve_job(job_id: str, user=_current_user) -> dict:
    try:
        job = SupervisorService.approve_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.get("/jobs", response_model=list[JobResponse], tags=["Recruitment"])
async def list_jobs(user=_current_user) -> list[dict]:
    return JobStore.list(user["id"])


@router.get("/jobs/{job_id}", response_model=JobResponse, tags=["Recruitment"])
async def get_job(job_id: str, user=_current_user) -> dict:
    job = JobStore.get(job_id, user["id"])
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job