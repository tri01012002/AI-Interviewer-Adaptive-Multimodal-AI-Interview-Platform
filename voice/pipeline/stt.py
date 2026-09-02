"""Speech-to-text service adapter placeholder for the voice pipeline."""

from __future__ import annotations


class STTService:
    """Adapter interface for integrating an external STT model later."""

    async def transcribe(self, audio_bytes: bytes) -> str:
        if not audio_bytes:
            return ""
        return "Transcription via STT provider is not configured in this deployment."
