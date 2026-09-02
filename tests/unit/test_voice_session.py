import base64
import asyncio

from voice.contracts import ClientVoiceEvent, VoiceEventType
from voice.providers import FakeSTTProvider, FakeTTSProvider, FakeVADProvider
from voice.session import VoiceSessionService


class FakeInterviewService:
    def __init__(self):
        self.calls = []

    def get_interview(self, interview_id, user_id, role):
        return {"interview_id": interview_id, "current_question": "Initial question"}

    def submit_answer(self, interview_id, answer, turn_id, user_id, role):
        self.calls.append((interview_id, answer, turn_id, user_id, role))
        return {"current_question": "How did you measure the result?"}


def test_partial_transcript_never_reaches_interview_service():
    asyncio.run(_test_partial_transcript_never_reaches_interview_service())


async def _test_partial_transcript_never_reaches_interview_service():
    service = FakeInterviewService()
    voice = VoiceSessionService(service, "user-1", "candidate")
    await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.SESSION_START))
    await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.AUDIO_START, utterance_id="utt-1"))

    events = await voice.handle(
        "interview-1",
        ClientVoiceEvent(
            type=VoiceEventType.AUDIO_CHUNK,
            utterance_id="utt-1",
            audio_base64=base64.b64encode(b"pcm").decode("ascii"),
        ),
    )

    assert [event.type for event in events] == ["transcript.partial"]
    assert service.calls == []


def test_final_transcript_commits_once_and_streams_question_audio():
    asyncio.run(_test_final_transcript_commits_once_and_streams_question_audio())


async def _test_final_transcript_commits_once_and_streams_question_audio():
    service = FakeInterviewService()
    voice = VoiceSessionService(service, "user-1", "candidate", vad=FakeVADProvider(), stt=FakeSTTProvider(), tts=FakeTTSProvider(4))
    await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.AUDIO_START, utterance_id="utt-1"))
    await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.AUDIO_CHUNK, utterance_id="utt-1", audio_base64=base64.b64encode(b"pcm").decode("ascii")))

    first = await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.AUDIO_END, utterance_id="utt-1"))
    second = await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.AUDIO_END, utterance_id="utt-1"))

    assert len(service.calls) == 1
    assert service.calls[0][2] == "utt-1"
    assert any(event.type == "transcript.final" for event in first)
    assert any(event.type == "tts.audio" for event in first)
    assert any(event.type == "interview.question" for event in second)


def test_audio_limits_return_structured_errors_and_clear_cancelled_buffer():
    asyncio.run(_test_audio_limits_return_structured_errors_and_clear_cancelled_buffer())


async def _test_audio_limits_return_structured_errors_and_clear_cancelled_buffer():
    service = FakeInterviewService()
    voice = VoiceSessionService(service, "user-1", "candidate", max_chunk_bytes=2, max_buffer_bytes=4)
    await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.AUDIO_START, utterance_id="utt-1"))
    oversized = await voice.handle(
        "interview-1",
        ClientVoiceEvent(type=VoiceEventType.AUDIO_CHUNK, utterance_id="utt-1", audio_base64=base64.b64encode(b"123").decode("ascii")),
    )
    assert oversized[0].code == "VOICE_AUDIO_TOO_LARGE"
    await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.UTTERANCE_CANCEL, utterance_id="utt-1"))
    assert "utt-1" not in voice.buffers


def test_buffer_overflow_returns_structured_error():
    asyncio.run(_test_buffer_overflow_returns_structured_error())


async def _test_buffer_overflow_returns_structured_error():
    voice = VoiceSessionService(FakeInterviewService(), "user-1", "candidate", max_chunk_bytes=4, max_buffer_bytes=4)
    await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.AUDIO_START, utterance_id="utt-2"))
    await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.AUDIO_CHUNK, utterance_id="utt-2", audio_base64=base64.b64encode(b"1234").decode("ascii")))
    events = await voice.handle("interview-1", ClientVoiceEvent(type=VoiceEventType.AUDIO_CHUNK, utterance_id="utt-2", audio_base64=base64.b64encode(b"5").decode("ascii")))
    assert events[0].code == "VOICE_BUFFER_OVERFLOW"


def test_unauthorized_session_does_not_start():
    asyncio.run(_test_unauthorized_session_does_not_start())


async def _test_unauthorized_session_does_not_start():
    class Unauthorized(FakeInterviewService):
        def get_interview(self, *args):
            return None

    events = await VoiceSessionService(Unauthorized(), "user-2", "candidate").handle(
        "interview-1", ClientVoiceEvent(type=VoiceEventType.SESSION_START)
    )
    assert events[0].code == "VOICE_INTERVIEW_FORBIDDEN"
