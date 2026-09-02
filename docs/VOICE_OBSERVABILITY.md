# Voice Observability

## Correlation IDs

Track `session_id`, `interview_id`, and `utterance_id` in voice events and logs. Provider/model
identifiers should be recorded when configured. Raw audio and transcript text are excluded.

## Metrics

The session boundary supports measuring:

- audio receive latency and buffer size
- audio duration and transcript length
- STT connection, first-partial, final, and utterance latency
- final transcript to question persistence latency
- TTS first-audio and total streaming latency
- provider error counts and safe fallback counts
- duplicate final/replay count
- WebSocket disconnect and cleanup count

Percentiles p50, p95, and p99 should be calculated by the deployment metrics backend. This phase
only emits structured metadata and does not claim a production benchmark.

## Error Codes

Current stable codes include `VOICE_INVALID_EVENT`, `VOICE_INVALID_AUDIO`,
`VOICE_AUDIO_TOO_LARGE`, `VOICE_BUFFER_OVERFLOW`, `VOICE_INTERVIEW_FORBIDDEN`, `STT_PROVIDER_ERROR`,
`STT_TIMEOUT`, `TURN_COMMIT_FAILED`, and `TTS_PROVIDER_ERROR`.

## Privacy Rules

Never log raw microphone bytes, full candidate transcripts, full prompts, or retrieved content.
Use IDs, lengths, durations, provider names, versions, counts, and error classifications instead.
