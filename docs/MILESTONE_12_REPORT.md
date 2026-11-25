# Milestone 12 Report — Production Verification & Reliability

## 1. Executive summary
- **PostgreSQL**: real PostgreSQL 16.2 was run locally (embedded `pgserver` binaries, loopback, disposable `test_*` databases). Migrations from empty reach the single head; all `tests/postgres` suites and the business workflow suites pass on it. CI now has `postgres-integration` and `redis-integration` jobs that fail if suites are skipped — **these CI jobs have not run yet** (nothing pushed).
- **Lease renewal**: implemented for campaign executions and e-mail sends; verified on PostgreSQL with multi-process tests.
- **RAG**: five measured retrieval defects fixed; probe hit-rate@5 0.1 → 1.0, zero tenant-isolation violations; still lexical only.
- **Candidate erasure**: authorized, tenant-scoped, idempotent, audited; retention policy is a product/legal blocker.
- **SMTP**: the previously reported ambiguity window did not exist (report corrected); pre-DATA sender refusals are now classified precisely.
- **Observability**: request IDs, safe log context, rate limits on answer / CV upload / login.
- **Voice**: AssemblyAI client rebuilt on the documented upload → transcript → poll API; MockTransport only.
- Found and fixed a latent logging defect: in-process Alembic runs disabled all existing application loggers.
- **Not production-ready**: real Redis, CI, external providers and deployment remain unverified (section 15).

## 2. Branch and HEAD
`main` @ `4d2e7ad44d11e798404716a6d7dbd2908e13bc73` (= `origin/main`, 0/0). No branch created or switched. Nothing committed or pushed. All earlier uncommitted work (audit coverage, secret redaction, tenant retrieval, STT protocol fixes) was preserved.

## 3. Files modified and why
| File | Why |
|---|---|
| `.github/workflows/test.yml` | lint list extended; `campaign-workflow` and `security-regression` groups; new `postgres-integration` (PG 16 + Redis 7) and `redis-integration` jobs with skip guards |
| `tools/ci_assert_no_skips.py` (new) | fails integration jobs on skipped or missing tests |
| `tests/redis/` (new) | real-Redis interview-turn lock suite; refuses fakeredis |
| `services/lease_renewal.py` (new), `services/recruitment_campaign_orchestrator.py`, `services/candidate_communication_service.py`, `config/settings.py` | bounded token-conditional lease renewal during LLM job design and SMTP sends |
| `tests/postgres/test_lease_renewal_postgres.py` (new), `tests/postgres/worker_harness.py`, `tests/postgres/test_multi_worker_reliability.py` | renewal tests; harness option to simulate a worker whose renewals stop; two stale-worker tests now state that explicitly |
| `tests/unit/test_lease_renewal.py` (new) | helper behavior offline |
| `rag/service.py`, `agents/interview_agent/graph/__init__.py`, `services/interview_service.py`, `rag/retrieval_probes.py` (new), `rag/corpus/retrieval_probes_v1.json` (new), `tests/unit/test_rag_phase_c_quality.py` (new) | retrieval quality, per-turn source traceability, evaluation probes |
| `services/candidate_data_deletion.py` (new), `services/candidate_cv_storage.py`, `apps/api/v1/routes/candidate.py`, `docs/CANDIDATE_DATA_DELETION.md` (new), `tests/unit/test_candidate_data_deletion.py` (new) | candidate personal-data erasure |
| `services/email_provider.py`, `tests/unit/test_email_delivery_phases.py` (new) | SMTP phase classification |
| `apps/api/middleware/request_context.py` (new), `apps/api/middleware/rate_limit.py` (new), `utils/logger.py`, `infrastructure/rate_limiter.py`, `apps/api/main.py`, `apps/api/v1/routes/{interview,recruitment_campaigns,auth}.py`, `tests/unit/test_request_context_and_rate_limits.py` (new) | request IDs, log context, rate limits |
| `voice/real_providers.py`, `voice/factory.py`, `tests/unit/test_stt_assemblyai_offline.py` | AssemblyAI documented flow |
| `alembic/env.py` | `fileConfig(..., disable_existing_loggers=False)` |
| `tests/unit/test_interview_scheduling.py`, `tests/unit/test_candidate_data_deletion.py`, `tests/unit/test_tenant_interview_retrieval.py` | test isolation/order fixes found on PostgreSQL (event query scoped to own schedules; audit assertions order-independent; stale comment) |
| `README.md`, `ARCHITECTURE.md`, `Makefile` | remove/flag unsupported claims (pgvector, ColBERT, Whisper, Celery, Prometheus/Sentry, Kubernetes, exactly-once); `run-worker` runs the real worker; broken targets fail explicitly |
| `docs/*` | this report, `PRODUCTION_READINESS.md`, `MILESTONE_12_EXECUTION.md`, hardening report correction, continuation status |

`apps/api/v1/routes/account_linking.py` and frontend code were not modified.

## 4. Features implemented
See sections 6–10. Each states implemented / tested / environment / real-infrastructure / limitations.

## 5. Security changes
- Candidate erasure endpoint (`DELETE /api/v1/candidates/{id}/personal-data`): org_admin only; other org → 404; audited with IDs only; under the already-audited support-access prefix.
- Rate limits on login (by client IP), interview answers and CV uploads (by actor), 429 + `Retry-After`; auth/tenant errors still precede 429.
- Request IDs are validated (`[A-Za-z0-9._-]{1,128}`), never echo unsafe input; log context accepts only a fixed list of ID fields (no PII).
- Retrieval changes keep organization/campaign filtering in the data layer; source ids recorded per turn are restricted to the interview's own tenant or approved global chunks.
- Tested offline and on PostgreSQL 16; not verified in a deployed environment.

## 6. Database and worker reliability
**Lease renewal** (`services/lease_renewal.py`): one daemon thread per claimed item, interval `lease/3` (configurable, clamped ≤ `lease/2`); renewal is `UPDATE … WHERE id AND status AND lease_token = mine AND lease_expires_at > now` so it never revives an expired or foreign lease; stops on ownership loss or after 3 consecutive failures; joined on task exit (including exceptions and worker shutdown). Final commits remain token-fenced. App clock (`datetime.now(UTC)`) is used consistently with existing claim logic; cross-host clock skew must stay well below `lease/3`.
- Implemented: yes. Tested: unit (6) + PostgreSQL multi-process (5 new). Environment: real PostgreSQL 16.2 local. Real infra: PostgreSQL yes (local), CI not yet run.
- Limitations: not exactly-once; a renewing worker that hangs inside the provider call holds the item until the provider timeout; clock skew assumption.

| Required scenario | Test |
|---|---|
| task longer than lease; renewed; second worker cannot reclaim | `test_execution_longer_than_lease_is_renewed_and_cannot_be_reclaimed`, `test_delivery_longer_than_lease_is_renewed_sent_once_and_not_swept` |
| worker losing ownership cannot commit | `test_expired_execution_lease_is_reclaimed_and_stale_worker_cannot_commit`, `test_delivery_slower_than_lease_is_not_resent_and_stale_result_is_discarded` (renewal disabled in the stale worker) |
| renewal never revives expired/foreign lease | `test_renewal_never_revives_an_expired_or_foreign_lease` |
| crashed worker's lease expires | `test_worker_killed_between_send_and_commit_is_never_resent_or_reported_sent`, `test_worker_loop_restart_recovers_without_resending` |
| retry limit → needs_attention | `test_expired_lease_at_attempt_limit_moves_to_needs_attention` |
| shutdown during active task | `test_worker_shutdown_during_long_task_finishes_and_stops_renewal` |

**Migrations**: single head `g20_campaign_interview_linkage`; empty → head on PostgreSQL 16 verified; no schema change in this milestone.

## 7. RAG before/after
Probe set `rag/corpus/retrieval_probes_v1.json` (10 positive, 4 negative, two synthetic tenants incl. a campaign-scoped chunk). Run: `python -m rag.retrieval_probes --summary [--service-path <HEAD copy>]`, re-run independently by the lead.

| Metric (k=5, lexical) | HEAD | Now |
|---|---|---|
| hit-rate@5 | 0.10 | **1.00** |
| zero-result rate | 0.90 | **0.00** |
| relevant-doc recall@5 | 0.10 | **0.925** |
| tenant-isolation violations | 0 | **0** |
| negative job-scope hits | 0 | **0** |

Changes: NULL competency = generic in both SQL and in-memory paths; fixed rubric-label → corpus-id table, `general` = no filter; conservative plural stripping with protected technical terms; seniority stripping + controlled role-family aliases (unrelated roles do not match); per-turn `retrieved_sources` (ids + scope only). Existing `rag.run_evaluation` benchmark unchanged (recall@5 0.893, MRR 0.929).
Limitations: still lexical; rows ingested earlier keep raw competency labels until re-upserted; small alias/protected tables; `retrieved_sources` are visible in candidate-facing interview state (decision needed).

## 8. Candidate data deletion
`services/candidate_data_deletion.py`, contract and inventory in `docs/CANDIDATE_DATA_DELETION.md`. Redact-in-place (composite FKs), CV files deleted and re-checked, membership revoked, pending work cancelled, interview state stripped and closed; numeric scores and hiring-outcome values kept (target counts). Order: one DB transaction (check in-flight work, redact, audit `pending`) → delete files → audit `completed` or `partial` (HTTP 202, `retryable`). Blocked (409, audited) while CV processing, a send, an unfinished schedule or an interview turn is in flight. Idempotent.
- Tested: 8 tests, SQLite and PostgreSQL 16. Real infra: PostgreSQL local only.
- Policy BLOCKERS (product/legal): retention period/auto-purge, which scores/outcomes/audit rows are retained, legal hold, platform-support erasure, global user-account deletion. No retention duration was invented.
- Limitations: report exports, backups, log aggregation and provider-side copies are out of scope; a concurrent write racing the check can survive until the erasure is repeated.

## 9. Email delivery
Classification by SMTP phase: connect/EHLO/STARTTLS/login failures → `retryable_not_accepted` (definitely not sent); auth rejection → `definitively_rejected`; `SMTPSenderRefused` (new) → rejected (5xx) / retryable (4xx); recipients refused → by code; `SMTPDataError` → by code; any other failure during `send_message` (timeout/disconnect before or after DATA) → `unknown` → `needs_attention`, admin reconciliation, never auto-retried; failure during QUIT after acceptance → `accepted`. Stable `Message-ID <communication_id@host>`. Not exactly-once.
- Tested with stubbed SMTP (22 new + existing); no real SMTP server.

## 10. Observability
Request-ID middleware (outermost), contextvar log context with `bind_log_context` for worker IDs, one "request completed" line per request (method, route template, status, duration; no raw paths, bodies, headers or IPs). Rate limits: fixed 60 s window, Redis (atomic INCR+EXPIRE transaction) when `REDIS_URL` is set, in-memory only when it is not; fail-open with a warning and a 30 s backend back-off (`RATE_LIMIT_FAIL_OPEN=false` → 503). Health/readiness unchanged (`/health/ready` checks DB + Redis).
- Not done (no deployment target): metrics backend, tracing, error reporting, circuit breaker wiring, LLM token/cost metrics. The worker is not yet bound to the log context helper.

## 11. Test commands and results
Environment unless stated: Python 3.11.15 venv (scratchpad), `ENVIRONMENT=test ENABLE_DEV_ADMIN_BOOTSTRAP=false LLM_PROVIDER=none EMBEDDING_PROVIDER=none`, fresh migrated DB per run.

| # | Command | Environment | Result |
|---|---|---|---|
| 1 | `pytest tests/unit -m "not live" --timeout=300` | SQLite + fakeredis stand-in | **504 passed, 0 failed**, 4 deselected (live). Baseline before milestone: 365 |
| 2 | `alembic heads` / `alembic upgrade head` / `alembic current` | PostgreSQL 16.2, fresh `test_m12_final_pg` | 1 head; empty → `g20_campaign_interview_linkage (head)` |
| 3 | `pytest tests/postgres --timeout=300` + `tools/ci_assert_no_skips.py` | PostgreSQL 16.2 | **18 passed, 0 skipped** (13 existing + 5 lease renewal) |
| 4 | Business workflow + new suites (19 files, same list as CI `postgres-integration` plus deletion/RAG/rate-limit) + skip guard | PostgreSQL 16.2, fresh `test_m12_final_wf` | **233 passed, 0 skipped** |
| 5 | `pytest tests/redis` | no real Redis available | 6 skipped without `REDIS_INTEGRATION_URL`; against fakeredis all fail with "not a real Redis server" (guard works). **No real-Redis result.** |
| 6 | `python -m rag.retrieval_probes --summary` (now vs HEAD `rag/service.py`) | SQLite, lexical | hit-rate@5 1.0 vs 0.1 (section 7) |
| 7 | `ruff check` on 25 modified + 18 new Python files vs HEAD copies (same `pyproject.toml`) | — | no new findings (154 now vs 162 at HEAD in modified files; new files clean) |
| 8 | `yaml.safe_load(.github/workflows/test.yml)` + path existence | — | valid; 5 jobs; all referenced test/tool paths exist. `actionlint` not available; workflow not executed on GitHub |
| 9 | Focused suites per phase (lease renewal unit 6, SMTP phases 22 + provider/communications, deletion 8, RAG 59, request context/rate limit 22, STT 40, voice suites) | SQLite (+ PostgreSQL where listed) | all passed in final runs above |

Notes:
- First full offline run on the final code had 2 failures (`test_request_context_and_rate_limits`) caused by test order: `test_candidate_invitation_migration` runs Alembic in-process and `fileConfig` disabled every existing logger. Fixed in `alembic/env.py`; reverting the fix reproduces the 2 failures.
- `test_overlapping_reservations_are_serialized_and_idempotent` counted every `reservation_conflict` event in the database; it failed when the shared DB contained other conflict events (reproduced 4/5 on reruns). Pre-existing test-isolation defect; the query is now scoped to the test's own schedules (passes repeatedly on a dirty DB).
- `test_competing_confirmations_commit_one_reservation_set` (PostgreSQL) failed twice while several heavy suites ran concurrently on this machine (also "paging file too small" errors) and passed 4/4 when run without contention. Treated as load-sensitive; it should be watched in CI.
- Two deletion tests assumed audit rows were ordered by `created_at`; on PostgreSQL equal timestamps (coarse clock) are unordered. Assertions were made order-independent; the service was not changed.

## 12. PostgreSQL verification
Verified locally on real PostgreSQL 16.2 (x86_64 mingw build via `pgserver`), loopback only, disposable `test_*` databases dropped/recreated per run. **Not yet verified in CI** (job defined, not run). Production/shared databases were never touched.

## 13. Redis verification
**Not verified against real Redis in this milestone.** Local runs used a fakeredis TCP server, which is only a stand-in for offline suites; the new `tests/redis` suite refuses it (`INFO server` required) and skips without `REDIS_INTEGRATION_URL`. Real verification happens in the `redis-integration` CI job (Redis 7 service), which has not run.

## 14. External provider verification
None. OpenAI, AssemblyAI, ElevenLabs and SMTP were exercised only through `httpx.MockTransport` / stubs. AssemblyAI request flow follows the public docs (files/upload, transcripts/submit, transcripts/get); exact `language_code` form and `speech_models` values remain unconfirmed.

## 15. Remaining blockers
1. CI jobs not yet run (needs a push the user must authorize).
2. Real Redis verification (CI).
3. Retention/erasure policy decisions (product/legal).
4. Live provider verification with spend caps (OpenAI, AssemblyAI, ElevenLabs, SMTP).
5. Voice: blocking call in async handler, non-streamed TTS, cancel does not clear provider audio, `retrieved_sources` candidate visibility decision.
6. Observability backend and deployment target.
7. Possible credential rotation (keys printed by the old offline config).

## 16. Next recommended milestone
**CI green + live-provider smoke**: push to a PR, get `postgres-integration` and `redis-integration` green (fix anything real-Redis exposes), then run the existing spend-capped `live-provider` job plus a new authorized AssemblyAI/ElevenLabs smoke. In parallel, obtain the retention-policy decision and fix the voice event-loop blocking (`asyncio.to_thread` for `submit_answer`).
