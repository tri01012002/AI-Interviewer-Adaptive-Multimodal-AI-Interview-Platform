from fastapi.testclient import TestClient

from apps.api.main import app


client = TestClient(app)


def test_start_interview_endpoint():
    response = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": "candidate-100", "position": "AI Engineer", "mode": "text"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate_id"] == "candidate-100"
    assert payload["position"] == "AI Engineer"
    assert "introduce" in payload["current_question"].lower()


def test_submit_answer_endpoint_updates_state_and_scores():
    start = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": "candidate-200", "position": "AI Engineer", "mode": "text"},
    )
    interview_id = start.json()["interview_id"]

    response = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={
            "answer": (
                "I have worked with Python, PyTorch, and YOLOv8 for computer vision. "
                "I also deployed a model to production and optimized inference latency."
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["skills"]["python"]["score"] >= 3
    assert payload["skills"]["pytorch"]["score"] >= 3
    assert payload["skills"]["computer_vision"]["score"] >= 3
    assert payload["overall_score"] >= 0
