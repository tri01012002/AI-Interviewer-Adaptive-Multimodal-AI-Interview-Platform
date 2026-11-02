from agents.interview_agent.graph import InterviewAgentCore
from evaluation.service import (
    AssessmentStatus,
    CompetencyAssessment,
    EvaluationResult,
    EvidenceItem,
    EvidenceStrength,
    SuggestedNextAction,
)


def evaluation(
    action: SuggestedNextAction,
    *,
    score: int = 2,
    gaps: list[str] | None = None,
    missing: list[str] | None = None,
    evidence_strength: EvidenceStrength = EvidenceStrength.MODERATE,
) -> EvaluationResult:
    gaps = gaps or []
    missing = missing or []
    status = (
        AssessmentStatus.STRONG_EVIDENCE
        if score >= 4 and evidence_strength == EvidenceStrength.STRONG
        else AssessmentStatus.DEMONSTRATED
    )
    return EvaluationResult(
        competency="general",
        evidence=[
            EvidenceItem(
                competency="general",
                text="I built a service",
                strength=evidence_strength,
                relevance=1.0,
            )
        ],
        assessment=CompetencyAssessment(
            competency="general",
            score=score,
            status=status,
            evidence_strength=evidence_strength,
            confidence=0.9,
            technical_correctness=score,
            relevance=score,
            depth=score,
            completeness=score,
            practical_experience=score,
            gaps=gaps,
            missing_evidence=missing,
            next_action=action,
        ),
    )


def test_graph_routes_moderate_evidence_with_gap_to_deeper_probe():
    agent = InterviewAgentCore()
    result = agent.handle_answer(
        agent.start_interview("candidate", "AI Engineer"),
        "I used Redis to cache API responses.",
    )

    assert result["next_action"] == "DIG_DEEPER"
    assert "invalidation" in result["identified_gaps"][1]
    assert result["extracted_evidence"]["evidence_strength"] == "moderate"


def test_graph_increases_difficulty_for_strong_complete_evidence():
    agent = InterviewAgentCore()
    result = agent.handle_answer(
        agent.start_interview("candidate", "AI Engineer"),
        "I deployed a Python PyTorch service to production with 40% lower latency.",
    )

    assert result["next_action"] == "INCREASE_DIFFICULTY"
    assert result["difficulty"] == "hard"


def test_graph_gathers_evidence_for_weak_answer():
    agent = InterviewAgentCore()
    result = agent.handle_answer(agent.start_interview("candidate", "AI Engineer"), "I worked on a project.")

    assert result["next_action"] == "DIG_DEEPER"
    assert result["current_question"]


def test_graph_finishes_after_sufficient_accumulated_evidence():
    agent = InterviewAgentCore()
    state = agent.start_interview("candidate", "AI Engineer")
    for answer in (
        "I used Python in production and measured latency.",
        "I used PyTorch in production and measured throughput.",
    ):
        state = agent.handle_answer(state, answer)

    assert state["next_action"] == "FINISH"
    assert state["status"] == "completed"
    assert state["next_action"] == "FINISH"


def test_structured_weak_answer_routes_to_targeted_evidence_follow_up():
    agent = InterviewAgentCore()
    state = agent.start_interview("candidate", "AI Engineer")
    result = agent.handle_answer(
        state,
        "I built a service",
        evaluation=evaluation(
            SuggestedNextAction.DIG_DEEPER,
            gaps=["failure handling"],
            missing=["measurable outcome"],
        ),
    )

    assert result["next_action"] == "DIG_DEEPER"
    assert "failure handling" in result["current_question"]
    assert "measurable outcome" in result["identified_gaps"]


def test_strong_structured_answer_increases_difficulty_when_rubric_is_satisfied():
    agent = InterviewAgentCore()
    result = agent.handle_answer(
        agent.start_interview("candidate", "AI Engineer"),
        "I built a service",
        evaluation=evaluation(
            SuggestedNextAction.INCREASE_DIFFICULTY,
            score=5,
            evidence_strength=EvidenceStrength.STRONG,
        ),
    )

    assert result["next_action"] == "INCREASE_DIFFICULTY"
    assert result["difficulty"] == "hard"


def test_structured_answer_changes_to_uncovered_planned_competency():
    agent = InterviewAgentCore()
    result = agent.handle_answer(
        agent.start_interview("candidate", "AI Engineer"),
        "I built a service",
        evaluation=evaluation(
            SuggestedNextAction.CHANGE_COMPETENCY,
            score=4,
            evidence_strength=EvidenceStrength.STRONG,
        ),
    )

    assert result["next_action"] == "CHANGE_COMPETENCY"
    assert result["current_competency"] == "python"


def test_low_rubric_score_decreases_difficulty_and_keeps_follow_up_targeted():
    agent = InterviewAgentCore()
    result = agent.handle_answer(
        agent.start_interview("candidate", "AI Engineer"),
        "I built a service",
        evaluation=evaluation(
            SuggestedNextAction.DECREASE_DIFFICULTY,
            score=2,
            gaps=["failure handling"],
        ),
    )

    assert result["next_action"] == "DECREASE_DIFFICULTY"
    assert result["difficulty"] == "easy"
    assert "failure handling" in result["current_question"]


def test_repeated_model_question_uses_unique_fallback():
    class RepeatingQuestionProvider:
        def generate_structured(self, system, content, schema):
            return schema(
                question="What concrete technique did you use, and what measurable result did it produce?",
                competency="general",
                difficulty="medium",
                intent="probe",
            )

    agent = InterviewAgentCore(llm_provider=RepeatingQuestionProvider())
    state = agent.start_interview("candidate", "AI Engineer")
    repeated = "What concrete technique did you use, and what measurable result did it produce?"
    result = agent.handle_answer(
        state,
        "I built a service",
        evaluation=evaluation(
            SuggestedNextAction.DIG_DEEPER,
            gaps=["failure handling"],
        ),
        asked_questions=[state["current_question"], repeated],
    )

    assert result["current_question"] != repeated


def test_question_budget_finishes_without_asking_an_extra_question():
    agent = InterviewAgentCore()
    state = agent.start_interview("candidate", "AI Engineer")
    state["question_budget"] = 1
    result = agent.handle_answer(
        state,
        "I built a service",
        evaluation=evaluation(SuggestedNextAction.DIG_DEEPER, gaps=["failure handling"]),
    )

    assert result["status"] == "completed"
    assert result["next_action"] == "FINISH"
    assert result["current_question"] == state["current_question"]
    assert len(result["questions_asked"]) == 1


def test_question_provider_failure_uses_unique_deterministic_fallback():
    from integrations.llm_providers.provider import LLMProviderError

    class FailingQuestionProvider:
        def generate_structured(self, system, content, schema):
            raise LLMProviderError("provider unavailable")

    agent = InterviewAgentCore(llm_provider=FailingQuestionProvider())
    result = agent.handle_answer(
        agent.start_interview("candidate", "AI Engineer"),
        "I built a service",
        evaluation=evaluation(SuggestedNextAction.DIG_DEEPER, gaps=["failure handling"]),
    )

    assert result["current_question"]
    assert result["question_source"] == "deterministic_fallback"
    assert any("question generation fallback" in error for error in result["errors"])


def test_invalid_structured_analysis_falls_back_without_corrupting_state():
    def invalid_analyzer(answer, state):
        return {"evidence_strength": "not-a-valid-strength"}

    result = InterviewAgentCore(analyzer=invalid_analyzer).handle_answer(
        InterviewAgentCore(analyzer=invalid_analyzer).start_interview("candidate", "AI Engineer"),
        "some answer",
    )

    assert result["history"]
    assert result["extracted_evidence"]["gaps"] == ["structured answer analysis unavailable"]


def test_analyzer_timeout_falls_back_without_completion_side_effects():
    def timed_out_analyzer(answer, state):
        raise TimeoutError("provider timeout")

    agent = InterviewAgentCore(analyzer=timed_out_analyzer)
    result = agent.handle_answer(agent.start_interview("candidate", "AI Engineer"), "some answer")

    assert result["history"]
    assert result["extracted_evidence"]["evidence"] == []
    assert "structured answer analysis unavailable" in result["identified_gaps"]
