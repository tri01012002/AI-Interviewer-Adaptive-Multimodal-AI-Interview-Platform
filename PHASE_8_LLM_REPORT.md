# Phase 8 LLM Provider Report

**Date:** 2026-09-02  
**Status:** PARTIAL

## 1. Audit

The repository had no LLM provider abstraction. The graph used a deterministic analyzer, evaluation
used `DeterministicEvaluator`, and RAG used the local hybrid retrieval pipeline. OpenAI settings and
`.env.example` placeholders existed, but provider selection, timeout, retry, and base URL settings
did not. No API key is configured in this environment.

Detailed audit: [PHASE_8_AUDIT.md](PHASE_8_AUDIT.md).

## 2. Architecture

```text
LangGraph
  -> LLMProvider abstraction
      -> OpenAIProvider (real HTTP implementation, optional)
      -> deterministic graph/evaluation fallbacks

InterviewService/repositories
  -> durable state and transaction ownership
```

The provider boundary exposes `generate` and `generate_structured`. Provider-specific HTTP response
objects do not escape into graph or domain code. No streaming infrastructure was added.

## 3. Concrete Provider

`OpenAIProvider` is a real OpenAI-compatible chat-completions HTTP client implemented with `httpx`.
It uses an environment-provided API key, model, endpoint, timeout, max tokens, temperature, and
bounded retry count. `provider_from_settings()` returns it only when `LLM_PROVIDER=openai` and a
non-empty key are explicitly configured.

**LIVE PROVIDER TEST: NOT VERIFIED.** No API key is configured and no external request was made.

## 4. Configuration

Added settings:

- `LLM_PROVIDER`, default `none`
- `OPENAI_BASE_URL`
- `LLM_TIMEOUT_SECONDS`
- `LLM_MAX_RETRIES`

`.env.example` contains placeholders only. No secrets were added.

## 5. Structured Output

The graph reuses validated `AnswerAnalysis` and `QuestionProposal` Pydantic contracts. The provider
adds a JSON schema instruction and validates the response before returning it. Invalid output is
retried within the configured bound, then raises `LLMInvalidOutputError`; the graph records an
explicit deterministic fallback error.

Candidate answer content is sent only in a clearly labeled `UNTRUSTED_CANDIDATE_CONTENT` section.
Question generation receives competency, gap, difficulty, prior-turn count, and clearly labeled
`UNTRUSTED_RETRIEVED_CONTEXT`; it does not receive the full prior answer history.

## 6. Retry, Timeout, and Failure Handling

The provider distinguishes configuration/authentication errors, rate limits, timeouts, network
failures, server failures, and invalid structured output. Retry count is bounded. Authentication
errors are not retried. Server/network/timeouts may retry with exponential backoff and jitter.
Question generation and answer analysis fall back deterministically when provider failures are
normalized into `LLMProviderError`.

## 7. LangGraph Integration

`InterviewAgentCore` accepts an optional `LLMProvider`. When configured, the graph uses
`generate_structured` for answer analysis and question generation. Deterministic routing remains
authoritative: provider output cannot select arbitrary application transitions, unknown
competencies are filtered, and question competency/difficulty must match the deterministic decision.
The service still owns durable persistence.

## 8. RAG Integration

The existing RAG abstraction remains unchanged and is called through an explicit graph
`retrieve_context` node. Retrieved content is formatted as untrusted source-attributed context.
The model is instructed to use it as reference data, not system instructions. Context and prior
history are bounded.

## 9. Evaluation Integration

The Phase 6 deterministic evaluator remains the default and fallback. No LLM evaluator was added.
Evidence, score, assessment state, and evidence-sufficiency confidence remain separate. No LLM
score is treated as ground truth.

## 10. Idempotency and Persistence

The existing client `turn_id`, durable turn lifecycle, unique constraints, repository transactions,
and assessment idempotency remain in place. A duplicate completed turn does not invoke the graph
again. Provider retries are limited to provider calls and do not create durable side effects.

No database migration was required for provider metadata; provider/model/prompt values are
available in telemetry and returned graph state.

## 11. Privacy and Security

Normal provider/graph telemetry records operation, provider, model, latency, retry count, token
usage when supplied, prompt version, and failure type. It does not record raw candidate answers or
full prompts. Candidate and retrieved text are explicitly untrusted data. No tools or arbitrary
application transitions are exposed to the provider.

## 12. Tests and Verification

```text
python -m pytest
52 passed, 118 warnings

Focused provider/graph/RAG run:
19 passed, 1 warning

python -m alembic current
8b2f_evidence_assessments (head)

python -m alembic downgrade -1
python -m alembic upgrade head
8b2f_evidence_assessments (head)

git diff --check
passed
```

Provider tests cover valid structured responses, malformed output with bounded retry, timeout
normalization, explicit provider selection, graph integration, untrusted candidate/context
separation, and deterministic fallback behavior. Touched files report no diagnostics.

## 13. Status and Limitations

| Capability | Status |
|---|---|
| Minimal LLM provider abstraction | IMPLEMENTED |
| Real OpenAI-compatible provider | IMPLEMENTED, LIVE UNVERIFIED |
| Environment-based configuration | IMPLEMENTED |
| Structured output validation | IMPLEMENTED |
| Bounded retry/timeout handling | IMPLEMENTED |
| LangGraph provider integration | IMPLEMENTED |
| RAG context integration | IMPLEMENTED locally |
| Deterministic fallback | IMPLEMENTED |
| Provider/model/prompt version exposure | IMPLEMENTED |
| External LLM evaluation | NOT IMPLEMENTED |
| Live API verification | NOT VERIFIED |
| Production token/cost accounting | PARTIAL; usage fields logged when provider returns them |
| PostgreSQL verification | NOT VERIFIED |
| Voice, Redis, Celery, browser, Kubernetes | NOT IMPLEMENTED |

## 14. Production Readiness

**PARTIAL, not production-ready.** The real provider boundary and local failure behavior are tested,
but no live provider call was verified, no provider rate-limit/load test was run, and the default
path remains deterministic when no key is configured. PostgreSQL and external deployment behavior
remain unverified.

Changes are not committed.
