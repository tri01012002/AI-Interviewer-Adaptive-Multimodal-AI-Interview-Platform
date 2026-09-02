# PHASE 1 STEP 1.5 REPORT

## 1. What Changed

- Added Alembic to `requirements.txt`.
- Configured Alembic to load `DATABASE_URL` from the environment/settings.
- Removed runtime calls to `init_db()` from application startup and `get_db_session()`.
- Replaced raw `sqlite3` implementation in `InterviewStore` with SQLAlchemy-backed compatibility persistence.
- Added normalized question and turn writes while retaining the existing API response contract.
- Added a migration enforcing unique question sequence numbers per interview.
- Added isolated database integrity tests.
- Restored the local database to the Alembic head revision.
- Added `DATABASE_ARCHITECTURE.md`.

## 2. Files Modified or Created

Modified:

- `services/database.py`
- `services/interview_store.py`
- `apps/api/main.py`
- `requirements.txt`

Created:

- `alembic.ini`
- `alembic/env.py`
- `alembic/versions/304be4757bda_001_initial_schema.py`
- `alembic/versions/5f2c7d9a1b10_unique_question_sequences.py`
- `tests/unit/test_database_integrity.py`
- `DATABASE_ARCHITECTURE.md`
- `PHASE_1_STEP_1_5_REPORT.md`

`check_db.py` was also created during the earlier verification work.

## 3. Database Architecture Before and After

Before:

```text
Interview API -> raw sqlite3 InterviewStore -> storage/interviews.db -> JSON state
Other stores -> SQLAlchemy -> storage/ai_interviewer.db
```

After:

```text
FastAPI interview API -> temporary SQLAlchemy compatibility store -> SQLAlchemy -> configured DATABASE_URL
```

The normalized schema contains `users`, `candidates`, `interviews`, `interview_questions`, `interview_turns`, `interview_evidence`, and `interview_competency_state`.

## 4. How Raw SQLite Was Retired

The runtime `InterviewStore` no longer imports or uses `sqlite3`, and it no longer opens `storage/interviews.db`. Its existing method names remain temporarily so the API contract does not change before STEP 2.

The legacy file was inspected and preserved. It contained 24 interview rows at the time of verification. No deletion or automatic historical migration was performed.

## 5. `state_json` Compatibility Strategy

`interviews.state_json` remains in the schema. New writes persist the core interview record and normalized question/turn records, while retaining a JSON snapshot for fields required by the current API and report exporter. The compatibility layer still reads that snapshot for fields not yet represented in normalized tables.

Therefore, `state_json` is transitional compatibility data, not yet fully retired from reads. Removing that read path belongs after the normalized application contract is complete.

## 6. PostgreSQL Verification

PostgreSQL verification was **blocked**. Docker is not installed in the environment (`docker` was not recognized), so no PostgreSQL container could be started. The models and migration were not claimed PostgreSQL-verified.

The project uses `psycopg2-binary`, and the Docker configuration contains a PostgreSQL service, but those facts are not execution evidence.

## 7. Migration Results

Commands executed:

```powershell
python -m alembic upgrade head
python -m alembic current
python -m alembic downgrade -1
python -m alembic upgrade head
python -m alembic current
```

Results:

- Clean SQLite upgrade succeeded.
- Downgrade from `5f2c7d9a1b10` to `304be4757bda` succeeded.
- Re-upgrade succeeded.
- Final revision: `5f2c7d9a1b10 (head)`.
- Final schema inspection found all seven application tables plus `alembic_version`.

## 8. Test Commands

```powershell
python -m pytest tests/unit/test_database_integrity.py
python -m pytest
```

## 9. Exact Test Results

Focused integrity tests:

```text
5 passed, 2 warnings in 0.40s
```

Full suite:

```text
12 passed, 21 warnings in 0.89s
```

The warnings were existing/dependency deprecations, including the unknown `asyncio_mode` pytest option, Pydantic configuration deprecation, Starlette/httpx deprecation, FastAPI event deprecation, and `datetime.utcnow()` deprecation in logging.

Dependency verification:

```powershell
python -m pip install --dry-run -r requirements.txt
```

Failed on this Python 3.14 environment while resolving `psycopg2-binary==2.9.9`: no compatible wheel was available, so pip attempted a source build and reported that `pg_config` was missing. This is an environment/package compatibility limitation and remains unresolved here.

## 10. Remaining Issues

- PostgreSQL upgrade, CRUD, downgrade, and re-upgrade were not executable because Docker is unavailable.
- A fresh dependency dry-run is blocked on Python 3.14 by `psycopg2-binary==2.9.9` requiring a source build without `pg_config`.
- Historical rows in `storage/interviews.db` were preserved but not migrated into the normalized database.
- `state_json` remains read for compatibility and is not yet legacy-write-only.
- `InterviewStore` is still a temporary compatibility abstraction; the formal repository layer is intentionally deferred to STEP 2.
- The existing development bootstrap still creates `admin@example.com` through application startup; no new production credentials were added by this step.
- The local API tests use the configured development database rather than a fully isolated per-test database.

## 11. STEP 1 Readiness

The database foundation is usable by the current application and is ready for the next implementation step, subject to the explicit PostgreSQL and legacy-data limitations above. This does not establish production readiness.

**Next step:** STEP 2 - Repository Layer + Transaction Boundaries.

**STEP 1.5 STATUS: PARTIAL**
