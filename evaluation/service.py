"""Evidence-based deterministic evaluation domain."""

from __future__ import annotations

import logging
from enum import StrEnum
from time import perf_counter
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator


class EvidenceStrength(StrEnum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class AssessmentStatus(StrEnum):
    NOT_ASSESSED = "not_assessed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DEMONSTRATED = "demonstrated"
    STRONG_EVIDENCE = "strong_evidence"


class EvidenceItem(BaseModel):
    competency: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_type: str = "candidate_statement"
    strength: EvidenceStrength = EvidenceStrength.WEAK
    specificity: str = "low"
    relevance: float = Field(default=0.0, ge=0.0, le=1.0)


class CompetencyAssessment(BaseModel):
    competency: str = Field(min_length=1)
    score: int = Field(default=0, ge=0, le=5)
    status: AssessmentStatus = AssessmentStatus.NOT_ASSESSED
    evidence_strength: EvidenceStrength = EvidenceStrength.WEAK
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    rationale: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    evaluator_type: str = "deterministic"
    evaluator_version: str = "phase-6-v1"

    @field_validator("confidence")
    @classmethod
    def confidence_is_evidence_sufficiency(cls, value: float) -> float:
        return round(value, 3)


class EvaluationResult(BaseModel):
    competency: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    assessment: CompetencyAssessment
    position: str = ""


class Rubric(BaseModel):
    competency: str
    weak: str = "Relevant terminology without a concrete example."
    acceptable: str = "Concrete example with the candidate's actions."
    strong: str = "Specific actions, trade-offs, and measurable results."
    exceptional: str = "Deep technical depth, ownership, trade-offs, and reproducible impact."


class Evaluator(Protocol):
    evaluator_type: str
    evaluator_version: str

    def evaluate(self, question: str, answer: str, competency: str, rubric: Rubric) -> EvaluationResult:
        ...


class DeterministicEvaluator:
    """Local evaluator based only on explicit answer evidence."""

    evaluator_type = "deterministic"
    evaluator_version = "phase-6-v1"
    _logger = logging.getLogger(__name__)

    def evaluate(self, question: str, answer: str, competency: str, rubric: Rubric) -> EvaluationResult:
        started = perf_counter()
        text = answer.strip()
        lowered = text.lower()
        evidence: list[EvidenceItem] = []
        if not text:
            assessment = CompetencyAssessment(
                competency=competency,
                status=AssessmentStatus.NOT_ASSESSED,
                gaps=["not assessed: no answer provided"],
                rationale="The competency was not assessed because no answer was provided.",
            )
            return EvaluationResult(competency=competency, assessment=assessment)

        indicators = {
            "python": ("python", "pandas", "numpy", "fastapi"),
            "pytorch": ("pytorch", "torch", "training loop"),
            "computer_vision": ("computer vision", "yolo", "opencv", "object detection"),
            "distributed_systems": ("redis", "cache", "distributed"),
        }
        terms = indicators.get(competency, (competency.replace("_", " "),))
        if any(term in lowered for term in terms):
            evidence.append(EvidenceItem(
                competency=competency,
                text=f"Explicitly referenced {competency.replace('_', ' ')}.",
                strength=EvidenceStrength.MODERATE,
                specificity="medium",
                relevance=1.0,
            ))
        has_action = any(token in lowered for token in ("built", "implemented", "designed", "deployed", "optimized", "used"))
        has_result = any(token in lowered for token in ("metric", "latency", "throughput", "fps", "%", "reduced", "improved"))
        if has_action and has_result:
            evidence.append(EvidenceItem(
                competency=competency,
                text="Described an action with a measurable or explicitly stated result.",
                strength=EvidenceStrength.STRONG,
                specificity="high",
                relevance=1.0,
            ))

        if not evidence:
            assessment = CompetencyAssessment(
                competency=competency,
                status=AssessmentStatus.NOT_ASSESSED,
                gaps=["not demonstrated in this answer"],
                rationale="The answer did not contain evidence attributable to this competency.",
            )
        else:
            strongest = EvidenceStrength.STRONG if has_action and has_result else EvidenceStrength.MODERATE
            score = 4 if strongest == EvidenceStrength.STRONG else 2
            gaps = [] if strongest == EvidenceStrength.STRONG else ["measurable impact or trade-offs"]
            assessment = CompetencyAssessment(
                competency=competency,
                score=score,
                status=AssessmentStatus.STRONG_EVIDENCE if strongest == EvidenceStrength.STRONG else AssessmentStatus.INSUFFICIENT_EVIDENCE,
                evidence_strength=strongest,
                confidence=min(1.0, 0.35 + (0.25 * len(evidence))),
                strengths=[item.text for item in evidence],
                gaps=gaps,
                rationale="Assessment reflects explicit answer evidence; missing details remain unassessed.",
            )
        self._logger.info(
            "evaluation completed",
            extra={
                "competency": competency,
                "evaluator_type": self.evaluator_type,
                "evaluator_version": self.evaluator_version,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
                "success": True,
            },
        )
        return EvaluationResult(competency=competency, evidence=evidence, assessment=assessment, position="")


class EvaluationService:
    """Compatibility facade and interview-level aggregation service."""

    def __init__(self, evaluator: Evaluator | None = None) -> None:
        self.evaluator = evaluator or DeterministicEvaluator()

    def evaluate_answer(self, answer: str, skills: dict[str, Any], position: str) -> dict[str, Any]:
        competency = next(iter(skills), "general")
        result = self.evaluator.evaluate("", answer, competency, Rubric(competency=competency))
        assessment = result.assessment
        return {
            "position": position,
            "skills_detected": {name: float(values.get("score", 0)) for name, values in skills.items()},
            "overall_score": round(assessment.score * 20.0, 2),
            "strengths": assessment.strengths,
            "weaknesses": assessment.gaps,
            "confidence": assessment.confidence,
            "evidence": [item.text for item in result.evidence],
            "assessment": assessment.model_dump(),
            "evaluator_type": self.evaluator.evaluator_type,
            "evaluator_version": self.evaluator.evaluator_version,
            "feedback": assessment.rationale,
        }

    @staticmethod
    def aggregate(assessments: list[CompetencyAssessment]) -> dict[str, Any]:
        assessed = [item for item in assessments if item.status != AssessmentStatus.NOT_ASSESSED]
        return {
            "competencies": [item.model_dump() for item in assessments],
            "demonstrated_strengths": [strength for item in assessed for strength in item.strengths],
            "demonstrated_gaps": [gap for item in assessments for gap in item.gaps],
            "evidence_coverage": round(len(assessed) / len(assessments), 3) if assessments else 0.0,
            "overall_score": round(sum(item.score for item in assessed) / len(assessed), 2) if assessed else None,
        }
