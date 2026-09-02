# Phase 3/4 Hardening Report

**Date:** 2026-09-02  
**Status:** PARTIAL, locally verified

## 1. Implemented

- Added explicit `received`, `processing`, `completed`, `failed_retryable`, and `failed_final` turn states with validated transitions.
- Persisted the turn claim before answer processing, so an interrupted process leaves a durable turn record.
- Added retryable failure marking and stale-processing reclaim using a configurable lease.
- Kept `(interview_id, turn_id)` as the database-authoritative uniqueness boundary and reconciled insert conflicts with a fresh read.
- Added `owner_user_id` to interviews, with an Alembic migration and repository-level authorized queries.
- Required authenticated users on interview endpoints and denied recruiter answer mutations.
- Changed self-registration to always create the least-privileged `candidate` role, ignoring submitted roles.
- Disabled known default-admin bootstrap by default; development bootstrap now requires explicit environment settings.

## 2. Concurrency Model

Turn claiming and processing use separate transactions. The first request claims the unique
client turn ID. A concurrent request that loses the database uniqueness race reads the existing
interview and returns without creating another turn. The database constraint remains the final
duplicate protection. This is at-least-once request delivery with idempotent durable effects,
not exactly-once execution.

## 3. Idempotency Guarantees

Sequential retries of a completed `(interview_id, turn_id)` replay the stored interview snapshot.
Concurrent duplicate tests confirm one turn and one unique follow-up question are persisted.
The current response for a request arriving while another request is still processing is the
last committed interview snapshot; clients should retry to obtain the completed result.

## 4. Turn State Machine

```text
RECEIVED -> PROCESSING -> COMPLETED
                     \-> FAILED_RETRYABLE -> PROCESSING
                     \-> FAILED_FINAL
```

Completed and final-failed turns are terminal. Invalid transitions and duplicate completion are
rejected by the transition validator.

## 5. Crash Recovery Behavior

If processing fails after the durable claim, the turn is marked `failed_retryable`. A later
submission with the same client turn ID can reclaim it. A stale `processing` turn is reclaimed
after `TURN_PROCESSING_LEASE_SECONDS`. No distributed recovery worker has been introduced.

## 6. Authorization and Ownership Model

JWT claims are resolved against the current user record. Interview creation records the creating
user as owner. Candidate users can access only owned interviews. Admins and recruiters can read
all interviews; recruiters are read-only for answer submission. The repository applies these
filters in SQL queries rather than fetching arbitrary resources and checking afterward.

## 7. RBAC and Admin Bootstrap Security

Public registration cannot select a role and always creates a candidate. Admin/interviewer/
recruiter assignment remains an administrative operation outside the public registration route.
Startup creates no account by default. Development bootstrap is opt-in and requires explicit
`BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`, and `ENABLE_DEV_ADMIN_BOOTSTRAP` settings.

## 8. Tests Executed

```text
python -m pytest
25 passed, 52 warnings

python -m alembic downgrade -1
python -m alembic upgrade head
python -m alembic current
7a1f_owner_and_turn_states (head)
```

Focused coverage includes concurrent duplicate submissions, stale processing retry, durable
failure marking, valid/invalid transitions, unauthenticated access, cross-user access, recruiter
write denial, and registration privilege escalation.

## 9. Tests Not Executed

- PostgreSQL migration/CRUD/downgrade was not executed because Docker/PostgreSQL is unavailable
  in this environment.
- Multi-process concurrency under PostgreSQL was not benchmarked.
- Load testing and security scanning were not run.

## 10. Known Limitations

- The current SQLite runtime does not enable foreign-key enforcement on its application engine;
  the integrity fixture does. PostgreSQL behavior remains unverified.
- Interviewer assignment and candidate-to-user relationships are not modeled yet. Ownership is
  creator ownership, not a complete candidate/interviewer organization relationship.
- The recovery policy reprocesses the same answer after a stale claim; it does not checkpoint
  individual AI side effects or provide a distributed worker.
- Normalized evidence and competency tables remain unused by the MVP evaluator.
- The agent, evaluator, RAG, voice, browser, Redis, and worker features remain mocked or planned.