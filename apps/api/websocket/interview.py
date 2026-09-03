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
from voice.factory import create_providers

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
    """Authenticated voice gateway with binary audio frame support."""
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
    
    # Create providers based on configuration
    vad, stt, tts = create_providers()
    
    session = VoiceSessionService(
        interview_service,
        user["id"],
        user["role"],
        vad=vad,
        stt=stt,
        tts=tts,
        max_chunk_bytes=settings.VOICE_MAX_AUDIO_CHUNK_BYTES,
        max_buffer_bytes=settings.VOICE_MAX_BUFFER_BYTES,
        max_utterance_seconds=settings.VOICE_MAX_UTTERANCE_SECONDS,
    )
    try:
        await websocket.send_text(VoiceServerEvent(type="session.ready", session_id=session.session_id).model_dump_json())
        
        # Track current audio session for binary frame handling
        current_utterance_id: str | None = None
        
        while True:
            # Support both JSON text messages (control) and binary frames (audio)
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                break
            
            if message.get("type") == "text":
                payload = message.get("text", "")
                try:
                    event = ClientVoiceEvent.parse_json(payload, settings.VOICE_WS_MAX_MESSAGE_BYTES)
                    responses = await session.handle(interview_id, event)
                    
                    # Track utterance_id for binary audio
                    if event.type.value == "audio.start":
                        current_utterance_id = event.utterance_id
                    elif event.type.value == "audio.end":
                        current_utterance_id = None
                except ValueError as exc:
                    responses = [session.error(VoiceErrorCode.INVALID_EVENT, str(exc))]
                
                for response in responses:
                    await websocket.send_text(response.model_dump_json())
            
            elif message.get("type") == "bytes":
                # Binary audio frame handling
                audio_bytes = message.get("bytes", b"")
                if not current_utterance_id:
                    error_response = session.error(VoiceErrorCode.PROTOCOL_ERROR, "No active audio session")
                    await websocket.send_text(error_response.model_dump_json())
                    continue
                
                if len(audio_bytes) > settings.VOICE_MAX_AUDIO_CHUNK_BYTES:
                    error_response = session.error(VoiceErrorCode.AUDIO_TOO_LARGE, "Audio chunk exceeds size limit")
                    await websocket.send_text(error_response.model_dump_json())
                    continue
                
                # Process binary audio as a chunk
                import base64
                responses = await session._audio_chunk_binary(current_utterance_id, audio_bytes)
                for response in responses:
                    await websocket.send_text(response.model_dump_json())
    
    except Exception as exc:
        import logging
        logging.exception("Voice session error")
        try:
            error_response = session.error(VoiceErrorCode.SESSION_ERROR, "Internal error")
            await websocket.send_text(error_response.model_dump_json())
        except Exception:
            pass
    finally:
        await session.close()
