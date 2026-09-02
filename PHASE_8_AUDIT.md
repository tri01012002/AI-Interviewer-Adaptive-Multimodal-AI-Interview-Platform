# Phase 8 LLM Integration Audit

**Date:** 2026-09-02

## Actual Current State

| Surface | Current state | Classification |
|---|---|---|
| LLM abstraction | None | NOT IMPLEMENTED |
| Semantic answer analysis | `_default_analyzer` in the LangGraph module | DETERMINISTIC FALLBACK |
| Question generation | Graph templates plus RAGQuestionService selection | DETERMINISTIC |
| Evidence extraction | Graph answer analysis plus deterministic evaluator | DETERMINISTIC |
| Evaluation | `DeterministicEvaluator` behind an evaluator protocol | IMPLEMENTED LOCAL / NOT LLM |
| RAG context | In-memory hybrid retrieval with BM25, deterministic vectors, RRF, reranker | PARTIAL LOCAL |
| Configuration | OpenAI key/model settings exist; provider, timeout, retry settings do not | PARTIAL |
| Secrets | `.env.example` contains placeholders; no live key is configured in the environment | NOT VERIFIED LIVE |
| Persistence | Service/repositories own durable state; graph has no database writes | IMPLEMENTED BOUNDARY |
| Logging | Graph/evaluation/RAG telemetry excludes raw answers by design; provider must preserve this | PARTIAL |

## Phase 8 Boundary

Add a minimal provider-neutral `LLMProvider` with `generate` and `generate_structured`, plus a
real OpenAI-compatible HTTP implementation. No SDK-specific objects will escape the provider
boundary. The provider is optional and disabled by default when no key is configured.

The deterministic analyzer/evaluator remain the safe fallback. No fake provider will be created,
and no live provider result can be verified without credentials.

## Known Risks

- The existing graph is synchronous, so provider calls use a bounded synchronous HTTP request.
- No external API key is configured; live provider behavior will remain NOT VERIFIED.
- Candidate and retrieved content must be delimited as untrusted data and excluded from logs.
- Provider output must be schema-validated before graph state is accepted.
