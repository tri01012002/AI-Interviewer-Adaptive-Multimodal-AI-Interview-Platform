"""Real-time WebSocket interview session endpoints."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from apps.api.v1.routes.interview import service as interview_service
from config import settings
from services.auth_service import decode_token
from services.support_access_audit import (
    MandatoryAuditUnavailable,
    persist_required_audit,
)
from services.tenant_authorization import TenantContext, resolve_tenant_context
from services.user_store import UserStore
from voice.contracts import ClientVoiceEvent, VoiceErrorCode, VoiceServerEvent
from voice.factory import create_providers
from voice.session import VoiceSessionService

router = APIRouter()


@router.websocket("/ws/interview/{interview_id}")
async def interview_socket(websocket: WebSocket, interview_id: str) -> None:
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401, reason="Authentication required")
        return
    try:
        claims = decode_token(token)
        user = UserStore.get_by_email(str(claims.get("sub", "")))
        if user is None:
            await websocket.close(code=4401, reason="Authentication failed")
            return
    except HTTPException:
        await websocket.close(code=4401, reason="Authentication failed")
        return

    context = await _authorize_websocket(websocket, interview_id, user)
    if context is None:
        return
    if context.is_platform_admin and not _audit_websocket_support(context, interview_id):
        await websocket.close(code=4503, reason="Required access audit unavailable")
        return
    state = interview_service.get_interview(
        interview_id,
        context.actor_user_id,
        context.role,
        context.organization_id,
    )
    if state is None:
        await websocket.close(code=4403, reason="Interview is not available")
        return
    try:
        await websocket.send_text(json.dumps({"type": "connected", "interview_id": interview_id, "status": "ready"}))
        while True:
            payload = await websocket.receive_text()
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                await websocket.close(code=4400, reason="Invalid message")
                return
            await websocket.send_text(
                json.dumps({"type": "message", "payload": data})
            )
    except WebSocketDisconnect:
        return


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
            await websocket.close(code=4401, reason="Authentication failed")
            return
    except HTTPException:
        await websocket.close(code=4401, reason="Authentication failed")
        return

    context = await _authorize_websocket(websocket, interview_id, user)
    if context is None:
        return
    if context.is_platform_admin and not _audit_websocket_support(context, interview_id):
        await websocket.close(code=4503, reason="Required access audit unavailable")
        return
    state = interview_service.get_interview(
        interview_id,
        context.actor_user_id,
        context.role,
        context.organization_id,
    )
    if state is None:
        await websocket.close(code=4403, reason="Interview is not available")
        return

    # Create providers based on configuration
    vad, stt, tts = create_providers()

    session = VoiceSessionService(
        interview_service,
        user["id"],
        context.role,
        context.organization_id,
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

            if message.get("type") == "websocket.receive" and "text" in message:
                payload = message["text"]
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

            elif message.get("type") == "websocket.receive" and "bytes" in message:
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
                responses = await session._audio_chunk_binary(current_utterance_id, audio_bytes)
                for response in responses:
                    await websocket.send_text(response.model_dump_json())

    except Exception:
        import logging
        logging.exception("Voice session error")
        try:
            error_response = session.error(VoiceErrorCode.SESSION_ERROR, "Internal error")
            await websocket.send_text(error_response.model_dump_json())
        except Exception:
            pass
    finally:
        await session.close()


async def _authorize_websocket(
    websocket: WebSocket,
    interview_id: str,
    user: dict[str, Any],
) -> TenantContext | None:
    await websocket.accept()
    try:
        authorization_text = await websocket.receive_text()
    except WebSocketDisconnect:
        return None
    if len(authorization_text) > 4096:
        if await _audit_denied_support(websocket, user, interview_id):
            return None
        await websocket.close(code=4400, reason="Authorization frame is too large")
        return None
    try:
        authorization_frame = json.loads(authorization_text)
    except json.JSONDecodeError:
        if await _audit_denied_support(websocket, user, interview_id):
            return None
        await websocket.close(code=4403, reason="Organization authorization required")
        return None
    if (
        not isinstance(authorization_frame, dict)
        or authorization_frame.get("type") != "authorize"
        or (
            authorization_frame.get("organization_id") is not None
            and not isinstance(authorization_frame.get("organization_id"), str)
        )
        or (
            authorization_frame.get("support_reason") is not None
            and not isinstance(authorization_frame.get("support_reason"), str)
        )
    ):
        if await _audit_denied_support(
            websocket, user, interview_id, authorization_frame
        ):
            return None
        await websocket.close(code=4403, reason="Organization authorization required")
        return None

    organization_id = authorization_frame.get("organization_id")
    support_reason = authorization_frame.get("support_reason")
    try:
        return resolve_tenant_context(user, organization_id, support_reason)
    except HTTPException:
        if await _audit_denied_support(
            websocket, user, interview_id, authorization_frame
        ):
            return None
        await websocket.close(code=4403, reason="Organization authorization failed")
        return None


async def _audit_denied_support(
    websocket: WebSocket,
    user: dict[str, Any],
    interview_id: str,
    authorization_frame: Any = None,
) -> bool:
    if user["role"] != "admin":
        return False
    organization_id = (
        authorization_frame.get("organization_id")
        if isinstance(authorization_frame, dict)
        and isinstance(authorization_frame.get("organization_id"), str)
        else None
    )
    support_reason = (
        authorization_frame.get("support_reason")
        if isinstance(authorization_frame, dict)
        and isinstance(authorization_frame.get("support_reason"), str)
        else None
    )
    if _audit_websocket_support_values(
        user["id"],
        organization_id,
        support_reason,
        interview_id,
    ):
        return False
    await websocket.close(code=4503, reason="Required access audit unavailable")
    return True


def _audit_websocket_support(
    context: TenantContext,
    interview_id: str,
) -> bool:
    return _audit_websocket_support_values(
        context.actor_user_id,
        context.organization_id,
        context.support_reason,
        interview_id,
    )


def _audit_websocket_support_values(
    actor_user_id: str,
    requested_organization_id: str | None,
    support_reason: str | None,
    interview_id: str,
) -> bool:
    requested_organization_id = (
        requested_organization_id
        if requested_organization_id and len(requested_organization_id) <= 256
        else None
    )
    reason = support_reason.strip() if support_reason else None
    if reason and (len(reason) > 500 or "\r" in reason or "\n" in reason):
        reason = None
    try:
        persist_required_audit(
            request_id=str(uuid4()),
            actor_user_id=actor_user_id,
            organization_id=requested_organization_id,
            requested_organization_id=requested_organization_id,
            action="websocket_access_attempt",
            resource_type="interview",
            resource_id=interview_id[:128] or "unknown",
            support_reason=reason,
        )
    except MandatoryAuditUnavailable:
        return False
    return True
