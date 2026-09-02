from fastapi.testclient import TestClient

from apps.api.main import app


client = TestClient(app)


def auth_headers() -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "secret123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_start_interview_endpoint():
    response = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": "candidate-100", "position": "AI Engineer", "mode": "text"},
        headers=auth_headers(),
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
        headers=auth_headers(),
    )
    interview_id = start.json()["interview_id"]

    response = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={
            "turn_id": "turn-api-1",
            "answer": (
                "I have worked with Python, PyTorch, and YOLOv8 for computer vision. "
                "I also deployed a model to production and optimized inference latency."
            ),
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["skills"]["python"]["score"] >= 3
    assert payload["skills"]["pytorch"]["score"] >= 3
    assert payload["skills"]["computer_vision"]["score"] >= 3
    assert payload["overall_score"] >= 0


def test_duplicate_turn_id_replays_without_appending_history():
    headers = auth_headers()
    start = client.post(
        "/api/v1/interview/start",
        json={"candidate_id": "candidate-idempotent", "position": "AI Engineer", "mode": "text"},
        headers=headers,
    )
    interview_id = start.json()["interview_id"]
    request = {
        "turn_id": "turn-duplicate-1",
        "answer": "I deployed a Python service to production.",
    }

    first = client.post(f"/api/v1/interview/{interview_id}/answer", json=request, headers=headers)
    second = client.post(f"/api/v1/interview/{interview_id}/answer", json=request, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(first.json()["history"]) == 1
    assert second.json()["history"] == first.json()["history"]


def test_interview_endpoints_require_authentication():
    response = client.get("/api/v1/interviews")
    assert response.status_code == 401
