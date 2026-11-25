# Milestone 12 — Execution Checklist

Branch: `main` @ `4d2e7ad` (= `origin/main`). Previous hardening work is uncommitted and preserved.
Local infrastructure: real PostgreSQL 16.2 (embedded `pgserver` binaries, loopback only, disposable `test_*` databases). Real Redis is **not** available locally (Windows, no Docker); real-Redis verification happens only in CI.

Status legend: [ ] todo · [x] done · [~] partial · [!] blocked

## Phase A — PostgreSQL + Redis CI
- [x] Verify prior-report hypotheses against code
- [x] CI: lint job, offline unit job (unchanged behavior)
- [~] CI: PostgreSQL 16 service job (defined + validated locally; **not yet run on GitHub**) (empty-DB migration, single head, `tests/postgres`, campaign/tenant/scheduling/communication/availability/interview/retrieval suites)
- [~] CI: Redis 7 integration job (defined; **not yet run on GitHub**) (locks: contention, expiry, release on exception, unavailable, reconnect)
- [~] Real-Redis lock tests written (skip unless `REDIS_INTEGRATION_URL`; fakeredis refused) — **not run against real Redis** (CI only)
- [x] Run PostgreSQL suites locally on real PostgreSQL 16

## Phase B — Worker lease renewal
- [x] Token-conditional renewal for campaign executions and communication sends
- [x] Bounded renewal thread, stops on completion/shutdown, safe on failure
- [x] PostgreSQL multi-worker tests (long task, renewal, no reclaim, lost ownership, crash expiry, retry → needs_attention, shutdown)

## Phase C — RAG retrieval quality
- [x] Competency normalization (rubric labels → corpus ids, `general`, NULL semantics consistent SQL/in-memory)
- [x] Conservative token normalization (plurals; protect technical terms)
- [x] Job role-family aliases (controlled)
- [x] Persist retrieved chunk ids per turn
- [x] Fixed evaluation probes + before/after metrics; tenant isolation preserved

## Phase D — Candidate data deletion
- [x] Inventory candidate data locations and dependencies
- [x] Authorized, tenant-checked, idempotent, audited deletion with partial-failure reporting
- [!] Retention policy decisions documented — **blocked on product-owner/legal approval**

## Phase E — SMTP ambiguous outcomes
- [x] Classify failures by SMTP phase; ambiguous → unknown/needs_attention, never blind retry
- [x] Tests for each failure phase

## Phase F — Observability, rate limiting, docs
- [x] Request/correlation IDs, structured log context, no PII
- [x] Rate limiting on expensive endpoints (existing limiter)
- [x] Correct README/ARCHITECTURE/Makefile/compose claims

## Phase G — AI/voice readiness
- [~] AssemblyAI client against documented API (MockTransport only; **live verification pending**)
- [x] Provider review notes

## Final
- [x] Full offline suite, ruff, PostgreSQL suite, migration checks, CI YAML validation
- [x] `docs/MILESTONE_12_REPORT.md`, `docs/PRODUCTION_READINESS.md`, update status docs

Result details: `docs/MILESTONE_12_REPORT.md`.
