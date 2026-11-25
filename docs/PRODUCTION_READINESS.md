# Production Readiness

As of 2026-11-24, `main` @ `4d2e7ad` plus uncommitted Milestone 12 changes. **Not production-ready.** See "Blockers".

## Verification levels

| Level | Meaning | Where it happened |
|---|---|---|
| SQLite offline | `pytest tests/unit -m "not live"` on a disposable SQLite file, fake Redis TCP stand-in, `LLM_PROVIDER=none`, `EMBEDDING_PROVIDER=none` | Developer machine |
| PostgreSQL integration | Real PostgreSQL 16.2 (embedded `pgserver` binaries, loopback, disposable `test_*` DBs): empty→head migration, `tests/postgres`, business workflow suites | Developer machine; CI job `postgres-integration` defined but **not yet run** |
| Real Redis | `tests/redis` + `tests/isolated` against a real Redis 7 | **Only in CI job `redis-integration`, not yet run.** No real Redis on the dev machine; fakeredis is refused by the suite |
| External providers | OpenAI, AssemblyAI, ElevenLabs, SMTP | **None verified.** Mock transports / stubs only |
| Production deployment | Real environment, TLS, secrets, scaling | **None** |

## Capability status

| Capability | Status | Evidence |
|---|---|---|
| Campaign lifecycle (objective → plan → approval → intake → screening → communication → availability → scheduling → interview → outcome) | IMPLEMENTED_AND_VERIFIED (SQLite + PostgreSQL 16) | campaign suites pass on both; CI `campaign-workflow` group |
| Tenant isolation (API, services, retrieval) | IMPLEMENTED_AND_VERIFIED (SQLite + PostgreSQL 16) | `test_resource_authorization`, `test_tenant_interview_retrieval`, `test_rag_phase_c_quality` |
| Platform support-access audit | IMPLEMENTED_AND_VERIFIED (offline + PG) | `test_support_access_audit_coverage` (route guard) |
| Migrations (single head, empty → head) | IMPLEMENTED_AND_VERIFIED on PostgreSQL 16 | `alembic heads`, `tests/postgres/test_migrations_postgres.py` |
| Worker claims, fencing, retry, needs_attention | IMPLEMENTED_AND_VERIFIED on PostgreSQL 16 (multi-process) | `tests/postgres/test_multi_worker_reliability.py` |
| Worker lease renewal | IMPLEMENTED_AND_VERIFIED on PostgreSQL 16 | `services/lease_renewal.py`; `tests/postgres/test_lease_renewal_postgres.py`; `tests/unit/test_lease_renewal.py` |
| Interview turn Redis lock | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | Lock primitive previously verified on real Redis (PHASE_11_5B/C); new service-level suite `tests/redis` runs only in CI |
| Email delivery outcome classification | IMPLEMENTED_AND_VERIFIED (stubbed SMTP) | `test_email_delivery_phases.py`, `test_email_provider.py`; no real SMTP server tested |
| Candidate personal-data erasure | IMPLEMENTED_AND_VERIFIED (SQLite + PG), policy BLOCKED | `services/candidate_data_deletion.py`, `docs/CANDIDATE_DATA_DELETION.md` |
| CV retention period / auto-purge | REQUIRES_PRODUCT_OWNER_DECISION; dry-run foundation + legal hold IMPLEMENTED_AND_VERIFIED | `services/retention_policy.py`, `docs/RETENTION_POLICY_DECISIONS.md` |
| RAG retrieval (lexical) | IMPLEMENTED_AND_VERIFIED (offline probes) | hit-rate@5 0.1 → 1.0 on `rag/corpus/retrieval_probes_v1.json` |
| Semantic retrieval (OpenAI embeddings) | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | opt-in live job only |
| Vector database / cross-encoder reranker | NOT_IMPLEMENTED | — |
| LLM provider (OpenAI) | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | MockTransport tests; live CI job opt-in |
| Anthropic provider | DOCUMENTED_BUT_NOT_IMPLEMENTED | settings only |
| STT (AssemblyAI upload → transcript → poll) | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | MockTransport only; live API unverified |
| TTS (ElevenLabs) | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | not streamed; 4xx not normalized |
| Voice pipeline default | MOCK_OR_DETERMINISTIC_ONLY | fake STT/TTS/VAD |
| Voice off-loop answers, TTS streaming, cancellation | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED (offline) | Milestone 13 Phase D |
| Candidate API privacy (allowlist views) | IMPLEMENTED_AND_VERIFIED (offline + PG) | `docs/INTERVIEW_API_CONTRACT.md` |
| End-to-end recruitment journey | IMPLEMENTED_AND_VERIFIED (SQLite + PG16, deterministic providers) | `tests/unit/test_e2e_recruitment_journey.py` |
| Request IDs + structured log context | IMPLEMENTED_AND_VERIFIED (offline) | `apps/api/middleware/request_context.py`, `utils/logger.py` |
| Rate limiting (answer, CV upload, login) | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | offline + recording fake; real Redis path not run |
| Secret redaction | IMPLEMENTED_AND_VERIFIED (offline) | `test_secret_redaction.py` |
| Metrics (Prometheus text, own registry) | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED (offline) | `utils/metrics.py`, `docs/OBSERVABILITY.md` |
| Tracing, alerting, error reporting | NOT_IMPLEMENTED | — |
| Circuit breaker in request path | NOT_IMPLEMENTED | class exists, unused |
| Kubernetes / production deployment | NOT_IMPLEMENTED | no manifests |

## Delivery guarantees
- Campaign executions and communications: at-least-once processing, lease-token fenced commits, lease renewal while active. **Not exactly-once.**
- Email: an SMTP failure while sending is `unknown` → `needs_attention` and requires explicit admin reconciliation; it is never retried automatically. A message accepted by the server whose worker then lost its lease is also left `unknown`.

## Blockers before production
1. Run the new CI jobs (`postgres-integration`, `redis-integration`) green on GitHub Actions.
2. Product/legal: CV and interview data retention period, legal hold, what scores/outcomes/audit rows are retained, platform-support erasure policy (`docs/CANDIDATE_DATA_DELETION.md`).
3. Live verification (authorized, spend-capped) of OpenAI, AssemblyAI, ElevenLabs and the SMTP relay.
4. Credential rotation decision for keys that may have been printed by the old offline indexing config.
5. Voice: blocking `submit_answer` inside the async WebSocket handler; TTS not streamed; `utterance.cancel` keeps provider audio until close.
6. Observability backend (metrics, error reporting, alerting) and deployment target are undecided.
7. ~~Candidate-visible `retrieved_sources`~~ — hidden from candidates in Milestone 13; candidate report export content still needs a product decision.
8. ~~Report exports not covered by erasure~~ — fixed in Milestone 13.

Milestone 13 status: `docs/MILESTONE_13_REPORT.md`.
