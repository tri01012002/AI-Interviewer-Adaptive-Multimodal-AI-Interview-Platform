"""Offline websocket tests: responsiveness, disconnect during processing,
cancel resets binary utterance, clean normal disconnect. Fake providers only."""
from __future__ import annotations

import base64
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.websocket import interview as ws_module
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    start_interview,
)

SAFETY_SECONDS = 10.0


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def voice_target(client: TestClient):
    tenant = create_tenant(client)
    _candidate, headers, profile, _invitation = create_candidate_through_invitation(client, tenant)
    start = start_interview(client, tenant, profile["candidate_id"])
    token = headers["Authorization"].removeprefix("Bearer ")
    return f"/ws/interview/{start['interview_id']}/voice?token={token}", tenant.organization_id


def connect(client: TestClient, url: str, organization_id: str):
    websocket = client.websocket_connect(url)
    socket = websocket.__enter__()
    socket.send_json({"type": "authorize", "organization_id": organization_id})
    assert socket.receive_json()["type"] == "session.ready"
    return websocket, socket


def send_utterance(socket, utterance_id: str) -> None:
    socket.send_json({"type": "audio.start", "utterance_id": utterance_id})
    socket.send_json({
        "type": "audio.chunk",
        "utterance_id": utterance_id,
        "audio_format": "pcm_s16le/16000/1",
        "audio_base64": base64.b64encode(b"pcm").decode("ascii"),
    })
    assert socket.receive_json()["type"] == "transcript.partial"


class BlockingSubmit:
    """Wraps the real submit_answer; blocks on a threading.Event first."""

    def __init__(self, original) -> None:
        self.original = original
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.results: list[dict] = []
        timer = threading.Timer(SAFETY_SECONDS, self.release.set)
        timer.daemon = True
        timer.start()

    def __call__(self, *args, **kwargs):
        self.entered.set()
        try:
            self.release.wait()
            result = self.original(*args, **kwargs)
            self.results.append(result)
            return result
        finally:
            self.finished.set()


def receive_until(socket, final_type: str) -> list[dict]:
    events = []
    while True:
        event = socket.receive_json()
        events.append(event)
        if event["type"] == final_type or event["type"] == "error":
            return events


def test_socket_stays_responsive_while_answer_blocked(client, voice_target, monkeypatch):
    url, organization_id = voice_target
    blocker = BlockingSubmit(ws_module.interview_service.submit_answer)
    monkeypatch.setattr(ws_module.interview_service, "submit_answer", blocker)

    first_cm, first = connect(client, url, organization_id)
    second_cm, second = connect(client, url, organization_id)
    try:
        send_utterance(first, "ws-utt-1")
        first.send_json({"type": "audio.end", "utterance_id": "ws-utt-1"})
        assert first.receive_json()["type"] == "transcript.final"
        assert blocker.entered.wait(5)

        # Same socket: control events are still answered.
        first.send_json({"type": "session.start"})
        assert first.receive_json()["type"] == "session.ready"
        first.send_json({"type": "audio.end", "utterance_id": "ws-utt-2"})
        busy = first.receive_json()
        assert busy["code"] == "VOICE_TURN_IN_PROGRESS" and busy["retryable"] is True

        # Another socket: full round trip while the first answer is blocked.
        second.send_json({"type": "session.start"})
        assert second.receive_json()["type"] == "session.ready"
        assert not blocker.release.is_set()

        blocker.release.set()
        events = receive_until(first, "tts.completed")
        types = [event["type"] for event in events]
        assert types[:2] == ["interview.question", "tts.started"]
        assert types[-1] == "tts.completed"
        assert "tts.audio" in types
    finally:
        blocker.release.set()
        first_cm.__exit__(None, None, None)
        second_cm.__exit__(None, None, None)


def test_disconnect_during_answer_processing_completes_turn_quietly(client, voice_target, monkeypatch, caplog):
    url, organization_id = voice_target
    original_submit = ws_module.interview_service.submit_answer
    blocker = BlockingSubmit(original_submit)
    monkeypatch.setattr(ws_module.interview_service, "submit_answer", blocker)
    caplog.set_level(logging.WARNING)

    websocket_cm, socket = connect(client, url, organization_id)
    send_utterance(socket, "ws-disc-1")
    socket.send_json({"type": "audio.end", "utterance_id": "ws-disc-1"})
    assert socket.receive_json()["type"] == "transcript.final"
    assert blocker.entered.wait(5)
    websocket_cm.__exit__(None, None, None)  # client disconnects mid-processing

    blocker.release.set()
    assert blocker.finished.wait(5)
    assert blocker.results, "the submitted DB turn must complete"
    committed_question = blocker.results[0]["current_question"]

    # Retrying the same utterance on a new socket is idempotent (DB turn_id).
    monkeypatch.setattr(ws_module.interview_service, "submit_answer", original_submit)
    retry_cm, retry = connect(client, url, organization_id)
    try:
        send_utterance(retry, "ws-disc-1")
        retry.send_json({"type": "audio.end", "utterance_id": "ws-disc-1"})
        events = receive_until(retry, "tts.completed")
        question = next(event for event in events if event["type"] == "interview.question")
        assert question["question"] == committed_question
        assert events[-1]["type"] == "tts.completed"
    finally:
        retry_cm.__exit__(None, None, None)

    noisy = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith(("apps.api.websocket", "voice", "root"))]
    assert noisy == []


def test_cancel_resets_binary_utterance(client, voice_target):
    url, organization_id = voice_target
    websocket_cm, socket = connect(client, url, organization_id)
    try:
        socket.send_json({"type": "audio.start", "utterance_id": "ws-cancel-1"})
        socket.send_bytes(b"\x00\x01" * 10)
        assert socket.receive_json()["type"] == "transcript.partial"
        socket.send_json({"type": "utterance.cancel", "utterance_id": "ws-cancel-1"})
        socket.send_bytes(b"\x00\x01" * 10)
        error = socket.receive_json()
        assert error["code"] == "VOICE_PROTOCOL_ERROR"
        # JSON chunks for the cancelled utterance are rejected as well.
        socket.send_json({
            "type": "audio.chunk",
            "utterance_id": "ws-cancel-1",
            "audio_base64": base64.b64encode(b"late").decode("ascii"),
        })
        assert socket.receive_json()["code"] == "VOICE_PROTOCOL_ERROR"
        # Restarting works and a later utterance completes normally.
        send_utterance(socket, "ws-cancel-2")
        socket.send_json({"type": "audio.end", "utterance_id": "ws-cancel-2"})
        events = receive_until(socket, "tts.completed")
        assert events[0]["type"] == "transcript.final"
        assert events[-1]["type"] == "tts.completed"
    finally:
        websocket_cm.__exit__(None, None, None)


def test_normal_disconnect_is_not_logged_as_error(client, voice_target, caplog):
    url, organization_id = voice_target
    caplog.set_level(logging.DEBUG)
    websocket_cm, socket = connect(client, url, organization_id)
    socket.send_json({"type": "session.start"})
    assert socket.receive_json()["type"] == "session.ready"
    websocket_cm.__exit__(None, None, None)
    # Let the server side observe the disconnect message.
    with client.websocket_connect(url) as probe:
        probe.send_json({"type": "authorize", "organization_id": organization_id})
        assert probe.receive_json()["type"] == "session.ready"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not any("Voice session error" in r.getMessage() for r in caplog.records)
