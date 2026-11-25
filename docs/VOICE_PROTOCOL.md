# Voice WebSocket Protocol

**Status:** local fake-provider implementation; live STT/TTS not verified

## Connection and Authentication

Connect to `/ws/interview/{interview_id}/voice?token={JWT}`. The token is the existing bearer JWT
passed as a query value because browser WebSocket clients cannot reliably set Authorization
headers. The server validates the JWT, resolves the user record, and applies the same interview
ownership/role query used by REST. Unauthorized or unavailable interviews are closed with 4401 or
4403 before session acceptance.

## Audio Contract

Audio is JSON base64 encoded `pcm_s16le`, mono, 16 kHz. Clients should include
`audio_format: "pcm_s16le/16000/1"`. Default limits are a 64 KiB chunk, 2 MiB buffered session,
120-second utterance, and 128 KiB WebSocket message.

## Client Events

```json
{"type":"session.start"}
{"type":"audio.start","utterance_id":"utt_123"}
{"type":"audio.chunk","utterance_id":"utt_123","audio_format":"pcm_s16le/16000/1","audio_base64":"..."}
{"type":"audio.end","utterance_id":"utt_123"}
{"type":"utterance.cancel","utterance_id":"utt_123"}
{"type":"session.stop"}
```

Every event for an utterance uses the same stable `utterance_id`. It is the idempotency key for
final transcript commit.

## Server Events

`session.ready`, `transcript.partial`, `transcript.final`, `interview.question`, `tts.started`,
`tts.audio`, `tts.completed`, `error`, and `session.closed` are emitted as typed JSON events.
`tts.audio.audio_base64` contains streamed fake-provider bytes in this local implementation.

Event types are unchanged; delivery is now progressive. After `audio.end` the server sends, in
order: `transcript.final`, `interview.question`, `tts.started`, one `tts.audio` per provider chunk
**as each chunk arrives** (the server never accumulates the whole response), then `tts.completed`.
If TTS fails part-way, already-sent chunks stand and a single `error` (`TTS_PROVIDER_ERROR`,
`TTS_TIMEOUT` or `TTS_RATE_LIMITED`, with `utterance_id`) replaces `tts.completed`; chunks are never
re-sent (the provider retries only before its first chunk). Clients should play `tts.audio` chunks
as they arrive and fall back to the displayed question on a TTS error.

Error events may carry `retryable: true`. New codes:

- `VOICE_TURN_IN_PROGRESS` (retryable): an `audio.end` arrived while this session's previous answer
  is still being processed. Per-session turns are strictly ordered; retry after the in-flight
  answer's events finish.
- `VOICE_BUSY` (retryable): the server's bounded answer-processing pool
  (`VOICE_ANSWER_MAX_CONCURRENCY`) had no free slot within `VOICE_ANSWER_QUEUE_TIMEOUT_SECONDS`.
  Retry the same `audio.end`/`utterance_id`.
- `TURN_COMMIT_FAILED` is now marked `retryable: true`.

Answer processing runs off the event loop, so other events on the same socket (for example
`session.start`, `utterance.cancel`) and other sessions keep being served while an answer is
processed.

## Cancellation

`utterance.cancel` releases the session's buffered audio and the STT provider's audio for that
utterance (`cancel_utterance`), and clears the utterance that binary frames are appended to. Later
chunks (JSON or binary) for the cancelled `utterance_id` are rejected with `VOICE_PROTOCOL_ERROR`
until a new `audio.start` reuses the id; a restarted utterance never contains earlier audio.
Cancelling the utterance whose answer is being processed stops transcript/TTS delivery; an answer
already handed to the interview service still completes durably (see below). Cancel is idempotent
and accepts unknown ids.

## Transcript Semantics

Partial transcripts are UI-only. They never call LangGraph, RAG, evaluation, or database writes.
Only a non-empty final transcript calls `InterviewService.submit_answer()`.

A repeated final for the same `utterance_id` reuses the session result and the durable interview
service's existing turn uniqueness/idempotency behavior. A correction arriving after durable
processing is not silently rewritten; future correction metadata belongs in the durable turn
model.

## Disconnect and Recovery

Disconnect before finalization leaves no durable voice turn. Disconnect while an answer is being
processed stops event delivery immediately (nothing is sent after close) and releases all session
and provider buffers; the answer already submitted to the interview service still completes in its
worker thread, and retrying the same `utterance_id` returns that durable result. Disconnect after final commit leaves
durable interview state; reconnecting through REST or a new voice session retrieves that state. A
repeated `utterance_id` does not create another turn or question.

## Errors and Privacy

Errors contain stable safe codes and human-readable messages, not internal exceptions. Raw audio,
transcripts, prompts, and credentials are not logged. Provider failures return structured errors;
TTS failure does not remove the persisted text question.
