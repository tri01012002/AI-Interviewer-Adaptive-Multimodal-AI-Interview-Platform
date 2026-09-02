"""Text-to-speech service adapter placeholder for the voice pipeline."""

from __future__ import annotations


class TTSService:
    """Adapter interface for integrating an external TTS model later."""

    async def synthesize(self, text: str) -> bytes:
        return text.encode("utf-8")
