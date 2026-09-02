from agents.interview_agent.graph import InterviewAgentCore


def test_initial_interview_question_and_state():
    agent = InterviewAgentCore()
    state = agent.start_interview(candidate_id="candidate-001", position="AI Engineer")

    assert state["candidate_id"] == "candidate-001"
    assert state["position"] == "AI Engineer"
    assert "introduce" in state["current_question"].lower()
    assert state["questions_asked"]
    assert state["skills"] == {}


def test_candidate_answer_updates_skill_evidence_and_next_question():
    agent = InterviewAgentCore()
    state = agent.start_interview(candidate_id="candidate-002", position="AI Engineer")

    answer = (
        "I have worked with Python, PyTorch, and YOLOv8 for computer vision. "
        "I also deployed a model to production and optimized inference latency."
    )
    next_state = agent.handle_answer(state, answer)

    assert next_state["skills"]["python"]["score"] >= 3
    assert next_state["skills"]["pytorch"]["score"] >= 3
    assert next_state["skills"]["computer_vision"]["score"] >= 3
    assert next_state["questions_asked"]
    assert "production" in next_state["current_question"].lower() or "latency" in next_state["current_question"].lower()
