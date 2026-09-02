# Voice Architecture

```text
Browser microphone
  -> authenticated WebSocket
  -> VoiceSessionService
  -> bounded audio buffer + PCM16 validation
  -> VADProvider
  -> STTProvider
       -> partial transcript -> UI only
       -> final transcript -> InterviewService
  -> existing LangGraph/RAG/LLM/fallback
  -> durable question persistence
  -> TTSProvider stream
  -> WebSocket audio events
  -> browser playback
```

`VoiceSessionService` owns session lifecycle, framing, provider calls, event emission, cleanup,
and transcript idempotency. It does not own competency scoring, RAG algorithms, prompts, or
persistence rules. Final transcripts converge on the existing `InterviewService` and its durable
`interview_turns` uniqueness boundary.

## Failure and Timeout Boundaries

The local fake providers are bounded by audio/message limits. Provider protocols are asynchronous
and permit future connection, idle, finalization, and synthesis timeout adapters. STT failure
returns a structured error and leaves interview state untouched. TTS failure occurs after question
persistence and returns the text question plus a structured error.

## Retry and Idempotency

Only final transcript commit enters business processing. `utterance_id` is passed as the existing
client turn ID. Duplicate final events reuse the session result or durable service replay. No
second voice state machine or database is introduced.

## Real-Time vs Durable Work

Audio chunks, VAD decisions, partial transcripts, and TTS chunks are real-time ephemeral work.
Final transcript turn processing, evidence, assessment, competency state, and question persistence
are durable business operations owned by the existing application service.

## Current Provider Status

`FakeVADProvider`, `FakeSTTProvider`, and `FakeTTSProvider` provide deterministic local behavior.
Real streaming STT/TTS vendor adapters are not implemented or live-verified in this phase.
