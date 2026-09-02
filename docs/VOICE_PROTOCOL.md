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

## Transcript Semantics

Partial transcripts are UI-only. They never call LangGraph, RAG, evaluation, or database writes.
Only a non-empty final transcript calls `InterviewService.submit_answer()`.

A repeated final for the same `utterance_id` reuses the session result and the durable interview
service's existing turn uniqueness/idempotency behavior. A correction arriving after durable
processing is not silently rewritten; future correction metadata belongs in the durable turn
model.

## Disconnect and Recovery

Disconnect before finalization leaves no durable voice turn. Disconnect after final commit leaves
durable interview state; reconnecting through REST or a new voice session retrieves that state. A
repeated `utterance_id` does not create another turn or question.

## Errors and Privacy

Errors contain stable safe codes and human-readable messages, not internal exceptions. Raw audio,
transcripts, prompts, and credentials are not logged. Provider failures return structured errors;
TTS failure does not remove the persisted text question.
