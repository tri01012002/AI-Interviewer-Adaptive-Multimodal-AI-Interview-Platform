from agents.interview_agent.graph import AnswerAnalysis, InterviewAgentCore


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


def test_graph_changes_competency_for_weak_answer():
    agent = InterviewAgentCore()
    result = agent.handle_answer(agent.start_interview("candidate", "AI Engineer"), "I worked on a project.")

    assert result["next_action"] == "CHANGE_COMPETENCY"
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
    assert "not covered" in state["current_question"]


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