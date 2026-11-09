# Task 2: PostgreSQL and Multi-Worker Reliability Verification

Branch `test/task2-postgres-multi-worker`, based on `main` at `eceaf92`.

## Summary

- The Alembic history is linear (19 revisions) with a single head, `g20_campaign_interview_linkage`. An empty PostgreSQL 16 database upgrades to head without errors.
- PostgreSQL exposed three defects that SQLite hides. All three are fixed on this branch, and none needed a schema change:
  - **D1:** candidates could not submit a CV.
  - **D2:** communication drafts could not be created.
  - **D3:** concurrent workers recorded a lease expiry twice.
- On PostgreSQL with the `main` code, 43 of 302 unit tests fail. With this branch, only the same 8 pre-existing failures remain that also fail on SQLite.
- New multi-process tests run 2 to 6 real worker processes against PostgreSQL and count real side effects (LLM calls, e-mail deliveries) in an autocommit ledger:
  - No scenario produced a duplicate reservation, a duplicate committed outcome or a false success state.
  - One expected duplicate was measured: a second LLM call when a lease expires during the call, because leases are not renewed.
- This is **not** a production-readiness claim. See [Not verified](#not-verified).

## Environment

| Item | Value |
|---|---|
| PostgreSQL | 16.15, official Ubuntu 24.04 package, disposable cluster on `127.0.0.1` only, `trust` auth, synthetic data |
| Isolation level | PostgreSQL default, `READ COMMITTED` |
| Python / SQLAlchemy / psycopg2 / Alembic | 3.12 / 2.0.23 / 2.9.9 / 1.16.5 |
| Workers | separate processes started with `multiprocessing` `spawn`, each with its own engine and pool, released together by a barrier |
| Leases in lease tests | `CAMPAIGN_WORKER_LEASE_SECONDS=10` (the minimum the settings allow) |
| Redis | not needed by `tests/postgres` (no app lifespan). The existing unit suites were run with an in-memory Redis stand-in because no Redis server was available locally |
| LLM / e-mail | `LLM_PROVIDER=none`. Counting test doubles are injected; no network calls |

No production or shared database was touched. No data was backfilled.

## Migrations

`tests/postgres/test_migrations_postgres.py`:

1. **History.** `ScriptDirectory` reports exactly one head, `g20_campaign_interview_linkage`, 19 revisions and no merge points.
2. **Empty to head.** The test creates a fresh database `<DATABASE_URL db>_migration`, runs `python -m alembic upgrade head` in a subprocess, checks `alembic current` and drops the database.
3. **Schema compared with the models** (`alembic.autogenerate.compare_metadata`). There are 20 differences in total:
   - **None is structural.** No table or column is missing or extra.
   - **Campaign tables:** three plain, non-unique lookup indexes are declared in the models (`index=True`) but are not created by any migration:
     - `ix_recruitment_campaign_executions_organization_id`
     - `ix_recruitment_campaigns_organization_id`
     - `ix_recruitment_campaigns_owner_user_id`

     They affect query performance only. No uniqueness guarantee depends on them, and they are **not** added here because this task allows no schema change. The test pins this exact list, so any new drift on campaign tables fails it.
   - **The other 17** are naming or duplicate-index differences on older tables (`access_audit`, `interview_*`, `organizations`, `rag_documents`, `users`). Uniqueness is equivalent: for example, `users.email` has both `users_email_key` and `ix_users_email`, and `interview_assessments` has `ix_assessment_unique_turn_competency` instead of the model's name.

## Defects found and fixed

### D1: CV submission always fails on PostgreSQL

| | |
|---|---|
| Where | `services/candidate_application_service.py`, `submit_cv` |
| Symptom | Every `POST .../applications` returned `409 "An application already exists for this campaign"`, even on an empty database |
| Cause | `RecruitmentApplicationRecord` and `CandidateCVDocumentRecord` are added in one flush. The models declare no `relationship()`, so the SQLAlchemy unit of work does not order the two INSERTs, and the CV document is inserted first. PostgreSQL rejects it (`fk_candidate_cv_documents_org_application`). The `IntegrityError` handler then reports it as a duplicate application |
| Why SQLite passes | SQLite does not enforce foreign keys unless `PRAGMA foreign_keys=ON`, and the project never sets it |
| Fix | `session.flush()` after adding the application, before adding the CV document |
| Impact on `main` | 38 of 79 campaign-area tests failed on PostgreSQL, because every flow that starts with a CV upload failed |

### D2: Communication draft creation always fails on PostgreSQL

| | |
|---|---|
| Where | `services/candidate_communication_service.py`, `create_draft` |
| Cause | Same as D1. The `communication_draft_created` event row was inserted before the communication row (`fk_candidate_communication_events_org_communication`). The existing `flush()` came after the event was added, too late to fix the order |
| Fix | Flush the new communication before recording its first transition |
| Impact on `main` | 19 further tests failed after D1 was fixed |

### D3: Concurrent lease-expiry sweeps record the recovery twice

| | |
|---|---|
| Where | `CandidateCommunicationService.discover_eligible`, the sweep for expired `sending` rows |
| Symptom | Two workers sweeping at the same moment both wrote `communication_lease_expired` events and audit rows for one communication |
| Cause | The sweep read expired rows with a plain `SELECT` and wrote them back through ORM attributes, an unconditional `UPDATE ... WHERE id = ?`. Under `READ COMMITTED` the second worker waits for the first, then overwrites the row and appends its own event. The final state was still correct, and nothing was resent, but the audit trail claimed two recoveries |
| Fix | A conditional `UPDATE` (`status = 'sending'`, same `lease_token`, lease still expired), with the event and audit recorded only when `rowcount == 1`. This is the pattern the application and availability sweeps already use |
| Reproduction | With the old sweep (and D1/D2 fixed), `test_worker_killed_between_send_and_commit_is_never_resent_or_reported_sent` failed 3 out of 3 runs with `assert 2 == 1` on the event count |

## Multi-worker tests

`tests/postgres/test_multi_worker_reliability.py`. "Side effects" are rows in `reliability_probe.side_effects`. Each row is written through an autocommit connection *before* the worker's own transaction commits, so it survives a crash.

| Scenario | Workers | Verified |
|---|---|---|
| Execution creation and job-design step claim | 4 × `dispatch_once` | 1 execution, 1 step, **1 LLM call**, attempt 1, 1 `campaign_execution_started` audit |
| Execution lease expires during the LLM call | stale worker + reclaimer after the lease expires | **2 LLM calls** (expected without lease renewal). Only the reclaimer's output is committed. `plan_version` is bumped exactly once. The stale worker's completion is rejected by the token check |
| CV screening claim | 4 × `process_dispatched` | 1 screening row, attempt 1, `needs_review` |
| Communication dispatch | 4 × `process_dispatched` | **1 delivery**, 1 attempt-started and 1 attempt-completed event, `sent` |
| Worker killed after the provider accepted, before commit | crash (`os._exit`), then 2 sweepers before and 2 after the lease expires | While the lease is live, nobody touches the row. After expiry the row becomes `needs_attention` / `delivery_status=unknown`, `sent_at` stays null, **still 1 delivery**, 1 expiry event |
| Delivery slower than the lease | slow worker + sweeper after the lease expires | 1 delivery. The slow worker cannot record `sent`; the row stays `needs_attention` / `unknown` for human reconciliation |
| Retry after `retryable_not_accepted` | 1 failing attempt, then 4 racing retries | **2 deliveries in total**, attempt 2, `sent`, exactly one worker reports `sent` |
| Reservation confirmation | 3 schedules for the same interviewer and slot, each confirmed by 2 workers (6 processes) | 1 `confirmed_internal`, **2 reservation rows** (candidate + interviewer), 0 overlapping reservations, 4 × 409, no 503 or errors |
| Invitation handoff | 4 × `advance_application_once` | 1 invitation draft and 1 draft event (idempotency key + unique constraint) |
| Two real polling loops | 2 × `RecruitmentCampaignWorker.run()` for 8 s, 4 communications + 1 LLM campaign in the backlog | Every communication delivered exactly once, 1 LLM call, execution attempt 1 |
| Worker restart | loop killed on its first delivery, then a replacement loop running past the lease | Rows the dead worker claimed end as `needs_attention` with at most 1 delivery. Every other row is `sent` with exactly 1 delivery |

### Results

| Suite | SQLite, `main` | SQLite, branch | PostgreSQL, `main` | PostgreSQL, branch |
|---|---|---|---|---|
| `tests/unit` | 294 passed / 8 failed | 294 passed / 8 failed | 259 passed / **43 failed** | 294 passed / 8 failed |
| `tests/postgres` | skipped | 13 skipped | 4 passed / **9 failed** | **13 passed** |

- `tests/postgres` passed in 3 consecutive runs, each on a freshly migrated database (about 2 minutes per run).
- The 8 failures that remain on both databases were present before this branch:
  - 3 in `test_campaign_lifecycle.py`, fixed separately in PR #8;
  - 5 RAG tests that fail on Linux because the corpus hash is computed over CRLF line endings.

## PostgreSQL compared with SQLite

| Aspect | SQLite (current CI) | PostgreSQL | Consequence |
|---|---|---|---|
| Foreign keys | Not enforced (no `PRAGMA foreign_keys=ON`) | Enforced | INSERT-order defects D1 and D2 are invisible in CI |
| Write concurrency | One writer per database file, so writers never interleave | Row-level locks. Under `READ COMMITTED` a blocked conditional `UPDATE` re-evaluates its `WHERE` on the committed row | Lost-update and double-transition races (D3) only appear on PostgreSQL |
| `SELECT ... FOR UPDATE` | Ignored | Takes row locks | The 9 `with_for_update()` calls in `candidate_invitation_service.py` are effectively untested in CI |
| Existing "concurrency" tests | Threads in one process sharing one pool | This branch uses separate processes | Thread-based tests cannot show cross-process behaviour |
| Timestamps | Stored naive. The code compensates with `.replace(tzinfo=UTC)` | `timestamptz`, timezone-aware | Lease comparisons behaved the same in all tests |
| Partial unique indexes | Supported (`sqlite_where`) | Supported (`postgresql_where`) | Same behaviour observed |

## Re-running

You need any disposable PostgreSQL 12+ that you can reach on loopback. Two safety guards apply:
- The database name must start with `test_`.
- `tests/conftest.py` refuses PostgreSQL unless `RUN_POSTGRES_TESTS=1` is set and the host is `127.0.0.1`, `localhost` or `::1`.

```bash
# Example with Docker; any disposable local server works.
docker run --rm -d --name task2-pg -p 127.0.0.1:55432:5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16
psql -h 127.0.0.1 -p 55432 -U postgres -c "CREATE DATABASE test_task2;"

export DATABASE_URL=postgresql+psycopg2://postgres@127.0.0.1:55432/test_task2
export ENVIRONMENT=test RUN_POSTGRES_TESTS=1 LLM_PROVIDER=none

python -m alembic upgrade head          # the multi-worker tests expect a migrated database
python -m pytest tests/postgres -q --timeout=300
```

The migration test also needs `CREATE DATABASE` rights, because it creates and drops `test_task2_migration`. To run the existing suites on PostgreSQL, use the same environment and add a reachable `REDIS_URL`, since those tests start the app lifespan:

```bash
python -m pytest tests/unit -q --timeout=300
```

Without `RUN_POSTGRES_TESTS`, the normal SQLite runs are unchanged, and `tests/postgres` is skipped.

## Changes

| File | Change |
|---|---|
| `services/candidate_application_service.py` | D1: flush the application before inserting its CV document |
| `services/candidate_communication_service.py` | D2: flush the draft before its first event. D3: conditional lease-expiry transition |
| `tests/conftest.py` | Allows an explicitly opted-in, loopback-only `test_*` PostgreSQL database. SQLite rules are unchanged |
| `tests/postgres/` | Migration tests, multi-worker tests and the spawned-worker harness |

No migration, model or schema change.

## Not verified

- **Lease renewal does not exist.** A provider call longer than `CAMPAIGN_WORKER_LEASE_SECONDS` (default 120 s) leads to:
  - a second LLM call for job design (measured: 2 calls);
  - for e-mail, an accepted message recorded as `unknown`, which needs a person to reconcile. It is never resent.

  A heartbeat that extends the lease while the call runs would avoid both. It is not implemented here.
- **Worker death between a side effect and its commit.** This is verified for e-mail delivery. The other side effects mostly commit together with their state in one transaction (screening, invitation drafts, availability requests, schedules), so a crash before commit leaves no trace. That relies on PostgreSQL atomicity and was not crash-injected for each flow. LLM job design is simply repeated after a crash; its cost is not bounded beyond `max_attempts`.
- **Recovery after `needs_attention`.** The explicit reconcile API (`reconcile`) was not exercised concurrently.
- **Worker against a human at the same moment.** An example is approval revoked between the execution-completion `UPDATE` and the campaign write in `_run`; the campaign row is not locked there. The worst case found by reading the code is `pending_review` overwriting a revocation, which still requires a new approval. Not tested.
- **Lock waits and isolation levels.** The scheduling mutex is one global row for all tenants. Throughput and lock-wait bounds (`lock_timeout`, `statement_timeout`) were not measured. Only `READ COMMITTED` was tested; `REPEATABLE READ` or `SERIALIZABLE` would turn mutex waits into serialization failures.
- **Infrastructure:**
  - clock skew between worker hosts (leases are compared with each worker's own clock);
  - PgBouncer or other connection poolers;
  - real Redis;
  - real SMTP.
- **CI still runs SQLite only.** Adding a PostgreSQL job, and enabling `PRAGMA foreign_keys=ON` for the SQLite test database, would catch D1/D2-type defects automatically. Both are left for the owner to decide.
- **Missing indexes.** The three campaign indexes above are not created.

## Open questions

1. Should a PostgreSQL job be added to `.github/workflows/test.yml`? The current guard deliberately rejects non-SQLite URLs.
2. Should the SQLite test engine enable foreign keys? It may surface more insert-order issues outside the campaign area, although none appeared in `tests/unit` on PostgreSQL.
3. Should lease renewal be added for long LLM and SMTP calls, or should the lease simply be set above the provider timeout?
4. Should the three missing indexes get a migration?
