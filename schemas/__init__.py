"""
Data schemas for AI Interviewer
"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ==========================================
# Candidate Schemas
# ==========================================

class CandidateBase(BaseModel):
    """Base candidate model"""
    name: str
    email: str
    phone: Optional[str] = None
    resume_url: Optional[str] = None


class CandidateCreate(CandidateBase):
    """Create candidate"""
    pass


class CandidateUpdate(BaseModel):
    """Update candidate"""
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    resume_url: Optional[str] = None


class CandidateResponse(CandidateBase):
    """Candidate response"""
    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ==========================================
# Interview Schemas
# ==========================================

class InterviewConfig(BaseModel):
    """Interview configuration"""
    duration: int = 1800  # seconds
    num_questions: int = 8
    difficulty: str = "medium"
    mode: str = "text"  # text, voice, video
    language: str = "en"


class ConversationTurn(BaseModel):
    """Single Q&A pair"""
    role: str  # "assistant" or "candidate"
    content: str
    timestamp: datetime
    duration: Optional[int] = None  # for audio


class SkillAssessment(BaseModel):
    """Skill assessment"""
    skill_name: str
    score: float = Field(ge=0, le=5)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = []
    category: Optional[str] = None


class InterviewState(BaseModel):
    """Complete interview state"""
    # Identifiers
    candidate_id: UUID
    interview_id: UUID
    position: str

    # Context
    job_description: str
    job_requirements: list[str]
    
    # Conversation
    conversation: list[ConversationTurn] = []
    current_round: int = 1
    
    # Skills
    skills: dict[str, SkillAssessment] = {}
    topics_covered: list[str] = []
    topics_missing: list[str] = []
    
    # Evaluation
    evaluations: list[dict] = []
    skill_scores: dict[str, float] = {}
    overall_score: Optional[float] = None
    
    # Metadata
    interview_stage: str = "introduction"
    remaining_time: int = 1800
    total_time_elapsed: int = 0
    follow_up_depth: int = 0
    
    # Flags
    interview_complete: bool = False
    needs_follow_up: bool = False
    should_continue: bool = True


class InterviewCreate(BaseModel):
    """Create interview"""
    candidate_id: UUID
    position: str
    job_id: UUID
    config: InterviewConfig


class InterviewResponse(BaseModel):
    """Interview response"""
    id: UUID
    candidate_id: UUID
    position: str
    status: str
    mode: str
    created_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ==========================================
# Question & Answer Schemas
# ==========================================

class QuestionResponse(BaseModel):
    """Question to candidate"""
    question: str
    question_id: UUID
    category: str
    difficulty: str
    follow_up: bool = False
    context: Optional[str] = None


class AnswerEvaluation(BaseModel):
    """Answer evaluation"""
    answer: str
    score: float = Field(ge=0, le=5)
    confidence: float = Field(ge=0, le=1)
    skills_detected: dict[str, float] = {}
    strengths: list[str] = []
    weaknesses: list[str] = []
    feedback: Optional[str] = None
    evidence: list[str] = []


# ==========================================
# Evaluation & Report Schemas
# ==========================================

class SkillScore(BaseModel):
    """Final skill score"""
    skill: str
    score: float = Field(ge=0, le=5)
    confidence: float = Field(ge=0, le=1)
    evidence: int = 0


class CandidateReport(BaseModel):
    """Final interview report"""
    candidate_id: UUID
    interview_id: UUID
    position: str
    overall_score: float = Field(ge=0, le=100)
    
    # Scores by category
    technical_score: float = Field(ge=0, le=10)
    communication_score: float = Field(ge=0, le=10)
    problem_solving_score: float = Field(ge=0, le=10)
    
    # Skills
    skills: list[SkillScore] = []
    
    # Analysis
    strengths: list[str] = []
    weaknesses: list[str] = []
    
    # Recommendation
    recommendation: str  # "strong_yes", "yes", "maybe", "no"
    confidence: float = Field(ge=0, le=1)
    
    # Report text
    summary: str = ""
    generated_at: datetime = Field(default_factory=datetime.now)


# ==========================================
# Voice Schemas
# ==========================================

class AudioChunk(BaseModel):
    """Audio chunk"""
    data: bytes
    sample_rate: int = 16000
    duration: float  # seconds


class Transcript(BaseModel):
    """Speech-to-text result"""
    text: str
    confidence: float = Field(ge=0, le=1)
    language: str = "en"
    duration: float  # seconds


# ==========================================
# RAG Schemas
# ==========================================

class Document(BaseModel):
    """RAG document"""
    id: UUID
    content: str
    metadata: dict[str, Any]
    embedding: Optional[list[float]] = None
    source: str = "unknown"


class RetrievalResult(BaseModel):
    """Retrieval result"""
    documents: list[Document]
    scores: list[float]
    query: str


# ==========================================
# Common Schemas
# ==========================================

class ErrorResponse(BaseModel):
    """Error response"""
    detail: str
    status_code: int
    request_id: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    timestamp: datetime
    version: str


class SuccessResponse(BaseModel):
    """Generic success response"""
    message: str
    data: Optional[Any] = None
