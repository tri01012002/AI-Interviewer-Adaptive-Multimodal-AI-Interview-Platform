"""Recruitment API contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JobDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    requirements: list[str] = Field(default_factory=list)


class GenerateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    hiring_request: str = Field(..., min_length=1)
    requirements: list[str] = Field(default_factory=list)


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_user_id: str
    title: str
    description: str
    requirements: list[str]
    status: Literal["draft", "approved", "published", "archived"]
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime