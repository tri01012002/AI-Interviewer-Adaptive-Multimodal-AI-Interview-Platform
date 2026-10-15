"""Interview workflow schemas."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .chunk import ConversationTurn
from .review import SkillAssessment


class InterviewConfig(BaseModel):
    """Interview configuration."""

    duration: int = 1800
    num_questions: int = 8
    difficulty: str = "medium"
    mode: str = "text"
    language: str = "en"


class InterviewState(BaseModel):
    """Complete interview state."""

    candidate_id: UUID
    interview_id: UUID
    position: str
    job_description: str
    job_requirements: list[str]
    conversation: list[ConversationTurn] = []
    current_round: int = 1
    skills: dict[str, SkillAssessment] = {}
    topics_covered: list[str] = []
    topics_missing: list[str] = []
    evaluations: list[dict] = []
    skill_scores: dict[str, float] = {}
    overall_score: Optional[float] = None
    interview_stage: str = "introduction"
    remaining_time: int = 1800
    total_time_elapsed: int = 0
    follow_up_depth: int = 0
    interview_complete: bool = False
    needs_follow_up: bool = False
    should_continue: bool = True


class InterviewCreate(BaseModel):
    """Interview creation payload."""

    candidate_id: UUID
    position: str
    job_id: UUID
    config: InterviewConfig


class InterviewResponse(BaseModel):
    """Interview response payload."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID
    position: str
    status: str
    mode: str
    created_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
