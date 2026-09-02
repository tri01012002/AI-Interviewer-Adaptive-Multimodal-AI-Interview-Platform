"""Production-ready voice processing manager with clear integration points for STT/TTS providers."""

from __future__ import annotations

from typing import Any

from voice.pipeline.stt import STTService
from voice.pipeline.tts import TTSService


class VoiceInterviewManager:
    """Encapsulates customer-facing voice interview operations."""

    def __init__(self, stt_service: STTService | None = None, tts_service: TTSService | None = None) -> None:
        self.stt_service = stt_service or STTService()
        self.tts_service = tts_service or TTSService()

    async def handle_audio_input(self, audio_bytes: bytes) -> str:
        return await self.stt_service.transcribe(audio_bytes)

    async def handle_text_output(self, text: str) -> bytes:
        return await self.tts_service.synthesize(text)

    async def process_turn(self, audio_bytes: bytes | None, text: str | None = None) -> dict[str, Any]:
        transcript = await self.handle_audio_input(audio_bytes) if audio_bytes else (text or "")
        audio_payload = await self.handle_text_output(transcript) if transcript else b""
        return {"transcript": transcript, "audio_bytes": audio_payload}
