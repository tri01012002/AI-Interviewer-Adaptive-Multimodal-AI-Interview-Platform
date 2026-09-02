# Phase 9 Voice Report

**Date:** 2026-09-02  
**Status:** PASS WITH KNOWN LIMITATIONS

## 1. Implemented

- Typed client/server voice event contracts
- Authenticated `/ws/interview/{interview_id}/voice` WebSocket
- Existing JWT and repository ownership authorization reuse
- PCM16 little-endian, mono, 16 kHz audio contract
- Message, chunk, buffer, and utterance-size limits
- Bounded per-utterance audio buffering and cleanup
- `VADProvider`, `STTProvider`, and `TTSProvider` protocols
- Deterministic fake VAD, streaming fake STT, and chunked fake TTS providers
- `VoiceSessionService` orchestration layer
- Partial transcript UI-only behavior
- Final transcript convergence into existing `InterviewService.submit_answer()`
- `utterance_id` reuse as the existing durable turn idempotency key
- Duplicate final replay without duplicate business processing
- Structured safe voice errors
- TTS failure isolation after text question persistence
- Voice configuration in settings and `.env.example`
- Protocol, architecture, and observability documentation

## 2. Architecture

```text
Browser microphone
  -> authenticated WebSocket
  -> VoiceSessionService
  -> bounded PCM16 buffer
  -> VADProvider
  -> STTProvider
       -> partial transcript -> UI only
       -> final transcript -> InterviewService
  -> existing LangGraph/RAG/LLM/fallback
  -> durable question persistence
  -> TTSProvider stream
  -> WebSocket tts.audio events
  -> browser playback
```

The voice layer does not contain interview business rules, competency scoring, RAG algorithms,
LLM prompts, or database persistence logic.

## 3. Event Protocol

Client events: `session.start`, `audio.start`, `audio.chunk`, `audio.end`, `utterance.cancel`, and
`session.stop`.

Server events: `session.ready`, `transcript.partial`, `transcript.final`, `interview.question`,
`tts.started`, `tts.audio`, `tts.completed`, `error`, and `session.closed`.

Full protocol: [docs/VOICE_PROTOCOL.md](docs/VOICE_PROTOCOL.md).

## 4. Partial and Final Semantics

Partial STT events are emitted only as `transcript.partial`; they do not call LangGraph, RAG,
evaluation, `InterviewService`, or the database. A non-empty final transcript is the only event
that enters `InterviewService.submit_answer()`.

`utterance_id` is stable across an utterance and is passed as the existing `turn_id`. Duplicate
final events reuse the session result and existing durable idempotency behavior.

## 5. Recovery

Disconnect before finalization leaves no durable voice turn. Disconnect after final commit leaves
durable interview state. Reconnect retrieves current state through the existing authorized service;
repeating the same `utterance_id` does not create a second turn or question. Post-commit STT
correction metadata is not silently rewritten and remains deferred.

## 6. Security and Limits

JWT validation and current-user lookup reuse existing auth. Interview access uses the existing
repository ownership/role policy. Invalid or unavailable interviews are rejected before voice
session acceptance. Audio uses explicit PCM16/16 kHz/mono validation. Defaults are 64 KiB chunks,
2 MiB buffered audio, 120-second utterances, and 128 KiB WebSocket messages.

Raw audio, transcripts, and prompts are not logged. Stable IDs, lengths, statuses, provider metadata,
and safe error codes are the intended telemetry fields.

## 7. Observability

The documented telemetry surface includes session/interview/utterance IDs, audio buffer/duration,
STT connection/partial/final latency, turn commit and graph latency, TTS first-audio/total latency,
provider errors, duplicate replays, and WebSocket cleanup. Percentiles p50/p95/p99 are deployment
metrics targets, not measured production claims in this phase.

## 8. Provider Verification

| Provider | Fake tested | Real tested |
|---|---:|---:|
| VAD | YES | NO |
| STT | YES | NO |
| TTS | YES | NO |

Real streaming STT/TTS vendor adapters are not implemented. No external voice credential is
configured and no live provider call was made.

## 9. Tests and Verification

```text
Focused voice suite:
python -m pytest tests/unit/test_voice_session.py tests/unit/test_voice_websocket.py
7 passed

Full suite:
python -m pytest
59 passed, 125 warnings

Migration:
python -m alembic current
8b2f_evidence_assessments (head)
python -m alembic downgrade -1
python -m alembic upgrade head
8b2f_evidence_assessments (head)

git diff --check
passed
```

Diagnostics for touched voice and WebSocket files are clean.

## 10. Known Limitations

- STT/TTS are deterministic fake providers only; no live vendor integration or live verification.
- Voice events currently use JSON/base64 audio rather than a production binary audio frame
  protocol or provider streaming connection.
- WebSocket authentication uses the JWT query parameter required by browser clients; deployment
  must protect URLs and avoid token leakage in access logs.
- The session uses an in-memory committed-result cache for same-session replay; durable turn
  idempotency remains the authoritative protection across reconnects/processes.
- Provider operation timeouts, real backpressure queues, VAD endpointing, interruption handling,
  correction reconciliation, and p50/p95/p99 measurements are not production-complete.
- Existing synchronous interview processing is called from the async session and should move to
  an executor or async service before high-concurrency deployment.
- PostgreSQL, multi-process WebSocket scaling, load testing, and deployment verification remain
  unavailable or not implemented.

## 11. Production Readiness

**PARTIAL.** The modular-monolith voice contract and final-only durable integration are tested with
fake providers. Before production deployment, implement and live-verify streaming STT/TTS adapters,
provider timeout/retry behavior, binary framing/backpressure, endpointing/interruption handling,
secure WebSocket token transport, async offloading, metrics collection, PostgreSQL concurrency,
and controlled voice load tests.

Redis, Celery, browser automation, Kubernetes, and unrelated distributed infrastructure were not
added in Phase 9.

Changes are not committed.
