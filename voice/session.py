"""Bounded voice session orchestration over the existing interview service.

``stream_events`` is the primary contract: an async generator that yields
server events progressively (TTS audio chunks are delivered as the provider
produces them, never accumulated). ``handle`` collects the same events into a
list for callers that want a batch result.

The synchronous ``interview_service.submit_answer`` runs off the event loop on
a bounded worker pool (``AnswerRunner``) so one slow answer never blocks other
sessions on the same loop.

STT/TTS calls are metered through ``utils.metrics`` (provider name, operation,
outcome and normalized voice error code only; never transcripts, audio or ids).
Recording a metric never changes the events a client receives.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import contextlib
import inspect
import logging
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

from utils import metrics
from voice.contracts import AudioFormat, ClientVoiceEvent, VoiceErrorCode, VoiceServerEvent
from voice.providers import (
    FakeSTTProvider,
    FakeTTSProvider,
    FakeVADProvider,
    STTProvider,
    TTSProvider,
    VADProvider,
)

logger = logging.getLogger(__name__)

_STT_CODE_MAP = {
    "STT_AUTH_ERROR": VoiceErrorCode.STT_AUTH_ERROR,
    "STT_TIMEOUT": VoiceErrorCode.STT_TIMEOUT,
    "STT_RATE_LIMITED": VoiceErrorCode.STT_RATE_LIMITED,
}
_TTS_CODE_MAP = {
    "TTS_TIMEOUT": VoiceErrorCode.TTS_TIMEOUT,
    "TTS_RATE_LIMITED": VoiceErrorCode.TTS_RATE_LIMITED,
}
_MAX_REMEMBERED_CANCELLED = 256


def _provider_label(provider: Any) -> str:
    name = getattr(provider, "provider_name", None)
    if name:
        return str(name)
    return "fake" if type(provider).__name__.startswith("Fake") else "other"


def _record_voice(kind: str, provider: Any, operation: str, error: VoiceServerEvent | None = None) -> None:
    """Count one STT/TTS operation; ``error`` is the error event sent to the client."""
    try:
        label = _provider_label(provider)
        outcome = "success" if error is None else "error"
        metrics.inc(
            "voice_provider_requests_total",
            {"kind": kind, "provider": label, "operation": operation, "outcome": outcome},
        )
        if error is not None:
            code = getattr(error.code, "value", error.code)
            metrics.inc("voice_provider_errors_total", {"kind": kind, "provider": label, "error_code": code})
    except Exception:  # pragma: no cover - metrics must never affect the voice flow
        logger.debug("voice metric update failed")


def _observe_voice(kind: str, provider: Any, operation: str, seconds: float) -> None:
    try:
        metrics.observe(
            "voice_provider_duration_seconds",
            seconds,
            {"kind": kind, "provider": _provider_label(provider), "operation": operation},
        )
    except Exception:  # pragma: no cover - metrics must never affect the voice flow
        logger.debug("voice metric update failed")


class AnswerCapacityError(Exception):
    """Raised when no answer-processing slot frees up before the timeout."""


class AnswerRunner:
    """Runs blocking answer submission on a bounded thread pool.

    At most ``max_workers`` submissions are outstanding at once (a
    ``threading.BoundedSemaphore`` guards submission, so the executor's
    internal queue never grows). A caller that cannot get a slot within
    ``acquire_timeout`` seconds gets ``AnswerCapacityError`` (retryable).

    The semaphore is thread-based (not bound to an event loop) and each slot
    is released by the concurrent future's done callback, i.e. only when the
    worker actually finished (or the work item was cancelled before starting).
    """

    def __init__(self, max_workers: int = 4, acquire_timeout: float = 5.0, poll_interval: float = 0.02) -> None:
        self.max_workers = max(1, int(max_workers))
        self.acquire_timeout = max(0.0, float(acquire_timeout))
        self.poll_interval = poll_interval
        self._slots = threading.BoundedSemaphore(self.max_workers)
        self._lock = threading.Lock()
        self._in_use = 0
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None

    @property
    def in_use(self) -> int:
        return self._in_use

    def _get_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.max_workers, thread_name_prefix="voice-answer"
                )
            return self._executor

    async def _acquire(self) -> None:
        deadline = time.monotonic() + self.acquire_timeout
        while not self._slots.acquire(blocking=False):
            if time.monotonic() >= deadline:
                raise AnswerCapacityError("Answer processing capacity exhausted")
            await asyncio.sleep(self.poll_interval)
        with self._lock:
            self._in_use += 1

    def _release(self, _future: concurrent.futures.Future | None = None) -> None:
        with self._lock:
            self._in_use -= 1
        self._slots.release()

    def submit(self, fn: Callable[..., Any], *args: Any) -> concurrent.futures.Future:
        """Submit after a slot was acquired; the slot is freed when the work ends."""
        try:
            future = self._get_executor().submit(fn, *args)
        except BaseException:
            self._release()
            raise
        future.add_done_callback(self._release)
        return future

    async def run(self, fn: Callable[..., Any], *args: Any, on_submit: Callable[[concurrent.futures.Future], None] | None = None) -> Any:
        """Run ``fn(*args)`` in a worker thread and await its result.

        Cancellation of the awaiting coroutine propagates (CancelledError), but
        the worker is shielded: it runs to completion and its result is
        discarded safely (no "exception was never retrieved" noise, nothing is
        delivered to a closed loop).
        """
        await self._acquire()
        future = self.submit(fn, *args)
        if on_submit is not None:
            on_submit(future)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(_consume_future_exception)
        return await asyncio.shield(wrapped)


def _consume_future_exception(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


_default_runner: AnswerRunner | None = None
_default_runner_lock = threading.Lock()


def get_default_answer_runner() -> AnswerRunner:
    global _default_runner
    with _default_runner_lock:
        if _default_runner is None:
            try:
                from config import settings

                workers = getattr(settings, "VOICE_ANSWER_MAX_CONCURRENCY", 4)
                timeout = getattr(settings, "VOICE_ANSWER_QUEUE_TIMEOUT_SECONDS", 5.0)
            except Exception:  # pragma: no cover - settings import failure
                workers, timeout = 4, 5.0
            _default_runner = AnswerRunner(max_workers=workers, acquire_timeout=timeout)
        return _default_runner


@contextlib.asynccontextmanager
async def _aclosing(iterator: Any) -> AsyncIterator[Any]:
    """Like contextlib.aclosing, tolerant of iterators without ``aclose``."""
    try:
        yield iterator
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


class VoiceSessionService:
    def __init__(self, interview_service, user_id: str, role: str, organization_id: str | None = None, *, vad: VADProvider | None = None, stt: STTProvider | None = None, tts: TTSProvider | None = None, max_chunk_bytes: int = 64 * 1024, max_buffer_bytes: int = 2 * 1024 * 1024, max_utterance_seconds: int = 120, answer_runner: AnswerRunner | None = None) -> None:
        self.interview_service = interview_service
        self.user_id = user_id
        self.role = role
        self.organization_id = organization_id
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
        self.answer_runner = answer_runner or get_default_answer_runner()
        self.cancelled_utterances: OrderedDict[str, None] = OrderedDict()
        # Per-session turn ordering: one answer in flight at a time.
        self.answer_in_flight: str | None = None
        self.submit_future: concurrent.futures.Future | None = None
        self.closed = False

    # ------------------------------------------------------------------ API

    async def handle(self, interview_id: str, event: ClientVoiceEvent) -> list[VoiceServerEvent]:
        """Batch form of ``stream_events`` (collects every event)."""
        events: list[VoiceServerEvent] = []
        async with _aclosing(self.stream_events(interview_id, event)) as stream:
            async for item in stream:
                events.append(item)
        return events

    async def stream_events(self, interview_id: str, event: ClientVoiceEvent) -> AsyncIterator[VoiceServerEvent]:
        """Yield server events for one client event as soon as each is ready."""
        handler = {
            "session.start": self._session_start_events,
            "audio.start": self._audio_start_events,
            "audio.chunk": self._audio_chunk_events,
            "audio.end": self._audio_end_dispatch,
            "utterance.cancel": self._cancel_events,
            "session.stop": self._session_stop_events,
        }.get(event.type.value)
        if handler is None:
            yield self.error(VoiceErrorCode.INVALID_EVENT, "Unsupported voice event.")
            return
        async with _aclosing(handler(interview_id, event)) as stream:
            async for item in stream:
                yield item

    async def _session_start_events(self, interview_id: str, event: ClientVoiceEvent) -> AsyncIterator[VoiceServerEvent]:
        state = self.interview_service.get_interview(
            interview_id,
            self.user_id,
            self.role,
            self.organization_id,
        )
        if state is None:
            yield self.error(VoiceErrorCode.INTERVIEW_FORBIDDEN, "Interview is not available.")
            return
        yield VoiceServerEvent(type="session.ready", session_id=self.session_id)

    async def _audio_start_events(self, interview_id: str, event: ClientVoiceEvent) -> AsyncIterator[VoiceServerEvent]:
        async with _aclosing(self._audio_start(event)) as stream:
            async for item in stream:
                yield item

    async def _audio_chunk_events(self, interview_id: str, event: ClientVoiceEvent) -> AsyncIterator[VoiceServerEvent]:
        for item in await self._audio_chunk(event):
            yield item

    async def _audio_end_dispatch(self, interview_id: str, event: ClientVoiceEvent) -> AsyncIterator[VoiceServerEvent]:
        if not event.utterance_id:
            yield self.error(VoiceErrorCode.INVALID_EVENT, "utterance_id is required.")
            return
        async with _aclosing(self._audio_end_events(interview_id, event.utterance_id)) as stream:
            async for item in stream:
                yield item

    async def _cancel_events(self, interview_id: str, event: ClientVoiceEvent) -> AsyncIterator[VoiceServerEvent]:
        if event.utterance_id:
            await self.cancel_utterance(event.utterance_id)
        return
        yield  # pragma: no cover - marks this function as an async generator

    async def _session_stop_events(self, interview_id: str, event: ClientVoiceEvent) -> AsyncIterator[VoiceServerEvent]:
        await self.close()
        yield VoiceServerEvent(type="session.closed", session_id=self.session_id)

    async def cancel_utterance(self, utterance_id: str) -> None:
        """Release session buffers and provider-side audio for an utterance.

        Safe to call repeatedly and for unknown ids. Later chunks for the same
        id are rejected until a new ``audio.start`` reuses it.
        """
        self.buffers.pop(utterance_id, None)
        self.cancelled_utterances[utterance_id] = None
        self.cancelled_utterances.move_to_end(utterance_id)
        while len(self.cancelled_utterances) > _MAX_REMEMBERED_CANCELLED:
            self.cancelled_utterances.popitem(last=False)
        await self._release_stt(utterance_id)

    # ------------------------------------------------------------ handlers

    async def _audio_start(self, event: ClientVoiceEvent) -> AsyncIterator[VoiceServerEvent]:
        if not event.utterance_id:
            yield self.error(VoiceErrorCode.INVALID_EVENT, "utterance_id is required.")
            return
        utterance_id = event.utterance_id
        # A (re)started utterance never inherits stale audio.
        self.cancelled_utterances.pop(utterance_id, None)
        self.buffers.pop(utterance_id, None)
        await self._release_stt(utterance_id)
        try:
            await self.stt.start_session(utterance_id, self.audio_format)
        except Exception as exc:
            logger.warning("STT session start failed (%s)", type(exc).__name__)
            error = self._stt_error(exc)
            _record_voice("stt", self.stt, "start", error)
            yield error
            return
        _record_voice("stt", self.stt, "start")

    def _validate_and_buffer(self, utterance_id: str, audio: bytes) -> VoiceServerEvent | None:
        if utterance_id in self.cancelled_utterances:
            return self.error(VoiceErrorCode.PROTOCOL_ERROR, "Utterance was cancelled.")
        if len(audio) > self.max_chunk_bytes:
            return self.error(VoiceErrorCode.AUDIO_TOO_LARGE, "Audio chunk exceeds the size limit.")
        if sum(len(item) for item in self.buffers.values()) + len(audio) > self.max_buffer_bytes:
            return self.error(VoiceErrorCode.BUFFER_OVERFLOW, "Audio buffer is full.")
        if len(self.buffers.get(utterance_id, b"")) + len(audio) > self.max_utterance_bytes:
            return self.error(VoiceErrorCode.AUDIO_TOO_LARGE, "Utterance exceeds the duration limit.")
        self.buffers[utterance_id].extend(audio)
        return None

    async def _partials(self, utterance_id: str, audio: bytes) -> list[VoiceServerEvent]:
        if not await self.vad.detect(audio, self.audio_format):
            return []
        events = []
        async with _aclosing(self.stt.send_audio(utterance_id, audio)) as stream:
            async for transcript in stream:
                if not transcript.is_final:
                    events.append(VoiceServerEvent(
                        type="transcript.partial",
                        session_id=self.session_id,
                        utterance_id=transcript.utterance_id,
                        text=transcript.text,
                        confidence=transcript.confidence,
                    ))
        return events

    async def _audio_chunk(self, event: ClientVoiceEvent) -> list[VoiceServerEvent]:
        if not event.utterance_id or not event.audio_base64:
            return [self.error(VoiceErrorCode.INVALID_EVENT, "utterance_id and audio_base64 are required.")]
        if event.audio_format and event.audio_format != "pcm_s16le/16000/1":
            return [self.error(VoiceErrorCode.INVALID_AUDIO, "Unsupported audio format.")]
        try:
            audio = base64.b64decode(event.audio_base64, validate=True)
        except (ValueError, binascii.Error):
            return [self.error(VoiceErrorCode.INVALID_AUDIO, "Audio must be valid base64 PCM16.")]
        return await self._audio_chunk_binary(event.utterance_id, audio)

    async def _audio_chunk_binary(self, utterance_id: str, audio: bytes) -> list[VoiceServerEvent]:
        """Handle a raw PCM16 chunk (binary frame or decoded JSON chunk)."""
        rejected = self._validate_and_buffer(utterance_id, audio)
        if rejected is not None:
            return [rejected]
        try:
            events = await self._partials(utterance_id, audio)
        except Exception as exc:
            logger.warning("STT streaming failed (%s)", type(exc).__name__)
            error = self._stt_error(exc)
            _record_voice("stt", self.stt, "stream", error)
            return [error]
        return events

    async def _audio_end(self, interview_id: str, event: ClientVoiceEvent) -> list[VoiceServerEvent]:
        """Batch form of the audio.end flow (kept for direct callers/tests)."""
        return await self.handle(interview_id, event)

    async def _final_transcript(self, utterance_id: str):
        async with _aclosing(self.stt.end_utterance(utterance_id)) as stream:
            async for transcript in stream:
                if transcript.is_final:
                    return transcript if transcript.text.strip() else None
        return None

    async def _audio_end_events(self, interview_id: str, utterance_id: str) -> AsyncIterator[VoiceServerEvent]:
        if self.answer_in_flight is not None or (self.submit_future is not None and not self.submit_future.done()):
            yield self.error(VoiceErrorCode.TURN_IN_PROGRESS, "An answer is already being processed; retry after it completes.", retryable=True)
            return
        self.answer_in_flight = utterance_id
        try:
            if utterance_id in self.committed:
                # Idempotent replay: never re-transcribe or re-submit.
                result = self.committed[utterance_id]
            else:
                stt_started = time.perf_counter()
                try:
                    final = await self._final_transcript(utterance_id)
                except Exception as exc:
                    logger.warning("STT finalization failed (%s)", type(exc).__name__)
                    error = self._stt_error(exc)
                    _observe_voice("stt", self.stt, "finalize", time.perf_counter() - stt_started)
                    _record_voice("stt", self.stt, "finalize", error)
                    yield error
                    return
                _observe_voice("stt", self.stt, "finalize", time.perf_counter() - stt_started)
                if final is None:
                    error = self.error(VoiceErrorCode.STT_ERROR, "STT did not produce a final transcript.")
                    _record_voice("stt", self.stt, "finalize", error)
                    yield error
                    return
                _record_voice("stt", self.stt, "finalize")
                yield VoiceServerEvent(
                    type="transcript.final",
                    session_id=self.session_id,
                    utterance_id=utterance_id,
                    text=final.text,
                    confidence=final.confidence,
                )
                try:
                    result = await self._submit_answer(interview_id, final.text, utterance_id)
                except AnswerCapacityError:
                    yield self.error(VoiceErrorCode.BUSY, "The server is busy; please retry this answer.", retryable=True)
                    return
                except Exception:
                    logger.exception("Interview processing failed")
                    yield self.error(VoiceErrorCode.TURN_COMMIT_FAILED, "Interview processing failed; please retry.", retryable=True)
                    return
                self.committed[utterance_id] = result
            question = result.get("current_question")
            yield VoiceServerEvent(type="interview.question", session_id=self.session_id, utterance_id=utterance_id, question=question)
            yield VoiceServerEvent(type="tts.started", session_id=self.session_id, utterance_id=utterance_id, question=question)
            async with _aclosing(self._tts_events(utterance_id, question or "")) as stream:
                async for item in stream:
                    yield item
        finally:
            self.answer_in_flight = None
            self.buffers.pop(utterance_id, None)
            # Provider audio for this utterance is never reused after audio.end.
            await self._release_stt(utterance_id)

    async def _submit_answer(self, interview_id: str, text: str, utterance_id: str) -> dict:
        def remember(future: concurrent.futures.Future) -> None:
            self.submit_future = future

            def store(done: concurrent.futures.Future) -> None:
                # Runs in the worker thread. If the awaiting coroutine was
                # cancelled (disconnect/cancel), keep the durable result so a
                # retry of this utterance in the same session is idempotent.
                if self.closed or done.cancelled() or done.exception() is not None:
                    return
                self.committed.setdefault(utterance_id, done.result())

            future.add_done_callback(store)

        return await self.answer_runner.run(
            self.interview_service.submit_answer,
            interview_id,
            text,
            utterance_id,
            self.user_id,
            self.role,
            self.organization_id,
            on_submit=remember,
        )

    async def _tts_events(self, utterance_id: str, text: str) -> AsyncIterator[VoiceServerEvent]:
        """Stream TTS audio chunk by chunk; a provider error ends the stream
        with one error event after any chunks already delivered."""
        tts_started = time.perf_counter()
        try:
            async with _aclosing(self.tts.stream(text)) as stream:
                async for audio in stream:
                    if not audio:
                        continue
                    yield VoiceServerEvent(
                        type="tts.audio",
                        session_id=self.session_id,
                        utterance_id=utterance_id,
                        audio_base64=base64.b64encode(audio).decode("ascii"),
                    )
        except Exception as exc:
            from voice.real_providers import TTSProviderError

            logger.warning("TTS failed (%s)", type(exc).__name__)
            code = VoiceErrorCode.TTS_ERROR
            if isinstance(exc, TTSProviderError):
                code = _TTS_CODE_MAP.get(exc.code, VoiceErrorCode.TTS_ERROR)
            error = self.error(code, "Audio response is unavailable; use the displayed question.")
            error.utterance_id = utterance_id
            _observe_voice("tts", self.tts, "synthesize", time.perf_counter() - tts_started)
            _record_voice("tts", self.tts, "synthesize", error)
            yield error
            return
        _observe_voice("tts", self.tts, "synthesize", time.perf_counter() - tts_started)
        _record_voice("tts", self.tts, "synthesize")
        yield VoiceServerEvent(type="tts.completed", session_id=self.session_id, utterance_id=utterance_id)

    # ------------------------------------------------------------- helpers

    async def _release_stt(self, utterance_id: str) -> None:
        cancel = getattr(self.stt, "cancel_utterance", None)
        if cancel is None:
            return
        try:
            result = cancel(utterance_id)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("STT cancel failed (%s)", type(exc).__name__)

    def _stt_error(self, exc: Exception) -> VoiceServerEvent:
        from voice.real_providers import STTProviderError

        if isinstance(exc, STTProviderError):
            return self.error(_STT_CODE_MAP.get(exc.code, VoiceErrorCode.STT_ERROR), exc.message)
        return self.error(VoiceErrorCode.STT_ERROR, "STT processing failed")

    @staticmethod
    def error(code: VoiceErrorCode, message: str, *, retryable: bool | None = None) -> VoiceServerEvent:
        return VoiceServerEvent(type="error", code=code, message=message, retryable=retryable)

    async def close(self) -> None:
        """Release all buffers and provider resources. Idempotent."""
        if self.closed:
            return
        self.closed = True
        self.buffers.clear()
        self.cancelled_utterances.clear()
        for provider in (self.stt, self.tts):
            try:
                await provider.close()
            except Exception as exc:
                logger.warning("Voice provider close failed (%s)", type(exc).__name__)
