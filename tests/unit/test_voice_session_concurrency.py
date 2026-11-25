"""Offline tests: non-blocking answers, progressive TTS, cancellation/cleanup.

No live providers: fakes and httpx.MockTransport only.
"""
from __future__ import annotations

import asyncio
import base64
import threading
import time

import httpx
import pytest

from voice.contracts import AudioFormat, ClientVoiceEvent, STTEvent, VoiceErrorCode, VoiceEventType
from voice.providers import FakeSTTProvider, FakeTTSProvider
from voice.real_providers import (
    AssemblyAISTTProvider,
    ElevenLabsTTSProvider,
    STTProviderError,
    TTSProviderError,
)
from voice.session import AnswerRunner, VoiceSessionService

SAFETY_SECONDS = 10.0
SENTINEL_TTS_KEY = "sentinel-tts-key-7d1e"


def ev(kind: VoiceEventType, utterance_id: str | None = None, audio: bytes | None = None) -> ClientVoiceEvent:
    return ClientVoiceEvent(
        type=kind,
        utterance_id=utterance_id,
        audio_base64=base64.b64encode(audio).decode("ascii") if audio is not None else None,
    )


class FakeInterviewService:
    def __init__(self, question: str = "How did you measure the result?") -> None:
        self.question = question
        self.calls: list[tuple] = []
        self._lock = threading.Lock()

    def get_interview(self, interview_id, user_id, role, organization_id=None):
        return {"interview_id": interview_id}

    def submit_answer(self, interview_id, answer, turn_id, user_id, role, organization_id=None):
        with self._lock:
            self.calls.append((interview_id, answer, turn_id))
        return {"current_question": f"{self.question} [{turn_id}]"}


class BlockingInterviewService(FakeInterviewService):
    """submit_answer blocks on a threading.Event (auto-released for safety)."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self._safety = threading.Timer(SAFETY_SECONDS, self.release.set)
        self._safety.daemon = True
        self._safety.start()

    def submit_answer(self, *args, **kwargs):
        self.entered.set()
        try:
            self.release.wait()
            return super().submit_answer(*args, **kwargs)
        finally:
            self.finished.set()


async def wait_for_thread_event(event: threading.Event, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not event.is_set():
        assert time.monotonic() < deadline, "timed out waiting for worker thread"
        await asyncio.sleep(0.01)


async def start_utterance(session: VoiceSessionService, utterance_id: str, audio: bytes = b"pcm-audio") -> None:
    await session.handle("interview-1", ev(VoiceEventType.AUDIO_START, utterance_id))
    await session.handle("interview-1", ev(VoiceEventType.AUDIO_CHUNK, utterance_id, audio))


def other_tasks() -> list[asyncio.Task]:
    current = asyncio.current_task()
    return [task for task in asyncio.all_tasks() if task is not current and not task.done()]


# --------------------------------------------------------------------------
# 1. Non-blocking answer processing
# --------------------------------------------------------------------------


def test_second_session_stays_responsive_while_first_submit_blocks():
    async def scenario():
        blocking = BlockingInterviewService()
        runner = AnswerRunner(max_workers=2, acquire_timeout=1.0)
        first = VoiceSessionService(blocking, "user-1", "candidate", answer_runner=runner)
        second = VoiceSessionService(FakeInterviewService(), "user-2", "candidate", answer_runner=runner)
        await start_utterance(first, "utt-a")
        first_task = asyncio.create_task(first.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-a")))
        await wait_for_thread_event(blocking.entered)

        started = time.monotonic()
        ready = await asyncio.wait_for(second.handle("interview-2", ev(VoiceEventType.SESSION_START)), timeout=2.0)
        await start_utterance(second, "utt-b")
        second_answer = await asyncio.wait_for(second.handle("interview-2", ev(VoiceEventType.AUDIO_END, "utt-b")), timeout=2.0)
        elapsed = time.monotonic() - started

        assert ready[0].type == "session.ready"
        assert [e.type for e in second_answer][:3] == ["transcript.final", "interview.question", "tts.started"]
        assert second_answer[-1].type == "tts.completed"
        assert elapsed < 2.0
        assert not blocking.release.is_set(), "first submit must still be blocked"
        assert not first_task.done()

        blocking.release.set()
        first_events = await asyncio.wait_for(first_task, timeout=5.0)
        assert first_events[-1].type == "tts.completed"
        assert [c[2] for c in blocking.calls] == ["utt-a"]
        await first.close()
        await second.close()
        assert runner.in_use == 0

    asyncio.run(scenario())


def test_second_audio_end_in_same_session_is_rejected_while_in_flight():
    async def scenario():
        blocking = BlockingInterviewService()
        session = VoiceSessionService(blocking, "user-1", "candidate", answer_runner=AnswerRunner(2, 1.0))
        await start_utterance(session, "utt-1")
        first = asyncio.create_task(session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1")))
        await wait_for_thread_event(blocking.entered)

        for utterance_id in ("utt-1", "utt-2"):
            rejected = await session.handle("interview-1", ev(VoiceEventType.AUDIO_END, utterance_id))
            assert [e.code for e in rejected] == [VoiceErrorCode.TURN_IN_PROGRESS]
            assert rejected[0].retryable is True

        blocking.release.set()
        events = await first
        assert events[-1].type == "tts.completed"
        assert len(blocking.calls) == 1

        replay = await session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1"))
        assert replay[0].type == "interview.question"
        assert len(blocking.calls) == 1
        await session.close()

    asyncio.run(scenario())


def test_exhausted_answer_capacity_returns_retryable_busy_error():
    async def scenario():
        blocking = BlockingInterviewService()
        runner = AnswerRunner(max_workers=1, acquire_timeout=0.05)
        first = VoiceSessionService(blocking, "user-1", "candidate", answer_runner=runner)
        other_service = FakeInterviewService()
        second = VoiceSessionService(other_service, "user-2", "candidate", answer_runner=runner)
        await start_utterance(first, "utt-a")
        first_task = asyncio.create_task(first.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-a")))
        await wait_for_thread_event(blocking.entered)

        await start_utterance(second, "utt-b")
        busy = await second.handle("interview-2", ev(VoiceEventType.AUDIO_END, "utt-b"))
        assert busy[-1].code == VoiceErrorCode.BUSY
        assert busy[-1].retryable is True
        assert other_service.calls == []
        assert runner.in_use == 1

        blocking.release.set()
        await first_task
        await first.close()
        await second.close()
        assert runner.in_use == 0

    asyncio.run(scenario())


def test_cancel_during_submit_discards_result_safely_and_retry_is_idempotent():
    async def scenario():
        blocking = BlockingInterviewService()
        runner = AnswerRunner(max_workers=1, acquire_timeout=1.0)
        session = VoiceSessionService(blocking, "user-1", "candidate", answer_runner=runner)
        await start_utterance(session, "utt-1")
        task = asyncio.create_task(session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1")))
        await wait_for_thread_event(blocking.entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.answer_in_flight is None
        assert session.buffers == {}

        # While the worker is still running, ordering is preserved.
        rejected = await session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-2"))
        assert rejected[0].code == VoiceErrorCode.TURN_IN_PROGRESS

        blocking.release.set()
        await wait_for_thread_event(blocking.finished)
        await asyncio.wrap_future(session.submit_future)
        assert runner.in_use == 0
        # The durable result was kept; a retry does not resubmit.
        retry = await session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1"))
        assert retry[0].type == "interview.question"
        assert len(blocking.calls) == 1
        await session.close()
        assert other_tasks() == []

    asyncio.run(scenario())


def test_concurrent_sessions_each_commit_their_own_turn():
    async def scenario():
        service = FakeInterviewService()
        runner = AnswerRunner(max_workers=3, acquire_timeout=2.0)
        sessions = [VoiceSessionService(service, f"user-{i}", "candidate", answer_runner=runner) for i in range(6)]
        for index, session in enumerate(sessions):
            await start_utterance(session, f"utt-{index}")
        results = await asyncio.gather(*(
            session.handle("interview-1", ev(VoiceEventType.AUDIO_END, f"utt-{index}"))
            for index, session in enumerate(sessions)
        ))
        for index, events in enumerate(results):
            question = next(e for e in events if e.type == "interview.question")
            assert question.question.endswith(f"[utt-{index}]")
            assert events[-1].type == "tts.completed"
        assert sorted(c[2] for c in service.calls) == sorted(f"utt-{i}" for i in range(6))
        for session in sessions:
            await session.close()
        assert runner.in_use == 0

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# 2. Progressive TTS streaming
# --------------------------------------------------------------------------


class GatedTTS:
    """Yields one chunk, then waits for a gate before the rest."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.gate = asyncio.Event()
        self.finished = False
        self.finalized = False
        self.fail_with = fail_with

    async def stream(self, text: str):
        try:
            yield b"chunk-1"
            await self.gate.wait()
            yield b"chunk-2"
            if self.fail_with is not None:
                raise self.fail_with
            yield b"chunk-3"
            self.finished = True
        finally:
            self.finalized = True

    async def close(self) -> None:
        return None


def test_tts_chunks_are_delivered_before_provider_finishes():
    async def scenario():
        tts = GatedTTS()
        session = VoiceSessionService(FakeInterviewService(), "user-1", "candidate", tts=tts, answer_runner=AnswerRunner(1, 1.0))
        await start_utterance(session, "utt-1")
        received = []
        stream = session.stream_events("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1"))
        async for event in stream:
            received.append(event)
            if event.type == "tts.audio":
                break
        # First audio chunk observed while the provider is still mid-stream.
        assert not tts.finished
        assert [e.type for e in received] == ["transcript.final", "interview.question", "tts.started", "tts.audio"]
        tts.gate.set()
        async for event in stream:
            received.append(event)
        types = [e.type for e in received]
        assert types == ["transcript.final", "interview.question", "tts.started", "tts.audio", "tts.audio", "tts.audio", "tts.completed"]
        audio = [base64.b64decode(e.audio_base64) for e in received if e.type == "tts.audio"]
        assert audio == [b"chunk-1", b"chunk-2", b"chunk-3"]
        await session.close()

    asyncio.run(scenario())


def test_tts_error_after_chunks_yields_single_error_without_duplicates():
    async def scenario():
        tts = GatedTTS(fail_with=TTSProviderError("TTS_RATE_LIMITED", "limit"))
        tts.gate.set()
        session = VoiceSessionService(FakeInterviewService(), "user-1", "candidate", tts=tts, answer_runner=AnswerRunner(1, 1.0))
        await start_utterance(session, "utt-1")
        events = await session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1"))
        types = [e.type for e in events]
        assert types == ["transcript.final", "interview.question", "tts.started", "tts.audio", "tts.audio", "error"]
        assert events[-1].code == VoiceErrorCode.TTS_RATE_LIMITED
        assert events[-1].utterance_id == "utt-1"
        assert "tts.completed" not in types
        await session.close()

    asyncio.run(scenario())


def test_cancellation_during_tts_streaming_stops_provider():
    async def scenario():
        tts = GatedTTS()
        session = VoiceSessionService(FakeInterviewService(), "user-1", "candidate", tts=tts, answer_runner=AnswerRunner(1, 1.0))
        await start_utterance(session, "utt-1")
        first_chunk = asyncio.Event()
        received = []

        async def consume():
            async for event in session.stream_events("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1")):
                received.append(event.type)
                if event.type == "tts.audio":
                    first_chunk.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(first_chunk.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tts.finalized and not tts.finished
        assert received.count("tts.audio") == 1
        assert "tts.completed" not in received
        assert session.answer_in_flight is None
        await session.close()
        assert other_tasks() == []

    asyncio.run(scenario())


def test_consumer_closing_stream_early_finalizes_provider_generators():
    async def scenario():
        tts = GatedTTS()
        session = VoiceSessionService(FakeInterviewService(), "user-1", "candidate", tts=tts, answer_runner=AnswerRunner(1, 1.0))
        await start_utterance(session, "utt-1")
        stream = session.stream_events("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1"))
        async for event in stream:
            if event.type == "tts.audio":
                break
        await stream.aclose()
        assert tts.finalized and not tts.finished
        assert session.answer_in_flight is None
        await session.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# 3. Cancellation / cleanup / provider failures
# --------------------------------------------------------------------------


class RecordingSTT(FakeSTTProvider):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []
        self.close_calls = 0

    async def cancel_utterance(self, utterance_id: str) -> None:
        self.cancelled.append(utterance_id)
        await super().cancel_utterance(utterance_id)

    async def close(self) -> None:
        self.close_calls += 1
        await super().close()


class HangingSTT(RecordingSTT):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.finalized = False

    async def end_utterance(self, utterance_id: str):
        try:
            self.entered.set()
            await asyncio.Event().wait()
            yield STTEvent(utterance_id=utterance_id, text="never", is_final=True)
        finally:
            self.finalized = True


def test_cancellation_during_stt_releases_everything():
    async def scenario():
        stt = HangingSTT()
        service = FakeInterviewService()
        session = VoiceSessionService(service, "user-1", "candidate", stt=stt, answer_runner=AnswerRunner(1, 1.0))
        await start_utterance(session, "utt-1")
        task = asyncio.create_task(session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1")))
        await asyncio.wait_for(stt.entered.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stt.finalized
        assert session.buffers == {}
        assert stt.audio == {}
        assert "utt-1" in stt.cancelled
        assert session.answer_in_flight is None
        assert service.calls == []
        await session.close()
        assert other_tasks() == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (STTProviderError("STT_TIMEOUT", "AssemblyAI transcript deadline exceeded"), VoiceErrorCode.STT_TIMEOUT),
        (STTProviderError("STT_RATE_LIMITED", "AssemblyAI rate limit exceeded"), VoiceErrorCode.STT_RATE_LIMITED),
        (RuntimeError("internal detail sentinel-xyz"), VoiceErrorCode.STT_ERROR),
    ],
)
def test_stt_provider_timeout_and_exception_become_structured_errors(error, code):
    class FailingSTT(RecordingSTT):
        async def end_utterance(self, utterance_id):
            raise error
            yield  # pragma: no cover

    async def scenario():
        stt = FailingSTT()
        service = FakeInterviewService()
        session = VoiceSessionService(service, "user-1", "candidate", stt=stt, answer_runner=AnswerRunner(1, 1.0))
        await start_utterance(session, "utt-1")
        events = await session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1"))
        assert [e.code for e in events] == [code]
        assert "sentinel-xyz" not in (events[0].message or "")
        assert service.calls == []
        assert session.buffers == {}
        assert stt.audio == {}
        await session.close()

    asyncio.run(scenario())


def test_tts_provider_timeout_maps_to_tts_timeout():
    class TimeoutTTS:
        async def stream(self, text):
            raise TTSProviderError("TTS_TIMEOUT", "ElevenLabs request timed out")
            yield b""  # pragma: no cover

        async def close(self):
            return None

    async def scenario():
        session = VoiceSessionService(FakeInterviewService(), "user-1", "candidate", tts=TimeoutTTS(), answer_runner=AnswerRunner(1, 1.0))
        await start_utterance(session, "utt-1")
        events = await session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1"))
        assert events[1].type == "interview.question"
        assert events[-1].code == VoiceErrorCode.TTS_TIMEOUT
        await session.close()

    asyncio.run(scenario())


def test_repeated_utterance_lifecycle_never_reuses_stale_audio():
    async def scenario():
        stt = RecordingSTT()
        service = FakeInterviewService()
        session = VoiceSessionService(service, "user-1", "candidate", stt=stt, answer_runner=AnswerRunner(1, 1.0))
        await start_utterance(session, "utt-1", b"stale-audio")
        assert bytes(stt.audio["utt-1"]) == b"stale-audio"

        await session.handle("interview-1", ev(VoiceEventType.UTTERANCE_CANCEL, "utt-1"))
        assert "utt-1" not in session.buffers
        assert "utt-1" not in stt.audio
        late = await session.handle("interview-1", ev(VoiceEventType.AUDIO_CHUNK, "utt-1", b"late"))
        assert [e.code for e in late] == [VoiceErrorCode.PROTOCOL_ERROR]
        assert "utt-1" not in session.buffers and "utt-1" not in stt.audio

        # Cancel is idempotent and tolerates unknown ids.
        await session.handle("interview-1", ev(VoiceEventType.UTTERANCE_CANCEL, "utt-1"))
        await session.handle("interview-1", ev(VoiceEventType.UTTERANCE_CANCEL, "unknown"))

        await start_utterance(session, "utt-1", b"fresh")
        assert bytes(session.buffers["utt-1"]) == b"fresh"
        assert bytes(stt.audio["utt-1"]) == b"fresh"
        events = await session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-1"))
        assert events[-1].type == "tts.completed"
        assert len(service.calls) == 1
        assert session.buffers == {} and stt.audio == {}

        # A different utterance afterwards starts clean too.
        await start_utterance(session, "utt-2", b"next")
        await session.handle("interview-1", ev(VoiceEventType.AUDIO_END, "utt-2"))
        assert [c[2] for c in service.calls] == ["utt-1", "utt-2"]
        await session.close()

    asyncio.run(scenario())


def test_close_is_idempotent_and_releases_all_resources():
    async def scenario():
        stt = RecordingSTT()
        runner = AnswerRunner(1, 1.0)
        session = VoiceSessionService(FakeInterviewService(), "user-1", "candidate", stt=stt, answer_runner=runner)
        await start_utterance(session, "utt-1")
        await start_utterance(session, "utt-2")
        await session.handle("interview-1", ev(VoiceEventType.UTTERANCE_CANCEL, "utt-2"))
        closed = await session.handle("interview-1", ev(VoiceEventType.SESSION_STOP))
        assert closed[0].type == "session.closed"
        await session.close()
        await session.close()
        assert stt.close_calls == 1
        assert session.buffers == {}
        assert stt.audio == {}
        assert session.cancelled_utterances == {}
        assert runner.in_use == 0
        assert other_tasks() == []

    asyncio.run(scenario())


def test_assemblyai_cancel_utterance_releases_provider_audio_offline():
    async def scenario():
        provider = AssemblyAISTTProvider(api_key="test-key")
        await provider.start_session("utt-1", AudioFormat())
        async for _ in provider.send_audio("utt-1", b"\x00\x01" * 100):
            pass
        assert "utt-1" in provider.sessions
        await provider.cancel_utterance("utt-1")
        await provider.cancel_utterance("utt-1")
        await provider.cancel_utterance("never-started")
        assert provider.sessions == {}

        session = VoiceSessionService(FakeInterviewService(), "user-1", "candidate", stt=provider, answer_runner=AnswerRunner(1, 1.0))
        await start_utterance(session, "utt-2")
        assert "utt-2" in provider.sessions
        await session.handle("interview-1", ev(VoiceEventType.UTTERANCE_CANCEL, "utt-2"))
        assert provider.sessions == {}
        await session.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# ElevenLabs streaming via httpx.MockTransport
# --------------------------------------------------------------------------


class ScriptedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, gate: asyncio.Event | None = None, error: Exception | None = None) -> None:
        self.chunks = chunks
        self.gate = gate
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if index == 1 and self.gate is not None:
                await self.gate.wait()
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def eleven_http(monkeypatch: pytest.MonkeyPatch):
    state: dict = {"responses": [], "requests": []}
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        assert request.url.host == "api.elevenlabs.io"
        response = state["responses"].pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("voice.real_providers.httpx.AsyncClient", client_factory)
    return state


def make_tts(max_retries: int = 1) -> ElevenLabsTTSProvider:
    provider = ElevenLabsTTSProvider(api_key=SENTINEL_TTS_KEY, voice_id="voice-123", max_retries=max_retries)

    async def no_sleep(_seconds):
        return None

    provider._sleep = no_sleep
    return provider


async def collect(provider: ElevenLabsTTSProvider, text: str = "Hello there") -> list[bytes]:
    return [chunk async for chunk in provider.stream(text)]


def test_elevenlabs_streams_chunks_progressively(eleven_http):
    async def scenario():
        gate = asyncio.Event()
        body = ScriptedStream([b"aa", b"bb", b"cc"], gate=gate)
        eleven_http["responses"].append(httpx.Response(200, stream=body))
        provider = make_tts()
        stream = provider.stream("Tell me about a project.")
        first = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert first == b"aa"
        assert not gate.is_set()  # provider body not finished yet
        gate.set()
        rest = [chunk async for chunk in stream]
        assert rest == [b"bb", b"cc"]
        assert body.closed
        request = eleven_http["requests"][0]
        assert request.method == "POST"
        assert request.url.path == "/v1/text-to-speech/voice-123/stream"
        assert request.headers["xi-api-key"] == SENTINEL_TTS_KEY

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "TTS_AUTH_ERROR"), (403, "TTS_AUTH_ERROR"), (429, "TTS_RATE_LIMITED"), (400, "TTS_ERROR"), (422, "TTS_ERROR")],
)
def test_elevenlabs_4xx_normalized_and_not_retried(eleven_http, status, code):
    async def scenario():
        eleven_http["responses"].append(httpx.Response(status, json={"detail": f"echo {SENTINEL_TTS_KEY}"}))
        with pytest.raises(TTSProviderError) as raised:
            await collect(make_tts(max_retries=2))
        assert raised.value.code == code
        assert SENTINEL_TTS_KEY not in str(raised.value)
        assert len(eleven_http["requests"]) == 1

    asyncio.run(scenario())


def test_elevenlabs_5xx_retried_before_first_chunk(eleven_http):
    async def scenario():
        eleven_http["responses"].extend([httpx.Response(503), httpx.Response(200, content=b"audio")])
        assert await collect(make_tts(max_retries=1)) == [b"audio"]
        assert len(eleven_http["requests"]) == 2

    asyncio.run(scenario())


def test_elevenlabs_5xx_exhausts_retries(eleven_http):
    async def scenario():
        eleven_http["responses"].extend([httpx.Response(500), httpx.Response(502)])
        with pytest.raises(TTSProviderError) as raised:
            await collect(make_tts(max_retries=1))
        assert raised.value.code == "TTS_ERROR"
        assert len(eleven_http["requests"]) == 2

    asyncio.run(scenario())


def test_elevenlabs_timeout_before_first_chunk_is_retried_then_normalized(eleven_http):
    async def scenario():
        eleven_http["responses"].extend([httpx.ConnectTimeout("slow"), httpx.Response(200, content=b"ok")])
        assert await collect(make_tts(max_retries=1)) == [b"ok"]
        eleven_http["requests"].clear()
        eleven_http["responses"].extend([httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow")])
        with pytest.raises(TTSProviderError) as raised:
            await collect(make_tts(max_retries=1))
        assert raised.value.code == "TTS_TIMEOUT"
        assert len(eleven_http["requests"]) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "code"),
    [(httpx.ReadError("reset"), "TTS_ERROR"), (httpx.ReadTimeout("stalled"), "TTS_TIMEOUT")],
)
def test_elevenlabs_failure_after_first_chunk_is_not_retried(eleven_http, error, code):
    async def scenario():
        eleven_http["responses"].append(httpx.Response(200, stream=ScriptedStream([b"one", b"two"], error=error)))
        eleven_http["responses"].append(httpx.Response(200, content=b"must-not-be-fetched"))
        received = []
        with pytest.raises(TTSProviderError) as raised:
            async for chunk in make_tts(max_retries=3).stream("hello"):
                received.append(chunk)
        assert raised.value.code == code
        assert received == [b"one", b"two"]
        assert len(eleven_http["requests"]) == 1

    asyncio.run(scenario())


def test_elevenlabs_consumer_close_stops_streaming(eleven_http):
    async def scenario():
        gate = asyncio.Event()
        body = ScriptedStream([b"aa", b"bb"], gate=gate)
        eleven_http["responses"].append(httpx.Response(200, stream=body))
        stream = make_tts().stream("hello")
        assert await stream.__anext__() == b"aa"
        await stream.aclose()
        assert body.closed
        assert len(eleven_http["requests"]) == 1

    asyncio.run(scenario())


def test_fake_tts_provider_stays_deterministic():
    async def scenario():
        provider = FakeTTSProvider(chunk_size=4)
        first = [chunk async for chunk in provider.stream("abcdefghij")]
        second = [chunk async for chunk in provider.stream("abcdefghij")]
        assert first == second == [b"abcd", b"efgh", b"ij"]

    asyncio.run(scenario())
