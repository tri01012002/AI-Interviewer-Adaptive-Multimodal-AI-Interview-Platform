"""Recruitment API contracts."""
"JobRequestFromUser is brief - formatted version of raw input."
"GenerateJobRequest is generated a JD from brief."
"JobResponse is the persisted job with metadata."
"MissingField is a field that is missing from the job request."
"JobIntakeRequest is to check whether the brief is complete or needs more input."
"JobIntakeResponse is the response to a job intake request."

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


#output from LLM
class JobDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    requirements: list[str] = Field(default_factory=list)


class JobRequestFromUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = ""
    seniority: str | None = None
    must_have_skills: list[str] = Field(default_factory=list)
    location: str | None = None
    salary_range: str | None = None
    years_of_experience: int | None = Field(default=None, ge=0)
    job_type: str | None = None
    benefits: list[str] = Field(default_factory=list)
    additional_notes: list[str] = Field(default_factory=list)  # ý user nói mà chưa có field riêng



#validate input from users
class GenerateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    hiring_request: str = Field(..., min_length=1)
    requirements: list[str] = Field(default_factory=list)


#validate output from LLM
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

class MissingField(BaseModel):
    field: str
    question: str


class JobIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_text: str | None = None
    brief: JobRequestFromUser | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.raw_text is None) == (self.brief is None):
            raise ValueError("Send exactly one of raw_text or brief")
        return self

#validate input after check missing fields
class JobIntakeResponse(BaseModel):
    status: Literal["needs_input", "created"]
    brief: JobRequestFromUser
    missing: list[MissingField] = Field(default_factory=list)
    job: JobResponse | None = None