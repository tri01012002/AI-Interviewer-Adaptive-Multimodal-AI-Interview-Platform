import base64

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.api.main import app
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    start_interview,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_authenticated_voice_websocket_final_flow_and_duplicate_final(
    client: TestClient,
):
    tenant = create_tenant(client)
    candidate, candidate_headers, profile, _invitation = (
        create_candidate_through_invitation(client, tenant)
    )
    start = start_interview(client, tenant, profile["candidate_id"])
    token = candidate_headers["Authorization"].removeprefix("Bearer ")
    organization_id = tenant.organization_id

    with client.websocket_connect(
        f"/ws/interview/{start['interview_id']}/voice?token={token}"
    ) as websocket:
        websocket.send_json(
            {"type": "authorize", "organization_id": organization_id}
        )
        ready = websocket.receive_json()
        assert ready["type"] == "session.ready"
        websocket.send_json({"type": "session.start"})
        assert websocket.receive_json()["type"] == "session.ready"
        websocket.send_json(
            {"type": "audio.start", "utterance_id": "voice-utt-1"}
        )
        websocket.send_json(
            {
                "type": "audio.chunk",
                "utterance_id": "voice-utt-1",
                "audio_format": "pcm_s16le/16000/1",
                "audio_base64": base64.b64encode(b"pcm").decode("ascii"),
            }
        )
        assert websocket.receive_json()["type"] == "transcript.partial"
        websocket.send_json(
            {"type": "audio.end", "utterance_id": "voice-utt-1"}
        )
        events = []
        while True:
            event = websocket.receive_json()
            events.append(event)
            if event["type"] == "tts.completed":
                break
        assert events[0]["type"] == "transcript.final"
        assert events[1]["type"] == "interview.question"
        assert events[2]["type"] == "tts.started"
        assert events[-1]["type"] == "tts.completed"

        websocket.send_json(
            {"type": "audio.end", "utterance_id": "voice-utt-1"}
        )
        duplicate_events = [websocket.receive_json() for _ in range(5)]
        assert duplicate_events[0]["type"] == "interview.question"
        assert not any(event["type"] == "transcript.final" for event in duplicate_events)


def test_unauthenticated_voice_websocket_is_rejected(client: TestClient):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/interview/missing/voice"):
            pass
