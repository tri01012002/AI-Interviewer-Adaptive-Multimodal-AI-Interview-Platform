"""Candidate and common response metadata schemas."""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CandidateBase(BaseModel):
    """Shared candidate fields."""

    name: str
    email: str
    phone: Optional[str] = None
    resume_url: Optional[str] = None


class CandidateCreate(CandidateBase):
    """Candidate creation payload."""

    pass


class CandidateUpdate(BaseModel):
    """Candidate update payload."""

    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    resume_url: Optional[str] = None


class CandidateResponse(CandidateBase):
    """Candidate response payload."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ErrorResponse(BaseModel):
    """Error response payload."""

    detail: str
    status_code: int
    request_id: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response payload."""

    status: str
    timestamp: datetime
    version: str


class SuccessResponse(BaseModel):
    """Generic success response payload."""

    message: str
    data: Optional[Any] = None
