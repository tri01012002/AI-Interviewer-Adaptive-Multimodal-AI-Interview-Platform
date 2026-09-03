"""Typed voice protocol contracts."""
from __future__ import annotations
from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, Field

class VoiceEventType(StrEnum):
    SESSION_START = "session.start"
    AUDIO_START = "audio.start"
    AUDIO_CHUNK = "audio.chunk"
    AUDIO_END = "audio.end"
    UTTERANCE_CANCEL = "utterance.cancel"
    SESSION_STOP = "session.stop"

class VoiceErrorCode(StrEnum):
    INVALID_EVENT = "VOICE_INVALID_EVENT"
    INVALID_AUDIO = "VOICE_INVALID_AUDIO"
    AUDIO_TOO_LARGE = "VOICE_AUDIO_TOO_LARGE"
    BUFFER_OVERFLOW = "VOICE_BUFFER_OVERFLOW"
    INTERVIEW_FORBIDDEN = "VOICE_INTERVIEW_FORBIDDEN"
    STT_ERROR = "STT_PROVIDER_ERROR"
    STT_TIMEOUT = "STT_TIMEOUT"
    STT_RATE_LIMITED = "STT_RATE_LIMITED"
    STT_AUTH_ERROR = "STT_AUTH_ERROR"
    TURN_COMMIT_FAILED = "TURN_COMMIT_FAILED"
    TTS_ERROR = "TTS_PROVIDER_ERROR"
    TTS_TIMEOUT = "TTS_TIMEOUT"
    TTS_RATE_LIMITED = "TTS_RATE_LIMITED"
    PROTOCOL_ERROR = "VOICE_PROTOCOL_ERROR"
    SESSION_ERROR = "VOICE_SESSION_ERROR"

class ClientVoiceEvent(BaseModel):
    type: VoiceEventType
    utterance_id: str | None = Field(default=None, min_length=1)
    audio_format: str | None = None
    audio_base64: str | None = None
    timestamp: int | None = None

    @classmethod
    def parse_json(cls, payload: str, max_bytes: int) -> "ClientVoiceEvent":
        if len(payload.encode("utf-8")) > max_bytes:
            raise ValueError("voice message exceeds configured size")
        return cls.model_validate_json(payload)

class VoiceServerEvent(BaseModel):
    type: str
    session_id: str | None = None
    utterance_id: str | None = None
    text: str | None = None
    audio_base64: str | None = None
    question: str | None = None
    code: VoiceErrorCode | None = None
    message: str | None = None
    timestamp: int | None = None
    provider: str | None = None
    model: str | None = None
    confidence: float | None = None

class STTEvent(BaseModel):
    utterance_id: str
    text: str
    is_final: bool = False
    confidence: float | None = None
    timestamp: int | None = None

class AudioFormat(BaseModel):
    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1

class TTSAudioEvent(BaseModel):
    audio_bytes: bytes
    is_final: bool = False
    sequence: int = 0
