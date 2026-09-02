"""Voice pipeline package scaffolding."""

from __future__ import annotations


class VoicePipeline:
    """Minimal placeholder for the future multimodal voice interview pipeline."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    async def transcribe(self, audio_bytes: bytes) -> str:
        return "Voice transcription is not enabled in the current minimal deployment."

    async def synthesize(self, text: str) -> bytes:
        return text.encode("utf-8")
