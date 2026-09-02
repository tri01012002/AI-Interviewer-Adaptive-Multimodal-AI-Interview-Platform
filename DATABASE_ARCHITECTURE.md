# Database Architecture

## Architecture

The active application path is:

```text
FastAPI -> SQLAlchemy session -> configured database
```

Interview routes retain the existing `InterviewStore` method names temporarily, but the implementation now uses SQLAlchemy and writes `InterviewRecord` plus normalized question and turn records. This compatibility layer is not the STEP 2 repository layer.

The legacy `storage/interviews.db` SQLite file is no longer opened by the application runtime. It is retained for manual migration or archival handling.

## SQLAlchemy Configuration

`services/database.py` reads `DATABASE_URL` through application settings. PostgreSQL URLs are used directly. The local fallback is `storage/ai_interviewer.db` for development and tests.

Sessions are created per operation with `SessionLocal()`. `SessionLocal.begin()` is used for atomic interview writes and rolls back when an exception escapes the context. No global shared session is used.

## PostgreSQL Configuration

Set `DATABASE_URL` to a PostgreSQL URL, for example:

```text
postgresql://user:password@localhost:5432/ai_interviewer
```

The project currently uses `psycopg2-binary`. PostgreSQL execution was not verified in this environment because Docker is unavailable; this remains an explicit verification item.

## Alembic Workflow

Schema changes are managed by Alembic. Application startup does not call `Base.metadata.create_all()`.

```powershell
python -m alembic upgrade head
python -m alembic current
python -m alembic downgrade -1
python -m alembic upgrade head
```

The current head is `5f2c7d9a1b10`.

## Schema Overview

- `users`: authentication records.
- `candidates`: candidate records.
- `interviews`: core interview record; `state_json` remains as a compatibility snapshot.
- `interview_questions`: normalized question history.
- `interview_turns`: normalized answer-turn records with unique interview/turn and interview/sequence indexes.
- `interview_evidence`: minimal future evidence storage.
- `interview_competency_state`: minimal future competency state storage.

The normalized tables contain foreign keys for interview-to-question, interview-to-turn, evidence-to-interview, evidence-to-turn, and competency-state-to-interview relationships. Organizations and tenant tables are intentionally not present because the current application does not implement multi-tenancy.

## Timestamps

Application timestamps use UTC-aware `datetime` values. Database columns use SQLAlchemy `DateTime(timezone=True)`. SQLite does not preserve timezone metadata in storage, so PostgreSQL verification is still required before making stronger cross-database claims.

## Legacy `state_json` Transition

`state_json` has not been deleted. New interview writes create normalized interview, question, and turn records and retain the JSON snapshot for compatibility with the current response shape and report exporter. The current compatibility layer still reads the snapshot to reconstruct fields not yet represented by the normalized schema.

The safe next transition is:

1. Add complete normalized fields needed by the application.
2. Verify normalized reads against existing snapshots.
3. Stop reading `state_json`.
4. Remove the column in a later migration.

No automatic migration of the 24 rows in the legacy `storage/interviews.db` file has been performed. That file was inspected and preserved.

## Local Development and Tests

Run migrations before starting the application:

```powershell
python -m alembic upgrade head
```

Integrity tests use an isolated in-memory SQLite database with foreign-key enforcement enabled. They do not use development data. The existing API tests use the configured local database and should be run against disposable development data.

## Migration Procedure

1. Back up the target database.
2. Set `DATABASE_URL` for the target environment.
3. Run `python -m alembic upgrade head`.
4. Confirm `python -m alembic current` reports the expected revision.
5. Run application and integrity tests.

## Rollback Procedure

For the tested local database, `python -m alembic downgrade -1` followed by `python -m alembic upgrade head` succeeds. Review the migration and take a backup before applying a downgrade to a database containing business data. The initial migration rollback drops the schema and must not be used as a data-preserving production rollback.
