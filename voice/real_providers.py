"""Real streaming STT and TTS provider implementations for Phase 10."""
from __future__ import annotations
import asyncio
import io
import logging
import json
import re
import time
import wave
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

ASSEMBLYAI_BASE_URL = "https://api.assemblyai.com"
# STT_MODEL values that mean "let AssemblyAI pick its account default model".
# "best"/"nano" were values of the deprecated `speech_model` field.
_ASSEMBLYAI_DEFAULT_MODEL_ALIASES = {"", "best", "default", "fake", "local-fake"}
_ASSEMBLYAI_TERMINAL_STATUSES = {"completed", "error"}
# Transcript ids are UUIDs; reject anything that could alter the poll URL path.
_TRANSCRIPT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _pcm16_mono_16k_to_wav(pcm: bytes) -> bytes:
    """Wrap headerless PCM16 16 kHz mono audio in a WAV (RIFF) container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


def _assemblyai_language_code(language: str) -> str | None:
    """Map BCP-47-ish settings ("en-US") to AssemblyAI codes ("en_us").

    Returns None for empty/"auto", meaning AssemblyAI language detection.
    """
    normalized = (language or "").strip().lower().replace("-", "_")
    if normalized in {"", "auto"}:
        return None
    return normalized


def _assemblyai_json_body(response: httpx.Response) -> dict:
    """Normalize a non-5xx AssemblyAI response into a JSON object or raise.

    4xx errors are never retried and never echo the response body.
    """
    status_code = response.status_code
    if status_code in {401, 403}:
        raise STTProviderError("STT_AUTH_ERROR", "AssemblyAI rejected the API key")
    if status_code == 429:
        raise STTProviderError("STT_RATE_LIMITED", "AssemblyAI rate limit exceeded")
    if status_code >= 400:
        raise STTProviderError("STT_ERROR", f"AssemblyAI rejected the request: {status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise STTProviderError("STT_ERROR", "AssemblyAI returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise STTProviderError("STT_ERROR", "AssemblyAI returned an unexpected response")
    return body


class AssemblyAISTTProvider:
    """Utterance-buffered AssemblyAI STT over HTTP (pre-recorded/async API).

    This is NOT WebSocket streaming: audio is buffered per utterance and
    transcribed once when the utterance ends, using AssemblyAI's documented
    pre-recorded flow (docs consulted 2026-11-24):

    1. ``POST /v2/upload`` with the raw file bytes as the body
       (``Content-Type: application/octet-stream``, ``authorization: <key>``)
       -> ``{"upload_url": ...}``.
       https://www.assemblyai.com/docs/api-reference/files/upload
    2. ``POST /v2/transcript`` with JSON ``{"audio_url": upload_url, ...}``
       -> transcript object ``{"id", "status", ...}``.
       https://www.assemblyai.com/docs/api-reference/transcripts/submit
    3. Poll ``GET /v2/transcript/{id}`` until ``status`` is ``completed``
       (``text``, ``confidence``) or ``error`` (``error``). AssemblyAI limits
       this endpoint to 60 requests/minute per key+transcript, so the poll
       interval must stay >= 1s.
       https://www.assemblyai.com/docs/api-reference/transcripts/get

    Uploads must be a supported container format; headerless PCM is not in
    AssemblyAI's supported file type list, so the PCM16 16 kHz mono buffer is
    wrapped in a WAV container first.
    https://www.assemblyai.com/docs/faq/what-audio-and-video-file-types-are-supported-by-your-api

    The contract above was read from public documentation and is exercised
    only against httpx.MockTransport in tests. It has NOT been verified
    against the live AssemblyAI API.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "best",
        language: str = "en",
        timeout_seconds: float = 20.0,
        max_retries: int = 1,
        poll_interval_seconds: float = 1.0,
        transcript_deadline_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("AssemblyAI API key is required")
        self.api_key = api_key
        self.model = model
        self.language = language
        self.timeout = httpx.Timeout(timeout_seconds)
        self.max_retries = max_retries
        # AssemblyAI allows 60 polls/minute per transcript; never poll faster.
        self.poll_interval_seconds = max(1.0, float(poll_interval_seconds))
        self.transcript_deadline_seconds = float(transcript_deadline_seconds)
        self.provider_name = "assemblylabs"
        self.sessions: dict[str, dict] = {}
        # Injectable for tests so deadline handling runs without real waiting.
        self._clock = time.monotonic
        self._sleep = asyncio.sleep

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
        # Async generator so callers can `async for` over it, as the STTProvider
        # protocol requires. This batch provider emits no partial transcripts.
        if utterance_id not in self.sessions:
            raise STTProviderError("SESSION_ERROR", f"Session {utterance_id} not started")
        self.sessions[utterance_id]["chunks"].extend(audio)
        if len(self.sessions[utterance_id]["chunks"]) > 120 * 32000:
            raise STTProviderError("AUDIO_TOO_LARGE", "Utterance exceeds 120 seconds")
        return
        yield  # pragma: no cover - marks this function as an async generator

    async def end_utterance(self, utterance_id: str) -> AsyncIterator[STTEvent]:
        if utterance_id not in self.sessions:
            raise STTProviderError("SESSION_ERROR", f"Session {utterance_id} not started")
        session = self.sessions[utterance_id]
        try:
            async for event in self._transcribe(utterance_id, session):
                yield event
        finally:
            # Each utterance is transcribed once; never keep its audio around.
            self.sessions.pop(utterance_id, None)

    def _transcript_request(self, upload_url: str) -> dict:
        body: dict = {"audio_url": upload_url}
        language_code = _assemblyai_language_code(self.language)
        if language_code is None:
            body["language_detection"] = True
        else:
            body["language_code"] = language_code
        model = (self.model or "").strip()
        if model.lower() not in _ASSEMBLYAI_DEFAULT_MODEL_ALIASES:
            body["speech_models"] = [model]
        return body

    async def _transcribe(self, utterance_id: str, session: dict) -> AsyncIterator[STTEvent]:
        audio_bytes = bytes(session["chunks"])
        if not audio_bytes:
            raise STTProviderError("STT_ERROR", "No audio data")

        wav_bytes = _pcm16_mono_16k_to_wav(audio_bytes)
        deadline = self._clock() + self.transcript_deadline_seconds
        auth = {"authorization": self.api_key}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            uploaded = await self._request(
                client,
                "POST",
                "/v2/upload",
                deadline,
                headers={**auth, "content-type": "application/octet-stream"},
                content=wav_bytes,
            )
            upload_url = uploaded.get("upload_url")
            if not isinstance(upload_url, str) or not upload_url:
                raise STTProviderError("STT_ERROR", "AssemblyAI upload returned no upload_url")

            result = await self._request(
                client,
                "POST",
                "/v2/transcript",
                deadline,
                headers=auth,
                json=self._transcript_request(upload_url),
            )
            transcript_id = result.get("id")
            if not isinstance(transcript_id, str) or not _TRANSCRIPT_ID_RE.fullmatch(transcript_id):
                raise STTProviderError("STT_ERROR", "AssemblyAI returned an invalid transcript id")

            while result.get("status") not in _ASSEMBLYAI_TERMINAL_STATUSES:
                if result.get("status") not in {"queued", "processing"}:
                    raise STTProviderError("STT_ERROR", "AssemblyAI returned an unknown status")
                if self._clock() + self.poll_interval_seconds > deadline:
                    raise STTProviderError("STT_TIMEOUT", "AssemblyAI transcript deadline exceeded")
                await self._sleep(self.poll_interval_seconds)
                result = await self._request(
                    client, "GET", f"/v2/transcript/{transcript_id}", deadline, headers=auth
                )

        if result.get("status") == "error":
            # The provider's error text is not echoed; it is not needed by callers.
            raise STTProviderError("STT_ERROR", "AssemblyAI transcription failed")
        transcript = result.get("text")
        if not isinstance(transcript, str):
            # Never submit a missing transcript as the candidate's answer.
            raise STTProviderError("STT_ERROR", "AssemblyAI did not return a completed transcript")
        confidence = result.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            confidence = None

        yield STTEvent(
            utterance_id=utterance_id,
            text=transcript,
            is_final=True,
            confidence=confidence,
            timestamp=int(time.time() * 1000),
        )
        session["final_text"] = transcript

    async def _request(
        self, client: httpx.AsyncClient, method: str, path: str, deadline: float, **kwargs
    ) -> dict:
        """Send one AssemblyAI request, retrying 5xx/timeouts/network errors only."""
        attempt = 0
        while True:
            if self._clock() >= deadline:
                raise STTProviderError("STT_TIMEOUT", "AssemblyAI transcript deadline exceeded")
            try:
                response = await client.request(method, f"{ASSEMBLYAI_BASE_URL}{path}", **kwargs)
            except httpx.TimeoutException as exc:
                if attempt >= self.max_retries:
                    raise STTProviderError("STT_TIMEOUT", "AssemblyAI request timed out") from exc
                attempt += 1
                continue
            except httpx.RequestError as exc:
                if attempt >= self.max_retries:
                    raise STTProviderError(
                        "STT_ERROR", f"Network error contacting AssemblyAI ({type(exc).__name__})"
                    ) from exc
                attempt += 1
                continue

            if response.status_code >= 500:
                if attempt < self.max_retries:
                    await self._sleep(min(0.5, 0.1 * (2 ** attempt)))
                    attempt += 1
                    continue
                raise STTProviderError(
                    "STT_ERROR", f"AssemblyAI server error: {response.status_code}"
                )
            return _assemblyai_json_body(response)

    async def cancel_utterance(self, utterance_id: str) -> None:
        """Drop buffered audio for an utterance; idempotent, unknown ids ok.

        Nothing has been sent to AssemblyAI before ``end_utterance`` (this is
        the buffered pre-recorded flow), so releasing local audio is enough.
        """
        self.sessions.pop(utterance_id, None)

    async def close(self) -> None:
        self.sessions.clear()

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"


def _elevenlabs_status_error(status_code: int) -> TTSProviderError:
    """Normalize an ElevenLabs HTTP error status; the body is never echoed."""
    if status_code in {401, 403}:
        return TTSProviderError("TTS_AUTH_ERROR", "ElevenLabs rejected the API key")
    if status_code == 429:
        return TTSProviderError("TTS_RATE_LIMITED", "ElevenLabs rate limit exceeded")
    if status_code >= 500:
        return TTSProviderError("TTS_ERROR", f"ElevenLabs server error: {status_code}")
    return TTSProviderError("TTS_ERROR", f"ElevenLabs rejected the request: {status_code}")


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
        # Injectable for tests so retry backoff runs without real waiting.
        self._sleep = asyncio.sleep

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Stream synthesized audio progressively via httpx ``client.stream``.

        Retries (5xx, timeouts, network errors) happen only before the first
        chunk is yielded; once audio has been delivered, any failure raises
        ``TTSProviderError`` so callers never receive duplicate chunks.
        4xx responses are never retried. Error bodies are never echoed.
        """
        if not text or not text.strip():
            return

        url = f"{ELEVENLABS_BASE_URL}/v1/text-to-speech/{self.voice_id}/stream"
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        attempt = 0
        while True:
            yielded = False
            retry_status: int | None = None
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    async with client.stream(
                        "POST", url, headers={"xi-api-key": self.api_key}, json=payload
                    ) as response:
                        status_code = response.status_code
                        if status_code >= 500 and attempt < self.max_retries:
                            retry_status = status_code
                        elif status_code >= 400:
                            raise _elevenlabs_status_error(status_code)
                        else:
                            # No chunk_size: forward bytes as they arrive (no re-buffering).
                            async for chunk in response.aiter_bytes():
                                if chunk:
                                    yielded = True
                                    yield chunk
                            return
            except TTSProviderError:
                raise
            except httpx.TimeoutException as exc:
                if yielded or attempt >= self.max_retries:
                    raise TTSProviderError("TTS_TIMEOUT", "ElevenLabs request timed out") from exc
            except (httpx.HTTPError, httpx.StreamError) as exc:
                if yielded or attempt >= self.max_retries:
                    raise TTSProviderError(
                        "TTS_ERROR", f"Network error contacting ElevenLabs ({type(exc).__name__})"
                    ) from exc
            if retry_status is not None:
                logger.info("ElevenLabs server error %s; retrying", retry_status)
            await self._sleep(min(0.5, 0.1 * (2 ** attempt)))
            attempt += 1

    async def close(self) -> None:
        pass
