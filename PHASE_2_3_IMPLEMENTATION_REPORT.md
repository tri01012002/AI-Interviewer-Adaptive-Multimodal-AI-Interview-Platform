# Phase 2 and Early Phase 3 Implementation Report

**Date:** 2026-09-02  
**Status:** PARTIAL, tested locally

## Implemented

- Added `InterviewRepository`, `QuestionRepository`, and `TurnRepository` with session-in/session-out persistence methods.
- Added `InterviewService` as the application boundary for start, read, list, and answer operations.
- Moved interview route orchestration out of FastAPI handlers.
- Kept `InterviewStore` as a compatibility facade for callers that still import it.
- Required a client-supplied `turn_id` on answer requests.
- Added durable `processing` and `completed` turn states within the answer transaction.
- Added completed-turn replay by `(interview_id, turn_id)` so normal retries do not re-run evaluation or append history.
- Required bearer authentication for interview start, answer, read, list, and report endpoints.

## Verification

```text
python -m pytest
13 passed, 29 warnings
```

The touched Python modules report no editor diagnostics.

## Deliberate Limitations

- The current transaction does not yet persist normalized evidence or competency-state updates.
- Two simultaneous first-time requests with the same client turn ID still need database-specific
  conflict handling and concurrency tests; the uniqueness constraint prevents duplicates, but a
  retry path around the resulting integrity error is not implemented.
- A process crash during `processing` is durable but not yet recovered by a worker or explicit
  retry state transition.
- Authentication is not authorization: roles are not enforced and the schema has no user-to-
  candidate ownership relationship, so cross-resource isolation remains unfinished.
- The legacy snapshot remains part of the read contract.

## Next Step

Complete Phase 3 with conflict-safe duplicate processing, retryable/final failure states, and
recovery tests, then implement Phase 4 ownership and role policy before replacing the keyword
agent.