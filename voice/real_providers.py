"""Real streaming STT and TTS provider implementations for Phase 10."""
from __future__ import annotations
import asyncio
import logging
import json
import time
from typing import AsyncIterator
import httpx
from voice.contracts import STTEvent, AudioFormat, VoiceErrorCode

logger = logging.getLogger(__name__)

class STTProviderError(Exception):
    """Base STT provider error."""
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

class TTSProviderError(Exception):
    """Base TTS provider error."""
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

class AssemblyAISTTProvider:
    """Real streaming STT using AssemblyAI with WebSocket."""

    def __init__(self, api_key: str, model: str = "best", language: str = "en", timeout_seconds: float = 20.0, max_retries: int = 1) -> None:
        if not api_key:
            raise ValueError("AssemblyAI API key is required")
        self.api_key = api_key
        self.model = model
        self.language = language
        self.timeout = httpx.Timeout(timeout_seconds)
        self.max_retries = max_retries
        self.provider_name = "assemblylabs"
        self.sessions: dict[str, dict] = {}

    async def start_session(self, utterance_id: str, audio_format: AudioFormat) -> None:
        if audio_format.encoding != "pcm_s16le" or audio_format.sample_rate != 16000:
            raise STTProviderError("INVALID_AUDIO", "AssemblyAI requires PCM16 16kHz mono")
        self.sessions[utterance_id] = {
            "started_at": time.time(),
            "chunks": bytearray(),
            "interim_text": "",
            "final_text": "",
        }

    async def send_audio(self, utterance_id: str, audio: bytes) -> AsyncIterator[STTEvent]:
        if utterance_id not in self.sessions:
            raise STTProviderError("SESSION_ERROR", f"Session {utterance_id} not started")
        self.sessions[utterance_id]["chunks"].extend(audio)
        if len(self.sessions[utterance_id]["chunks"]) > 120 * 32000:
            raise STTProviderError("AUDIO_TOO_LARGE", "Utterance exceeds 120 seconds")

    async def end_utterance(self, utterance_id: str) -> AsyncIterator[STTEvent]:
        if utterance_id not in self.sessions:
            raise STTProviderError("SESSION_ERROR", f"Session {utterance_id} not started")
        session = self.sessions[utterance_id]
        audio_bytes = bytes(session["chunks"])
        if not audio_bytes:
            raise STTProviderError("STT_ERROR", "No audio data")

        started = time.time()
        attempt = 0
        while attempt <= self.max_retries:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        "https://api.assemblyai.com/v2/transcript",
                        headers={"Authorization": self.api_key},
                        json={
                            "audio_data": audio_bytes.hex(),
                            "encoding": "pcm_s16le",
                            "sample_rate": 16000,
                            "language": self.language,
                        },
                    )

                if response.status_code == 401:
                    raise STTProviderError("STT_AUTH_ERROR", "Invalid AssemblyAI API key")
                if response.status_code == 429:
                    raise STTProviderError("STT_RATE_LIMITED", "AssemblyAI rate limit exceeded")
                if response.status_code >= 500:
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(0.5, 0.1 * (2 ** attempt)))
                        attempt += 1
                        continue
                    raise STTProviderError("STT_ERROR", f"AssemblyAI server error: {response.status_code}")

                response.raise_for_status()
                result = response.json()
                transcript = result.get("text", "")
                confidence = result.get("confidence", 0.0) if "confidence" in result else None

                yield STTEvent(
                    utterance_id=utterance_id,
                    text=transcript,
                    is_final=True,
                    confidence=confidence,
                    timestamp=int(time.time() * 1000),
                )
                session["final_text"] = transcript
                return
            except httpx.TimeoutException:
                if attempt >= self.max_retries:
                    raise STTProviderError("STT_TIMEOUT", "AssemblyAI request timed out")
                attempt += 1
            except httpx.RequestError as exc:
                if attempt >= self.max_retries:
                    raise STTProviderError("STT_ERROR", f"Network error: {exc}")
                attempt += 1

        raise STTProviderError("STT_ERROR", "STT request failed after retries")

    async def close(self) -> None:
        self.sessions.clear()

class ElevenLabsTTSProvider:
    """Real streaming TTS using ElevenLabs."""

    def __init__(self, api_key: str, voice_id: str = "21m00Tcm4TlvDq8ikWAM", model: str = "eleven_monolingual_v1", timeout_seconds: float = 20.0, max_retries: int = 1) -> None:
        if not api_key:
            raise ValueError("ElevenLabs API key is required")
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self.timeout = httpx.Timeout(timeout_seconds)
        self.max_retries = max_retries
        self.provider_name = "elevenlabs"

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        if not text or not text.strip():
            return

        attempt = 0
        while attempt <= self.max_retries:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream",
                        headers={"xi-api-key": self.api_key},
                        json={
                            "text": text,
                            "model_id": self.model,
                            "voice_settings": {
                                "stability": 0.5,
                                "similarity_boost": 0.75,
                            },
                        },
                    )

                if response.status_code == 401:
                    raise TTSProviderError("TTS_AUTH_ERROR", "Invalid ElevenLabs API key")
                if response.status_code == 429:
                    raise TTSProviderError("TTS_RATE_LIMITED", "ElevenLabs rate limit exceeded")
                if response.status_code >= 500:
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(0.5, 0.1 * (2 ** attempt)))
                        attempt += 1
                        continue
                    raise TTSProviderError("TTS_ERROR", f"ElevenLabs server error: {response.status_code}")

                response.raise_for_status()
                async with response:
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if chunk:
                            yield chunk
                return
            except httpx.TimeoutException:
                if attempt >= self.max_retries:
                    raise TTSProviderError("TTS_TIMEOUT", "ElevenLabs request timed out")
                attempt += 1
            except httpx.RequestError as exc:
                if attempt >= self.max_retries:
                    raise TTSProviderError("TTS_ERROR", f"Network error: {exc}")
                attempt += 1

        raise TTSProviderError("TTS_ERROR", "TTS request failed after retries")

    async def close(self) -> None:
        pass
