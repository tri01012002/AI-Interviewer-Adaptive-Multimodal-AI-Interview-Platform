"""Bounded voice session orchestration over the existing interview service."""
from __future__ import annotations
import base64
import binascii
import logging
from collections import defaultdict
from uuid import uuid4
from voice.contracts import AudioFormat, ClientVoiceEvent, VoiceErrorCode, VoiceServerEvent
from voice.providers import FakeSTTProvider, FakeTTSProvider, FakeVADProvider, STTProvider, TTSProvider, VADProvider

logger = logging.getLogger(__name__)

class VoiceSessionService:
    def __init__(self, interview_service, user_id: str, role: str, *, vad: VADProvider | None = None, stt: STTProvider | None = None, tts: TTSProvider | None = None, max_chunk_bytes: int = 64 * 1024, max_buffer_bytes: int = 2 * 1024 * 1024, max_utterance_seconds: int = 120) -> None:
        self.interview_service = interview_service
        self.user_id = user_id
        self.role = role
        self.session_id = str(uuid4())
        self.vad = vad or FakeVADProvider()
        self.stt = stt or FakeSTTProvider()
        self.tts = tts or FakeTTSProvider()
        self.max_chunk_bytes = max_chunk_bytes
        self.max_buffer_bytes = max_buffer_bytes
        self.max_utterance_bytes = max_utterance_seconds * 32000
        self.audio_format = AudioFormat()
        self.buffers: dict[str, bytearray] = defaultdict(bytearray)
        self.committed: dict[str, dict] = {}

    async def handle(self, interview_id: str, event: ClientVoiceEvent) -> list[VoiceServerEvent]:
        if event.type.value == "session.start":
            state = self.interview_service.get_interview(interview_id, self.user_id, self.role)
            if state is None:
                return [self.error(VoiceErrorCode.INTERVIEW_FORBIDDEN, "Interview is not available.")]
            return [VoiceServerEvent(type="session.ready", session_id=self.session_id)]
        if event.type.value == "audio.start":
            if not event.utterance_id:
                return [self.error(VoiceErrorCode.INVALID_EVENT, "utterance_id is required.")]
            await self.stt.start_session(event.utterance_id, self.audio_format)
            return []
        if event.type.value == "audio.chunk":
            return await self._audio_chunk(event)
        if event.type.value == "audio.end":
            return await self._audio_end(interview_id, event)
        if event.type.value == "utterance.cancel":
            if event.utterance_id:
                self.buffers.pop(event.utterance_id, None)
            return []
        if event.type.value == "session.stop":
            await self.close()
            return [VoiceServerEvent(type="session.closed", session_id=self.session_id)]
        return [self.error(VoiceErrorCode.INVALID_EVENT, "Unsupported voice event.")]

    async def _audio_chunk(self, event: ClientVoiceEvent) -> list[VoiceServerEvent]:
        if not event.utterance_id or not event.audio_base64:
            return [self.error(VoiceErrorCode.INVALID_EVENT, "utterance_id and audio_base64 are required.")]
        if event.audio_format and event.audio_format != "pcm_s16le/16000/1":
            return [self.error(VoiceErrorCode.INVALID_AUDIO, "Unsupported audio format.")]
        try:
            audio = base64.b64decode(event.audio_base64, validate=True)
        except (ValueError, binascii.Error):
            return [self.error(VoiceErrorCode.INVALID_AUDIO, "Audio must be valid base64 PCM16.")]
        if len(audio) > self.max_chunk_bytes:
            return [self.error(VoiceErrorCode.AUDIO_TOO_LARGE, "Audio chunk exceeds the size limit.")]
        if sum(len(item) for item in self.buffers.values()) + len(audio) > self.max_buffer_bytes:
            return [self.error(VoiceErrorCode.BUFFER_OVERFLOW, "Audio buffer is full.")]
        if len(self.buffers[event.utterance_id]) + len(audio) > self.max_utterance_bytes:
            return [self.error(VoiceErrorCode.AUDIO_TOO_LARGE, "Utterance exceeds the duration limit.")]
        self.buffers[event.utterance_id].extend(audio)
        if not await self.vad.detect(audio, self.audio_format):
            return []
        events = []
        async for transcript in self.stt.send_audio(event.utterance_id, audio):
            if not transcript.is_final:
                events.append(VoiceServerEvent(type="transcript.partial", session_id=self.session_id, utterance_id=transcript.utterance_id, text=transcript.text))
        return events

    async def _audio_end(self, interview_id: str, event: ClientVoiceEvent) -> list[VoiceServerEvent]:
        if not event.utterance_id:
            return [self.error(VoiceErrorCode.INVALID_EVENT, "utterance_id is required.")]
        events: list[VoiceServerEvent] = []
        async for transcript in self.stt.end_utterance(event.utterance_id):
            if not transcript.is_final or not transcript.text.strip():
                return [self.error(VoiceErrorCode.STT_ERROR, "STT did not produce a final transcript.")]
            if event.utterance_id in self.committed:
                result = self.committed[event.utterance_id]
            else:
                events.append(VoiceServerEvent(type="transcript.final", session_id=self.session_id, utterance_id=event.utterance_id, text=transcript.text))
                try:
                    result = self.interview_service.submit_answer(interview_id, transcript.text, event.utterance_id, self.user_id, self.role)
                except Exception:
                    self.buffers.pop(event.utterance_id, None)
                    return [self.error(VoiceErrorCode.TURN_COMMIT_FAILED, "Interview processing failed; please retry.")]
                self.committed[event.utterance_id] = result
            events.append(VoiceServerEvent(type="interview.question", session_id=self.session_id, utterance_id=event.utterance_id, question=result.get("current_question")))
            events.append(VoiceServerEvent(type="tts.started", session_id=self.session_id, utterance_id=event.utterance_id, question=result.get("current_question")))
            try:
                async for audio in self.tts.stream(result.get("current_question", "")):
                    events.append(VoiceServerEvent(type="tts.audio", session_id=self.session_id, utterance_id=event.utterance_id, audio_base64=base64.b64encode(audio).decode("ascii")))
                events.append(VoiceServerEvent(type="tts.completed", session_id=self.session_id, utterance_id=event.utterance_id))
            except Exception:
                events.append(self.error(VoiceErrorCode.TTS_ERROR, "Audio response is unavailable; use the displayed question."))
        self.buffers.pop(event.utterance_id, None)
        return events

    @staticmethod
    def error(code: VoiceErrorCode, message: str) -> VoiceServerEvent:
        return VoiceServerEvent(type="error", code=code, message=message)

    async def close(self) -> None:
        self.buffers.clear()
        await self.stt.close()
        await self.tts.close()
