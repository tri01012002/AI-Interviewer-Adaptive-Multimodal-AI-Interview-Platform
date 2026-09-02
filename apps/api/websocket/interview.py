"""Real-time WebSocket interview session endpoints."""

from __future__ import annotations

import json
from urllib.parse import parse_qs
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from config import settings
from services.auth_service import decode_token
from services.user_store import UserStore
from apps.api.v1.routes.interview import service as interview_service
from voice.contracts import ClientVoiceEvent, VoiceErrorCode, VoiceServerEvent
from voice.session import VoiceSessionService

router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, interview_id: str) -> None:
        await websocket.accept()
        self.active_connections[interview_id] = websocket

    def disconnect(self, interview_id: str) -> None:
        self.active_connections.pop(interview_id, None)

    async def send_event(self, interview_id: str, event: dict[str, Any]) -> None:
        socket = self.active_connections.get(interview_id)
        if socket is not None:
            await socket.send_text(json.dumps(event))


manager = ConnectionManager()


@router.websocket("/ws/interview/{interview_id}")
async def interview_socket(websocket: WebSocket, interview_id: str) -> None:
    await manager.connect(websocket, interview_id)
    try:
        await websocket.send_text(json.dumps({"type": "connected", "interview_id": interview_id, "status": "ready"}))
        while True:
            payload = await websocket.receive_text()
            data = json.loads(payload)
            await manager.send_event(interview_id, {"type": "message", "payload": data})
    except WebSocketDisconnect:
        manager.disconnect(interview_id)


@router.websocket("/ws/interview/{interview_id}/voice")
async def voice_interview_socket(websocket: WebSocket, interview_id: str) -> None:
    """Authenticated voice gateway; only final STT events reach InterviewService."""
    if not settings.VOICE_ENABLED:
        await websocket.close(code=4403, reason="Voice interviews are disabled")
        return
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401, reason="Authentication required")
        return
    try:
        claims = decode_token(token)
        user = UserStore.get_by_email(str(claims.get("sub", "")))
        if user is None:
            raise ValueError("user not found")
        state = interview_service.get_interview(interview_id, user["id"], user["role"])
        if state is None:
            await websocket.close(code=4403, reason="Interview is not available")
            return
    except Exception:
        await websocket.close(code=4401, reason="Authentication failed")
        return

    await websocket.accept()
    session = VoiceSessionService(
        interview_service,
        user["id"],
        user["role"],
        max_chunk_bytes=settings.VOICE_MAX_AUDIO_CHUNK_BYTES,
        max_buffer_bytes=settings.VOICE_MAX_BUFFER_BYTES,
        max_utterance_seconds=settings.VOICE_MAX_UTTERANCE_SECONDS,
    )
    try:
        await websocket.send_text(VoiceServerEvent(type="session.ready", session_id=session.session_id).model_dump_json())
        while True:
            payload = await websocket.receive_text()
            try:
                event = ClientVoiceEvent.parse_json(payload, settings.VOICE_WS_MAX_MESSAGE_BYTES)
                responses = await session.handle(interview_id, event)
            except ValueError as exc:
                responses = [session.error(VoiceErrorCode.INVALID_EVENT, str(exc))]
            for response in responses:
                await websocket.send_text(response.model_dump_json())
    except WebSocketDisconnect:
        await session.close()
    finally:
        await session.close()
