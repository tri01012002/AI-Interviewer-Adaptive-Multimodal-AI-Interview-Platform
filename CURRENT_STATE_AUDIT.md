# Current State Audit

**Date:** 2026-09-02  
**Source of truth:** repository source, migrations, tests, and working-tree history

## 1. Current Architecture

The implemented request path is:

```text
FastAPI route -> module-level InterviewAgentCore / EvaluationService / RAGQuestionService
              -> temporary InterviewStore -> SQLAlchemy SessionLocal -> configured database
```

Authentication exists for auth and candidate routes, but interview routes currently do not
depend on authentication or an application service. The WebSocket route is present but is not
an implemented interview transport.

## 2. Actual Implementation Status

| Area | Status | Evidence |
|---|---|---|
| FastAPI REST API | IMPLEMENTED | `apps/api/main.py` and versioned routes |
| Authentication | PARTIAL | JWT login/register/me and PBKDF2 hashing exist |
| Authorization/RBAC | PARTIAL | Interview endpoints now require bearer authentication; role enforcement and resource ownership are still absent |
| Database persistence | PARTIAL | SQLAlchemy models and normalized interview tables exist; `state_json` remains authoritative for compatibility |
| Repository layer | PARTIAL | `services/repositories.py` provides session-scoped interview, question, and turn repositories; legacy `InterviewStore` remains as a facade |
| Application service | IMPLEMENTED | `InterviewService` owns orchestration and routes delegate to it |
| Transactions | PARTIAL | Start and answer operations use one `SessionLocal.begin()` boundary; normalized evidence/competency writes are not yet part of the turn operation |
| Turn idempotency | PARTIAL | API requires client `turn_id`, has a unique database key, persists processing/completed status, and replays completed snapshots; concurrent duplicate handling and retryable recovery are not complete |
| Crash recovery | PARTIAL | Durable processing claims, retryable failure marking, and stale-claim reclaim exist; no distributed recovery worker |
| Adaptive agent | MOCKED | `InterviewAgentCore` uses keyword matching and procedural branches; no LangGraph graph |
| Evidence extraction | MOCKED | Keyword strings are labeled as evidence |
| Evaluation | MOCKED | Scores are derived from keyword counts and evidence count |
| RAG | MOCKED | Hardcoded position-to-question dictionary; no ingestion, embeddings, lexical retrieval, or reranking |
| Voice/STT/TTS/VAD | MOCKED | STT returns a placeholder transcript and TTS encodes text; no streaming or VAD |
| Browser agent | PLANNED | Package marker only; no structured execution |
| Redis/workers | PARTIAL | Dependencies and compose services exist; no runtime use verified in the interview path |
| Observability | PARTIAL | Logging exists; no request/turn correlation, AI cost telemetry, or metrics path verified |
| CI/CD | PARTIAL | A test workflow exists, but quality gates and deployment claims require review |
| Load testing | PLANNED | No benchmark evidence found |

## 3. Database Status

SQLAlchemy defines users, candidates, interviews, questions, turns, evidence, and competency
state. Alembic migrations are present and the documented local head is `5f2c7d9a1b10`.
The application falls back to `storage/ai_interviewer.db` unless a PostgreSQL URL is configured.
PostgreSQL execution was not verified in this environment. Foreign keys are present on several
normalized tables, but interview ownership is not represented by a user relationship and there
is no tenant model.

The legacy `storage/interviews.db` file is preserved and is not opened by the current runtime.
The repository reports 24 legacy rows, with no repeatable import having been implemented.

## 4. API Status

Implemented endpoints include auth, candidate CRUD, interview start, answer, read, list, and
report export. Responses are plain dictionaries rather than a consistently separated response
schema. Interview start and answer are publicly callable at the route layer. Answer requests
contain only `answer`; retries therefore create new generated turn records and repeat all
orchestration side effects.

## 5. Security Status

JWT verification and password hashing are present. Registration defaults to `admin`, accepts a
caller-supplied role, and startup creates `admin@example.com` with `secret123`, which is unsafe
outside a controlled development environment. Interview resources lack authentication,
ownership checks, and role checks. Candidate email uniqueness is global, with no tenant boundary.

## 6. Agent Status

The agent stores a mutable dictionary containing the current question, history, keyword-derived
skills, and status. It selects among a few fixed questions using substring checks. There is no
durable graph checkpoint, conditional graph routing, evidence-gap model, question fingerprint,
or model/prompt version metadata.

## 7. RAG Status

`RAGQuestionService` performs exact position lookup against an in-memory dictionary and mutates
the selected list while adding skill-specific questions. It is not a retrieval pipeline and has
no independently measurable retrieval quality.

## 8. Evaluation Status

`EvaluationService` identifies a small set of keywords, converts keyword-derived skill scores to
an overall score, and computes confidence from evidence count. It does not distinguish evidence,
score, and evidence sufficiency confidence, and it has no rubric, model, prompt, or evaluation
versioning.

## 9. Voice Status

The voice package contains adapter-shaped placeholders only. STT does not call a provider,
produce partial/final transcript events, or handle corrections. TTS returns UTF-8 text bytes,
not synthesized audio. No VAD, interruption, latency telemetry, or provider fallback is wired.

## 10. Testing Status

The repository has unit tests for the API, auth/candidate flow, interview core, production
pipeline, and database integrity. Existing tests exercise the happy path and migration/schema
constraints. There are no focused tests for authorization, client turn idempotency, concurrent
duplicates, rollback of a complete answer operation, crash recovery, real graph routing, RAG
metrics, or voice lifecycle behavior.

## 11. CI/CD Status

The repository contains `.github/workflows/test.yml`, but CI quality gates and deployment
configuration must be treated as unverified until the workflow is inspected and run. Docker
Compose declares PostgreSQL, Redis, Celery, monitoring, and frontend services; declaration is not
evidence that those services are required, healthy, or tested by the application.

## 12. Technical Debt and Contradictions

- README and architecture documents describe LangGraph, real RAG, voice, browser automation,
  and objective evaluation more strongly than the source supports.
- `state_json` and normalized records can diverge because normalized writes are inferred from
  snapshot changes in the compatibility store.
- The current turn persistence associates a generated turn with the next question rather than a
  client-submitted logical answer ID.
- Database defaults make every user an admin, and startup bootstrapping uses known credentials.
- Heavy infrastructure dependencies are declared without a demonstrated runtime integration.
- PostgreSQL and Python 3.14 dependency compatibility remain unverified.

## 13. Recommended Execution Order

1. Phase 2: introduce focused repositories and an application service with one transaction
   boundary for interview operations, while preserving the public API shape.
2. Phase 3: add client-supplied turn IDs, unique constraints, duplicate replay behavior,
   lifecycle states, optimistic/concurrency handling, and recovery tests.
3. Phase 4: require authentication, enforce resource ownership and role policy, remove unsafe
   public admin registration/bootstrap behavior, and add authorization tests.
4. Phase 5: replace the keyword core with a real stateful adaptive graph only after the durable
   service contract is stable.
5. Phase 6: add structured evidence and rubric-based evaluation with explicit version metadata.
6. Phase 7: build independently testable ingestion and hybrid retrieval, then measure whether a
   reranker is worth its cost.
7. Phase 8 onward: add voice, resilience, observability, background work, and deployment only
   where an executable requirement and test justify them.

## Audit Conclusion

The database foundation is usable as a development baseline, but the platform is an alpha MVP.
The next concrete implementation slice is Phase 2: repository/application-service boundaries
and transaction tests. No later-phase capability should be labeled implemented until its runtime
path and focused tests exist.