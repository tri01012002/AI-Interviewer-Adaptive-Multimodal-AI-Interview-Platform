# Milestone 13 Report — Release Candidate

> P14 note (2026-11-25): this is the historical P13 report. P13 was pushed
> as `71a3314edd7962ed0f6629291b403c5e6ef2ff37`, not `4d2e7ad`. Evidence below
> predates P14 Docker/context hardening and must not be cited as P14 CI or
> production verification.
>
> P14.1 follow-up: GitHub Actions run
> [45571865439](https://github.com/tri01012002/AI-Interviewer-Adaptive-Multimodal-AI-Interview-Platform/actions/runs/45571865439)
> ran on that P13 baseline and failed overall on the RAG digest matrix and a
> campaign ordering assertion. Its PostgreSQL (18 tests), Redis 7 (21 tests),
> and quality jobs passed. P14.1 local offline verification is documented in
> `docs/P14_1_STABILIZATION_REPORT.md`; it does not verify the current
> uncommitted changes in GitHub Actions.

**Verdict: Release Candidate for offline + PostgreSQL scope, NOT production-ready.** Real Redis, GitHub CI execution, live providers, image build and deployment are unverified; retention policy requires product-owner decisions.

## 1. Git state
- Branch `main`, HEAD `4d2e7ad` (= `origin/main`). No branch created/switched, nothing committed or pushed.
- `git status`: 46 modified, 45 untracked paths. `git diff --stat` (tracked files): 46 files, +2601 / −475.
- `apps/api/v1/routes/account_linking.py` and frontend untouched. All Milestone 11–12 work preserved.

## 2. Phase results

| Phase | Status | Evidence |
|---|---|---|
| A. Real Redis | **BLOCKED** | No Redis 7, Docker or WSL distro on this machine; installing one needs owner authorization. Suites ready: `tests/redis/` (lock suite + new rate-limit/key-isolation suite, 11 tests; fakeredis is refused). Commands: `docs/DEPLOYMENT_RUNBOOK.md` (disposable `redis:7-alpine` on 127.0.0.1). |
| B. CI validation | IMPLEMENTED, TESTED_OFFLINE (not run on GitHub) | Health-checked PG16/Redis7 services, loopback `test_*` DB guard, empty→head migration, single-head check, skip guards; new `tools/ci_check_test_coverage.py` (lint job) fails CI if any `tests/unit` file is not run — it found 7 files (incl. all Milestone 12 tests) that CI never ran; all now registered. |
| C. Deletion incl. report exports | IMPLEMENTED_AND_VERIFIED (SQLite + PG16 local) | Exports now in `STORAGE_PATH/reports/<org>/<interview>/`, legacy `report_*` scanned safely; erasure deletes + re-checks; 202/retry semantics; 410 for exports after erasure. Inventory in `docs/CANDIDATE_DATA_DELETION.md`. Bug found by Phase J and fixed: erasure wrongly blocked by a future confirmed slot whose interview had already completed/been decided. |
| D. Voice | IMPLEMENTED, TESTED_OFFLINE (MOCK_OR_DETERMINISTIC_ONLY providers) | Bounded off-loop answer runner (no unbounded queue, per-session ordering, idempotent), progressive TTS (`stream_events`, ElevenLabs `client.stream`), `cancel_utterance` + idempotent close, clean disconnects. 34 new tests. Real ElevenLabs/AssemblyAI: BLOCKED (no authorization). |
| E. Source visibility | IMPLEMENTED_AND_VERIFIED (offline + PG) | Candidate interview responses are a server-side allowlist (no `retrieved_sources`, ids, evaluation internals); staff views unchanged. `docs/INTERVIEW_API_CONTRACT.md`. |
| F. Retention | Foundation IMPLEMENTED_AND_VERIFIED (offline + PG); policy **REQUIRES_PRODUCT_OWNER_DECISION** | `services/retention_policy.py`: config-only rules, dry-run default, execution needs `RETENTION_EXECUTION_ENABLED` + rules + `--execute`; legal hold (migration `g21_candidate_legal_hold`, org_admin `PUT/DELETE /api/v1/candidates/{id}/legal-hold`) blocks manual and automated erasure. No schedule enabled. `docs/RETENTION_POLICY_DECISIONS.md`. |
| G. Observability | IMPLEMENTED, TESTED_OFFLINE | `utils/metrics.py` (own registry, label allowlists, no ids/emails), wired to HTTP, rate limits, OpenAI (latency, tokens from `usage`, cost only if prices configured), RAG, worker items/lease renewals/heartbeat, voice STT/TTS, readiness; `/metrics` token-protected, 404 in production without token. `docs/OBSERVABILITY.md`. No tracing/alerting/dashboards. |
| H. Provider smoke tests | IMPLEMENTED; live runs **BLOCKED** (no authorization) | `tests/live/test_provider_smoke.py` (OpenAI, AssemblyAI, ElevenLabs, SMTP), double opt-in, no retries, fixed tiny inputs, key-leak assertions; all skip by default. `docs/PROVIDER_SMOKE_RUNBOOK.md`. |
| I. Deployment | Runbook IMPLEMENTED; local process checks VERIFIED (Windows, embedded PG16, fakeredis stand-in) | `docs/DEPLOYMENT_RUNBOOK.md`; API startup/readiness/graceful shutdown, production `SECRET_KEY` refusal, worker run/status/pause/resume, `tools/backup_restore_check.py` (pg_dump/pg_restore round trip, 34 tables equal). Fixed: `.env.example` failed to load; container healthchecks used tools absent from the image; added `.dockerignore` (images would have contained `.env` and `storage/` with CVs). Image build/compose: BLOCKED (no Docker). |
| J. End-to-end | IMPLEMENTED_AND_VERIFIED (SQLite + PG16 local) | `tests/unit/test_e2e_recruitment_journey.py`: all 20 journey steps through real API/services/worker + 8 negative tests (cross-tenant, unapproved campaign, unauthorized hire, invalid schedule, duplicate answer, worker crash, Redis down → 503, partial deletion → retry). Uses fake Redis offline, deterministic providers. |

## 3. Tests (exact commands, actual results)
Env: Python 3.11.15 venv; `ENVIRONMENT=test ENABLE_DEV_ADMIN_BOOTSTRAP=false LLM_PROVIDER=none EMBEDDING_PROVIDER=none`; fresh migrated DB per run.

| Command | Environment | Result |
|---|---|---|
| `pytest tests/unit -m "not live" --timeout=300` | SQLite file + fakeredis stand-in | **599 passed, 0 failed**, 4 deselected (M12 end: 504) |
| `alembic heads` / `upgrade head` / `current` | PostgreSQL 16.2 (embedded `pgserver`, loopback, `test_m13_pg`) | 1 head; empty → `g21_candidate_legal_hold (head)` |
| `pytest tests/postgres` + `tools/ci_assert_no_skips.py` | PostgreSQL 16.2 | **18 passed, 0 skipped** — first run 17 passed / 1 failed: `test_migration_history_has_single_expected_head` still expected 19 revisions after the new `g21` migration; count corrected, re-run passed |
| CI `postgres-integration` workflow file list (parsed from the YAML) + skip guard | PostgreSQL 16.2 | **153 passed, 0 skipped** (17 files incl. end-to-end); then deletion/export-erasure/retention/visibility suites added to the CI list and run on a fresh PG DB: **28 passed, 0 skipped** (with migration tests) |
| `pytest tests/redis` | no real Redis | 11 skipped without `REDIS_INTEGRATION_URL`; against fakeredis the 9 server tests fail with "not a real Redis server" (guard works). **No real-Redis result.** |
| `pytest tests/live` | — | 4 skipped (opt-in) |
| `ruff check` 38 modified + 36 new `.py` vs HEAD copies | — | no new findings (188 vs 207 at HEAD in modified files; new files clean) |
| `tools/ci_check_test_coverage.py`; YAML parse + referenced paths | — | all `tests/unit` files referenced; YAML valid; 0 missing paths; `actionlint` unavailable |

Order/isolation issues found and fixed during final runs: the end-to-end test assumed its CV was the next worker item (failed only in the full suite) → targeted CV step + bounded worker drain; reproduced and verified after polluting suites (37/37).

## 4. Verification status summary
- **PostgreSQL:** P13 report records local PostgreSQL 16.2 verification; the later P13 baseline Actions run also passed 18 PostgreSQL tests. Neither verifies the uncommitted P14.1 changes.
- **Redis:** P13 baseline Actions run passed 21 tests against Redis 7. P14.1 local supported-version verification remains BLOCKED; the available disposable local Redis is 3.0.504.
- **Voice:** TESTED_OFFLINE only; providers MOCK_OR_DETERMINISTIC_ONLY.
- **Providers (OpenAI, AssemblyAI, ElevenLabs, SMTP):** BLOCKED — no authorization; smoke tests ready.
- **Deployment:** local process checks only; no image build, no server.

## 5. Product-owner decisions (REQUIRES_PRODUCT_OWNER_DECISION)
1. Retention periods and enabled rules; hired-candidate handling; which scores/outcomes/audit rows survive erasure; legal-hold governance (who, expiry, queued erasure requests); platform-support erasure; global user-account deletion; partial (CV-only) erasure; scheduling (`docs/RETENTION_POLICY_DECISIONS.md`).
2. Candidate report export currently returns scores, evidence and AI recommendation to candidates — keep, reduce or block?
3. Candidates no longer see evaluations/scores in interview views — confirm.
4. Whether campaign interviews may start before the scheduled slot (currently allowed).

## 6. Deployment blockers
P14.1 GitHub Actions verification; supported-version Redis verification on P14.1 changes; authorized live provider smoke tests; container image build and non-root runtime verification; production compose/orchestration (migration job, worker env); shared/object storage for multi-instance file storage; secrets management; metrics scraping/alerting backend; credential rotation decision for keys printed by the old offline config.

## 7. Owner actions to unblock
1. **Real Redis locally** (authorize one): `docker run --rm -p 127.0.0.1:6379:6379 redis:7-alpine`, then `REDIS_INTEGRATION_URL=redis://127.0.0.1:6379/1 python -m pytest tests/redis tests/isolated/test_distributed_lock_verification.py -ra`.
2. **GitHub CI**: run the workflow on the P14.1 revision after the owner authorizes publishing it; do not treat the P13 baseline run as evidence for P14.1.
3. **Live smoke tests**: follow `docs/PROVIDER_SMOKE_RUNBOOK.md` with spend caps and test accounts.

## 8. Next milestone
Run the three owner actions above, fix whatever real Redis/CI/providers expose, build and scan the images (non-root), and take the retention decisions — then re-assess production readiness.
