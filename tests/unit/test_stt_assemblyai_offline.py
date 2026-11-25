"""Offline protocol and error-handling tests for AssemblyAISTTProvider.

Every HTTP call goes to httpx.MockTransport; no audio is sent to AssemblyAI.
These tests prove the provider follows AssemblyAI's documented pre-recorded
flow (upload -> create transcript -> poll) as read from public docs. They do
NOT show that the flow works against the real AssemblyAI API.
"""

from __future__ import annotations

import inspect
import io
import json
import wave

import httpx
import pytest

from voice.contracts import AudioFormat
from voice.providers import get_provider
from voice.real_providers import AssemblyAISTTProvider, STTProviderError

SENTINEL_KEY = "sentinel-stt-key-41c2"
PCM_CHUNK = b"\x01\x00" * 1600  # 0.1s of PCM16 16kHz mono
UPLOAD_URL = "https://cdn.assemblyai.com/upload/synthetic-upload-id"
TRANSCRIPT_ID = "5551722-f677-48a7-9b19-4a8ab4a6c4b1"


def _upload_ok() -> httpx.Response:
    return httpx.Response(200, json={"upload_url": UPLOAD_URL})


def _transcript(status: str, **fields) -> httpx.Response:
    return httpx.Response(200, json={"id": TRANSCRIPT_ID, "status": status, **fields})


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch):
    """Route the provider's AsyncClient through a programmable MockTransport."""
    state = {"responses": [], "requests": []}
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        assert request.url.host == "api.assemblyai.com"
        response = state["responses"].pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("voice.real_providers.httpx.AsyncClient", client_factory)
    return state


class FakeClock:
    """Deterministic clock; sleeping advances it instead of waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def _provider_with_audio(utterance_id: str = "utt-1", **kwargs) -> AssemblyAISTTProvider:
    kwargs.setdefault("max_retries", 1)
    provider = AssemblyAISTTProvider(api_key=SENTINEL_KEY, **kwargs)
    clock = FakeClock()
    provider._clock = clock
    provider._sleep = clock.sleep
    await provider.start_session(utterance_id, AudioFormat())
    async for _event in provider.send_audio(utterance_id, PCM_CHUNK):
        pass
    return provider


async def _final_events(provider: AssemblyAISTTProvider, utterance_id: str = "utt-1"):
    return [event async for event in provider.end_utterance(utterance_id)]


def _assert_safe_error(error: STTProviderError) -> None:
    assert SENTINEL_KEY not in f"{error!s} {error.message} {error!r}"


def test_send_audio_honours_async_iterator_protocol():
    assert inspect.isasyncgenfunction(AssemblyAISTTProvider.send_audio)
    assert inspect.isasyncgenfunction(AssemblyAISTTProvider.end_utterance)


async def test_send_audio_buffers_without_partial_events():
    provider = AssemblyAISTTProvider(api_key=SENTINEL_KEY)
    await provider.start_session("utt-1", AudioFormat())
    events = [event async for event in provider.send_audio("utt-1", PCM_CHUNK)]
    assert events == []
    assert bytes(provider.sessions["utt-1"]["chunks"]) == PCM_CHUNK


async def test_send_audio_requires_started_session():
    provider = AssemblyAISTTProvider(api_key=SENTINEL_KEY)
    with pytest.raises(STTProviderError) as excinfo:
        async for _event in provider.send_audio("missing", PCM_CHUNK):
            pass
    assert excinfo.value.code == "SESSION_ERROR"


async def test_utterance_over_120_seconds_is_rejected():
    provider = AssemblyAISTTProvider(api_key=SENTINEL_KEY)
    await provider.start_session("utt-1", AudioFormat())
    with pytest.raises(STTProviderError) as excinfo:
        async for _event in provider.send_audio("utt-1", b"\x00\x00" * (120 * 16000 + 1)):
            pass
    assert excinfo.value.code == "AUDIO_TOO_LARGE"


async def test_happy_path_upload_create_poll_completed(mock_http):
    mock_http["responses"] = [
        _upload_ok(),
        _transcript("queued"),
        _transcript("processing", text=None),
        _transcript("completed", text="I scaled the service.", confidence=0.91),
    ]
    provider = await _provider_with_audio()

    events = await _final_events(provider)

    assert [(event.text, event.is_final, event.confidence) for event in events] == [
        ("I scaled the service.", True, 0.91)
    ]
    assert provider.sessions == {}

    requests = mock_http["requests"]
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/v2/upload"),
        ("POST", "/v2/transcript"),
        ("GET", f"/v2/transcript/{TRANSCRIPT_ID}"),
        ("GET", f"/v2/transcript/{TRANSCRIPT_ID}"),
    ]
    for request in requests:
        assert request.url.scheme == "https"
        assert request.headers["authorization"] == SENTINEL_KEY

    upload = requests[0]
    assert upload.headers["content-type"] == "application/octet-stream"
    with wave.open(io.BytesIO(upload.content), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.readframes(wav_file.getnframes()) == PCM_CHUNK

    create_body = json.loads(requests[1].content)
    assert create_body == {"audio_url": UPLOAD_URL, "language_code": "en"}
    assert "audio_data" not in create_body

    # One poll interval slept before each poll; never faster than 1 poll/second.
    assert provider._clock.sleeps == [1.0, 1.0]


async def test_create_response_already_completed_skips_polling(mock_http):
    mock_http["responses"] = [_upload_ok(), _transcript("completed", text="Done.", confidence=None)]
    provider = await _provider_with_audio()

    events = await _final_events(provider)

    assert [(event.text, event.confidence) for event in events] == [("Done.", None)]
    assert len(mock_http["requests"]) == 2


@pytest.mark.parametrize(
    ("language", "model", "expected"),
    [
        ("en-US", "local-fake", {"language_code": "en_us"}),
        ("en", "universal-2", {"language_code": "en", "speech_models": ["universal-2"]}),
        ("auto", "best", {"language_detection": True}),
    ],
)
async def test_transcript_request_maps_language_and_model(mock_http, language, model, expected):
    mock_http["responses"] = [_upload_ok(), _transcript("completed", text="ok")]
    provider = await _provider_with_audio(language=language, model=model)

    await _final_events(provider)

    assert json.loads(mock_http["requests"][1].content) == {"audio_url": UPLOAD_URL, **expected}


@pytest.mark.parametrize(
    "final_body",
    [
        {"id": TRANSCRIPT_ID, "status": "error", "error": "synthetic failure"},
        {"id": TRANSCRIPT_ID, "status": "completed"},
        {"id": TRANSCRIPT_ID, "status": "completed", "text": None},
        {"id": TRANSCRIPT_ID, "status": "mystery"},
    ],
)
async def test_non_completed_transcript_is_rejected_not_submitted(mock_http, final_body):
    mock_http["responses"] = [_upload_ok(), _transcript("queued"), httpx.Response(200, json=final_body)]
    provider = await _provider_with_audio()

    with pytest.raises(STTProviderError) as excinfo:
        await _final_events(provider)

    assert excinfo.value.code == "STT_ERROR"
    assert "synthetic failure" not in excinfo.value.message
    assert provider.sessions == {}


async def test_polling_past_deadline_is_a_timeout(mock_http):
    mock_http["responses"] = [_upload_ok(), _transcript("queued")] + [
        _transcript("processing") for _ in range(20)
    ]
    provider = await _provider_with_audio(transcript_deadline_seconds=5.0, poll_interval_seconds=1.0)

    with pytest.raises(STTProviderError) as excinfo:
        await _final_events(provider)

    assert excinfo.value.code == "STT_TIMEOUT"
    # upload + create + 4 polls; the poll due exactly at the deadline is not sent.
    assert len(mock_http["requests"]) == 6
    assert sum(provider._clock.sleeps) <= 5.0
    assert provider.sessions == {}


async def test_poll_interval_is_clamped_to_rate_limit():
    provider = AssemblyAISTTProvider(api_key=SENTINEL_KEY, poll_interval_seconds=0.1)
    assert provider.poll_interval_seconds == 1.0


@pytest.mark.parametrize("stage", [0, 1, 2])
@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [(401, "STT_AUTH_ERROR"), (403, "STT_AUTH_ERROR"), (429, "STT_RATE_LIMITED"), (400, "STT_ERROR"), (422, "STT_ERROR")],
)
async def test_http_errors_are_normalized_not_retried_and_never_echo_the_key(
    mock_http, stage, status_code, expected_code
):
    flow = [_upload_ok(), _transcript("queued"), _transcript("processing")]
    flow[stage] = httpx.Response(status_code, json={"error": SENTINEL_KEY})
    mock_http["responses"] = flow[: stage + 1]
    provider = await _provider_with_audio(max_retries=3)

    with pytest.raises(STTProviderError) as excinfo:
        await _final_events(provider)

    assert excinfo.value.code == expected_code
    _assert_safe_error(excinfo.value)
    assert len(mock_http["requests"]) == stage + 1  # 4xx at any stage is not retried
    assert provider.sessions == {}


async def test_upload_failure_stops_before_transcript_creation(mock_http):
    mock_http["responses"] = [httpx.Response(200, json={"detail": "no url"})]
    provider = await _provider_with_audio()

    with pytest.raises(STTProviderError) as excinfo:
        await _final_events(provider)

    assert excinfo.value.code == "STT_ERROR"
    assert [r.url.path for r in mock_http["requests"]] == ["/v2/upload"]


async def test_unsafe_transcript_id_is_rejected(mock_http):
    mock_http["responses"] = [_upload_ok(), httpx.Response(200, json={"id": "../v2/upload", "status": "queued"})]
    provider = await _provider_with_audio()

    with pytest.raises(STTProviderError) as excinfo:
        await _final_events(provider)

    assert excinfo.value.code == "STT_ERROR"
    assert len(mock_http["requests"]) == 2


async def test_server_errors_are_retried_then_reported(mock_http):
    mock_http["responses"] = [httpx.Response(503), httpx.Response(503)]
    provider = await _provider_with_audio(max_retries=1)

    with pytest.raises(STTProviderError) as excinfo:
        await _final_events(provider)

    assert excinfo.value.code == "STT_ERROR"
    assert len(mock_http["requests"]) == 2


async def test_transient_poll_server_error_is_retried_and_recovers(mock_http):
    mock_http["responses"] = [
        _upload_ok(),
        _transcript("queued"),
        httpx.Response(502),
        _transcript("completed", text="Recovered.", confidence=0.5),
    ]
    provider = await _provider_with_audio(max_retries=1)

    events = await _final_events(provider)

    assert [event.text for event in events] == ["Recovered."]
    assert len(mock_http["requests"]) == 4


async def test_timeouts_are_retried_then_reported(mock_http):
    mock_http["responses"] = [
        httpx.ReadTimeout("synthetic timeout"),
        httpx.ReadTimeout("synthetic timeout"),
    ]
    provider = await _provider_with_audio(max_retries=1)

    with pytest.raises(STTProviderError) as excinfo:
        await _final_events(provider)

    assert excinfo.value.code == "STT_TIMEOUT"
    assert len(mock_http["requests"]) == 2
    assert provider.sessions == {}


async def test_network_errors_do_not_echo_exception_text(mock_http):
    mock_http["responses"] = [
        httpx.ConnectError(f"boom {SENTINEL_KEY}"),
        httpx.ConnectError(f"boom {SENTINEL_KEY}"),
    ]
    provider = await _provider_with_audio(max_retries=1)

    with pytest.raises(STTProviderError) as excinfo:
        await _final_events(provider)

    assert excinfo.value.code == "STT_ERROR"
    _assert_safe_error(excinfo.value)


@pytest.mark.parametrize("stage", [0, 2])
async def test_invalid_json_is_a_provider_error(mock_http, stage):
    flow = [_upload_ok(), _transcript("queued"), _transcript("processing")]
    flow[stage] = httpx.Response(200, content=b"not-json")
    mock_http["responses"] = flow[: stage + 1]
    provider = await _provider_with_audio()

    with pytest.raises(STTProviderError) as excinfo:
        await _final_events(provider)

    assert excinfo.value.code == "STT_ERROR"
    assert len(mock_http["requests"]) == stage + 1


async def test_empty_utterance_is_rejected_without_a_request(mock_http):
    provider = AssemblyAISTTProvider(api_key=SENTINEL_KEY)
    await provider.start_session("utt-1", AudioFormat())

    with pytest.raises(STTProviderError) as excinfo:
        await _final_events(provider)

    assert excinfo.value.code == "STT_ERROR"
    assert mock_http["requests"] == []
    assert provider.sessions == {}


def test_unknown_stt_provider_fails_instead_of_falling_back_to_fake():
    with pytest.raises(ValueError):
        get_provider("bogus", "stt")
    with pytest.raises(ValueError):
        get_provider("assemblylabs", "stt", api_key="")
