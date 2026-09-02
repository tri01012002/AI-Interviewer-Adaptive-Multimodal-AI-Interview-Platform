from uuid import uuid4

from sqlalchemy import select

from evaluation.service import (
    AssessmentStatus,
    CompetencyAssessment,
    DeterministicEvaluator,
    EvidenceStrength,
    EvaluationService,
    Rubric,
)
from services.database import InterviewAssessmentRecord, InterviewEvidenceRecord, SessionLocal
from services.interview_service import InterviewService


def test_empty_answer_is_not_assessed_not_zero_failure():
    result = DeterministicEvaluator().evaluate("Question", "", "python", Rubric(competency="python"))

    assert result.assessment.status == AssessmentStatus.NOT_ASSESSED
    assert result.assessment.score == 0
    assert "not assessed" in result.assessment.gaps[0]
    assert result.assessment.confidence == 0


def test_explicit_action_and_result_produce_strong_evidence():
    result = DeterministicEvaluator().evaluate(
        "How did you use Python?",
        "I implemented a Python API and reduced latency by 40%.",
        "python",
        Rubric(competency="python"),
    )

    assert result.assessment.status == AssessmentStatus.STRONG_EVIDENCE
    assert result.assessment.score == 4
    assert result.assessment.evidence_strength == EvidenceStrength.STRONG
    assert result.assessment.confidence > 0


def test_unrelated_answer_does_not_invent_evidence():
    result = DeterministicEvaluator().evaluate(
        "How did you use Python?", "I enjoy solving customer problems.", "python", Rubric(competency="python")
    )

    assert result.evidence == []
    assert result.assessment.status == AssessmentStatus.NOT_ASSESSED
    assert result.assessment.gaps == ["not demonstrated in this answer"]


def test_scores_and_confidence_are_bounded():
    assessment = CompetencyAssessment(competency="python", score=5, confidence=1)

    assert 0 <= assessment.score <= 5
    assert 0 <= assessment.confidence <= 1


def test_interview_aggregation_does_not_turn_missing_competency_into_zero_assessment():
    missing = CompetencyAssessment(competency="database", status=AssessmentStatus.NOT_ASSESSED)
    strong = CompetencyAssessment(
        competency="python", score=4, status=AssessmentStatus.STRONG_EVIDENCE, strengths=["built an API"]
    )

    aggregate = EvaluationService.aggregate([missing, strong])

    assert aggregate["evidence_coverage"] == 0.5
    assert aggregate["overall_score"] == 4
    assert aggregate["competencies"][0]["status"] == AssessmentStatus.NOT_ASSESSED


def test_answer_persists_evidence_assessment_and_versioned_competency_state():
    service = InterviewService()
    owner_id = f"evaluation-owner-{uuid4()}"
    state = service.start_interview(f"evaluation-candidate-{uuid4()}", "AI Engineer", owner_user_id=owner_id)
    service.submit_answer(
        state["interview_id"],
        "I implemented a Python API and reduced latency by 40%.",
        f"evaluation-turn-{uuid4()}",
        owner_id,
    )

    with SessionLocal() as session:
        evidence = session.execute(
            select(InterviewEvidenceRecord).where(InterviewEvidenceRecord.interview_id == state["interview_id"])
        ).scalars().all()
        assessments = session.execute(
            select(InterviewAssessmentRecord).where(InterviewAssessmentRecord.interview_id == state["interview_id"])
        ).scalars().all()

    assert evidence
    assert all(item.turn_id == assessments[0].turn_id for item in evidence)
    assert assessments[0].evaluator_type == "deterministic"
    assert assessments[0].evaluator_version == "phase-6-v1"
