"""Real-time WebSocket interview session endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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

logger = logging.getLogger(__name__)

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
    await _run_voice_session(websocket, interview_id, session)


async def _run_voice_session(
    websocket: WebSocket, interview_id: str, session: VoiceSessionService
) -> None:
    await _VoiceConnection(websocket, interview_id, session).run()


class _VoiceConnection:
    """Drive one voice session over a WebSocket.

    The receive loop never blocks on answer processing: ``audio.end`` runs in
    one background task per session (a second ``audio.end`` while it runs gets
    a retryable ``VOICE_TURN_IN_PROGRESS``), and its events, including each
    ``tts.audio`` chunk, are sent as they are produced. Disconnect, cancel and
    ``session.stop`` cancel that task; nothing is sent after the socket closes.
    """

    def __init__(
        self, websocket: WebSocket, interview_id: str, session: VoiceSessionService
    ) -> None:
        self.websocket = websocket
        self.interview_id = interview_id
        self.session = session
        self.closed = False
        self.send_lock = asyncio.Lock()
        self.processing: asyncio.Task | None = None
        self.processing_utterance: str | None = None
        # Utterance that binary audio frames are appended to.
        self.current_utterance_id: str | None = None

    async def run(self) -> None:
        try:
            await self.send(
                VoiceServerEvent(type="session.ready", session_id=self.session.session_id)
            )
            while not self.closed:
                message = await self.websocket.receive()
                message_type = message.get("type")
                if message_type == "websocket.disconnect":
                    break
                if message_type != "websocket.receive":
                    continue
                if message.get("text") is not None:
                    await self.on_text(message["text"])
                elif message.get("bytes") is not None:
                    await self.on_bytes(message["bytes"])
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("Voice session error")
            await self.send(self.session.error(VoiceErrorCode.SESSION_ERROR, "Internal error"))
        finally:
            self.closed = True
            await self.stop_processing()
            await self.session.close()

    async def send(self, event: VoiceServerEvent) -> None:
        if self.closed:
            return
        async with self.send_lock:
            if self.closed:
                return
            try:
                await self.websocket.send_text(event.model_dump_json())
            except (WebSocketDisconnect, RuntimeError, OSError):
                # Client went away mid-send; stop sending. The receive loop
                # observes the disconnect and cleans up.
                self.closed = True

    async def pump(self, events) -> bool:
        """Send every event from ``events``; return True if any was an error."""
        had_error = False
        async with contextlib.aclosing(events) as stream:
            async for event in stream:
                had_error = had_error or event.type == "error"
                await self.send(event)
        return had_error

    async def stop_processing(self) -> None:
        task = self.processing
        self.processing, self.processing_utterance = None, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        # gather(return_exceptions) absorbs the child's CancelledError but
        # still propagates cancellation of this coroutine itself.
        (outcome,) = await asyncio.gather(task, return_exceptions=True)
        if isinstance(outcome, Exception):
            logger.error("Voice answer processing failed (%s)", type(outcome).__name__)

    async def on_text(self, payload: str) -> None:
        try:
            event = ClientVoiceEvent.parse_json(payload, settings.VOICE_WS_MAX_MESSAGE_BYTES)
        except ValueError as exc:
            await self.send(self.session.error(VoiceErrorCode.INVALID_EVENT, str(exc)))
            return
        event_type = event.type.value
        if event_type == "audio.end":
            await self.start_answer(event)
            return
        if event_type == "utterance.cancel":
            if event.utterance_id is None or event.utterance_id == self.current_utterance_id:
                self.current_utterance_id = None
            if self.processing_utterance is not None and self.processing_utterance == event.utterance_id:
                # Stops transcript/TTS delivery; an answer already submitted
                # to the interview service still completes.
                await self.stop_processing()
        elif event_type == "session.stop":
            self.current_utterance_id = None
            await self.stop_processing()
        had_error = await self.pump(self.session.stream_events(self.interview_id, event))
        if event_type == "audio.start" and not had_error:
            self.current_utterance_id = event.utterance_id

    async def start_answer(self, event: ClientVoiceEvent) -> None:
        if self.processing is not None and not self.processing.done():
            await self.send(self.session.error(
                VoiceErrorCode.TURN_IN_PROGRESS,
                "An answer is already being processed; retry after it completes.",
                retryable=True,
            ))
            return
        await self.stop_processing()  # reap the finished task, if any
        self.current_utterance_id = None
        self.processing_utterance = event.utterance_id
        self.processing = asyncio.create_task(
            self.pump(self.session.stream_events(self.interview_id, event)),
            name=f"voice-answer-{self.session.session_id}",
        )

    async def on_bytes(self, audio_bytes: bytes) -> None:
        if not self.current_utterance_id:
            await self.send(self.session.error(VoiceErrorCode.PROTOCOL_ERROR, "No active audio session"))
            return
        if len(audio_bytes) > settings.VOICE_MAX_AUDIO_CHUNK_BYTES:
            await self.send(self.session.error(VoiceErrorCode.AUDIO_TOO_LARGE, "Audio chunk exceeds size limit"))
            return
        for response in await self.session._audio_chunk_binary(self.current_utterance_id, audio_bytes):
            await self.send(response)


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
