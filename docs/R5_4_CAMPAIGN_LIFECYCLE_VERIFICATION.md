# R5.4 Campaign Lifecycle Verification

**Overall status:** PASSED offline. All 8 lifecycle tests pass. Two code defects are fixed, each with a regression test, and two test-setup defects are corrected without changing any assertion. Real Redis, PostgreSQL and truly concurrent workers are not verified.

## Scope

R5.4 requires the following. A campaign resumes after a worker restart. Each application is processed independently. Approvals and policy are respected. Dispatch stops once the headcount target is reached.

The acceptance suite is `tests/unit/test_campaign_lifecycle.py`. Code was changed only where a test or code review proved a defect. No assertion was loosened.

Unchanged:
- approval and policy rules;
- `apps/api/v1/routes/account_linking.py`;
- the frontend.

## Environment

- Python 3.12 project `.venv`.
- `ENVIRONMENT=test`, `LLM_PROVIDER=none`, `EMBEDDING_PROVIDER=none`, `OPENAI_API_KEY` empty. No live AI request was made.
- Email: `offline_simulation`. The lifecycle tests also replace `smtplib.SMTP` and `socket.create_connection` with `pytest.fail`, so any real send or network connection would fail the test.
- Database: an isolated SQLite file per run (`storage/test_*.db`), deleted afterwards. No production database was used.
- Redis: no Redis server was available on the verification machine. A local in-memory stand-in was loaded as a pytest plugin; it is not part of this change. It patches `redis.Redis.from_url` and implements `get`, `set` (`nx`/`xx`/`ex`/`px`), `delete`, `incr`, `expire`, `ttl`, `ping`, and the two Lua scripts used by `infrastructure/distributed_lock.py`.

## Initial state

With the Redis stand-in, `tests/unit/test_campaign_lifecycle.py` gave **5 passed, 3 failed**:

| Test | Requirement | Result | Cause |
|---|---|---|---|
| `test_version_change_and_stale_claim_commit_fencing` | stale claim / version fencing | failed | F1, test setup |
| `test_target_reached_prevents_dispatch_and_interview_starts` | stop at headcount target | failed | F2, code |
| `test_expired_deadline_and_terminal_outcomes_do_not_busy_loop` | no busy loop after deadline | failed | F3, test setup |

`tests/unit/` overall: 294 passed, 8 failed, 4 skipped.

## Defects

### F2: replaying interview start after a hiring outcome returns 409 (code)

- **Failure:**
  1. The candidate completes the interview.
  2. An org admin records `hired`.
  3. The candidate calls `POST /interview/campaigns/{campaign_id}/applications/{application_id}/start` again.

  The response is `409 {"detail": "Application already has a recorded hiring outcome"}`. Before the outcome, the same call returns `200` with the existing interview state. The lifecycle test requires `200` with the same `interview_id`.
- **Cause:** `_campaign_interview_target` checked for a recorded outcome before `start_campaign_interview` looked up the existing interview, so the replay branch was unreachable.
- **Fix:** `services/interview_service.py`.
  - The outcome check moved into `_has_recorded_outcome`; the query is unchanged.
  - It now runs after the existing-interview replay (lines 216–226) and before the target check.
  - A recorded outcome therefore blocks creating a new interview, as before, but no longer blocks returning the existing one.

| Situation | Before | After |
|---|---|---|
| Interview exists, outcome recorded, start replayed | 409 | 200, same `interview_id`, nothing created |
| No interview, outcome recorded | 409 | 409 (unchanged) |
| No interview, target achieved | 409 | 409 (unchanged) |

### F4: one failing application blocks advancement of all others (code)

- **Failure:** `RecruitmentCampaignOrchestrator.advance_eligible_applications` calls `advance_application_once` for each eligible application without isolation. If one handoff raises, the exception aborts the loop for the whole tick. For example, `create_draft` rejects a stored candidate address with `422 Candidate email address is invalid`.
  - The failing application keeps its `updated_at`, so it stays first in the ordered query on every tick.
  - Later applications never advance.
  - The query is not tenant-scoped, so one tenant's bad row also blocks other tenants.
- **Reproduction on the previous code:** two reviewed applications, the first with a synthetic invalid address. Three `RecruitmentCampaignWorker().dispatch_once()` calls each raised `HTTPException`, and the healthy application got 0 invitation drafts. When this row was left in the shared test database, 6 otherwise passing lifecycle tests for other tenants also failed.
- **Why the existing test missed it:** `test_failure_on_one_application_does_not_block_unrelated_applications` marks the failing application `needs_attention` up front, so it is filtered out before the loop.
- **Fix:** `services/recruitment_campaign_orchestrator.py`, lines 494–512.
  - Each `advance_application_once` call catches `HTTPException`, logs `application_id` and `status_code`, and continues.
  - No exception detail or address is logged.
  - The failing application keeps its state, is not marked successful, and is retried on later ticks.
  - Database errors still propagate to the worker's existing error handling.

### F1: invalid screening fixture in the stale-claim test (test)

- **Failure:** the test built `CandidateScreeningResult(application_id=..., assessments=[], confidence=1.0, decision="advance")`. The model is `extra="forbid"` and requires `rubric_version` and `rubric_snapshot`, so construction raised `ValidationError` before `_persist_screening` was reached.
- **Fix:** build `CandidateScreeningResult(rubric_version="stale-claim-rubric", rubric_snapshot=[], assessments=[])`. The assertions are unchanged: a non-matching lease token returns `None` and the application stays `pending`.
- **Result:** the test passes on the previous service code, so the fencing in `_persist_screening` was already correct.

### F3: outcome recorded on an unreviewed application (test)

- **Failure:** the test recorded `rejected` directly after CV upload. `record_hiring_outcome` intentionally returns `409 Application screening must be reviewed before recording an outcome`. This human-in-the-loop rule was kept.
- **Fix:** the test now screens the application (`CandidateApplicationService.process_dispatched`) and reviews it (`POST /recruitment-applications/{id}/review`) before recording the outcome. This is the same sequence `_prepare_candidate_with_worker` uses. The assertions are unchanged.
- **Result:** the test passes on the previous service code.

## Regression tests

New file: `tests/unit/test_campaign_lifecycle_regressions.py`. It reuses the lifecycle helpers.

| Test | Covers | Previous code | This change |
|---|---|---|---|
| `test_interview_start_replay_after_outcome_returns_existing_interview` | F2. The target is 2 so the headcount check cannot mask the result. A hired candidate's replay returns 200 with the same `interview_id`. A rejected candidate without an interview still gets 409 with the recorded-outcome message. The campaign has exactly 1 interview | failed | passed |
| `test_failing_application_handoff_does_not_block_other_applications` | F4. A synthetic invalid address on the first application. After 3 ticks: 0 drafts for the broken application, 1 for the healthy one, broken application still `needs_review`/`reviewed`. The original address is restored in `finally` so the shared test database stays clean | failed | passed |
| `test_stale_screening_token_cannot_overwrite_live_application_claim` | Stale-claim fencing while another worker holds a live lease. Persisting with a stale token returns `None`; status, `lease_token` and `review_status` are unchanged | passed | passed |

## Results

Command pattern (Redis stand-in loaded with `-p`):

```bash
DATABASE_URL="sqlite:///./storage/test_<name>.db" ENVIRONMENT=test \
LLM_PROVIDER=none EMBEDDING_PROVIDER=none OPENAI_API_KEY= \
.venv/bin/python -m pytest -p <redis-stand-in-plugin> <tests> -q -p no:cacheprovider --timeout=300
```

| Run | Code | Result |
|---|---|---|
| `test_campaign_lifecycle.py` as originally written | previous | 5 passed, 3 failed (F1, F2, F3) |
| `test_campaign_lifecycle.py` + regressions | previous service code | 8 passed, 3 failed (F2 lifecycle test, F2 regression, F4 regression) |
| `test_campaign_lifecycle.py` + regressions | this change | **11 passed** |
| `tests/unit/` (run twice, identical) | this change | **300 passed, 5 failed, 4 skipped** |

The 5 remaining failures are pre-existing and unrelated:
- `test_rag_evaluation.py`:
  - `test_retrieval_benchmark_claims_semantic_only_when_dense_search_runs`
  - `test_retrieval_benchmark_does_not_claim_semantics_after_provider_failures`
- `test_rag_semantic_retrieval.py`:
  - `test_verified_global_visibility_requires_explicit_policy_and_approval`
  - `test_demo_corpus_is_versioned_and_contains_requested_document_types`
  - `test_question_retrieval_reads_persisted_vectors_and_respects_job_scope`

The expected hash for `rag/corpus/demo_v1.json` matches the file with CRLF line endings (`46706d61…`). A Linux checkout has LF endings (`731add0b…`), and the repository has no `.gitattributes`.

The 4 skips are opt-in live-provider and real-Redis tests. They were not enabled.

`ruff check` reports no new findings in the changed files. The four pre-existing findings were left as they are, because they are outside the R5.4 scope:
- C901 on `RecruitmentCampaignOrchestrator._run`;
- three unused imports in `test_campaign_lifecycle.py`.

## Requirement status

| Requirement | Evidence | Status |
|---|---|---|
| Missing/invalid automation policy → waiting state with reason and handler | `test_missing_and_invalid_automation_policy_produces_actionable_waiting_states` | passed, unchanged |
| Version change blocks stale-claim commits | `test_version_change_and_stale_claim_commit_fencing`, `test_stale_screening_token_cannot_overwrite_live_application_claim` | passed after F1 |
| Replay / restart creates no duplicate communication, availability request, interview or outcome | `test_replaying_and_worker_restart_are_strictly_idempotent`, F2 regression (1 interview) | passed |
| One application's error does not stop others | `test_failure_on_one_application_does_not_block_unrelated_applications`, F4 regression | passed after F4 |
| Target reached stops dispatch and unnecessary interview starts | `test_target_reached_prevents_dispatch_and_interview_starts`, F2 regression | passed after F2 |
| Expired deadline / terminal state does not busy-loop | `test_expired_deadline_and_terminal_outcomes_do_not_busy_loop` | passed after F3 |
| Only approved, qualifying outcomes count | `test_non_qualifying_outcomes_do_not_count_toward_headcount_target` | passed, unchanged |
| Orchestrator / communication / availability / scheduling / interview start interaction | `test_approved_campaign_recovers_across_intake_to_explicit_hiring_outcomes` | passed, unchanged |

## Not verified

- Real Redis. Only the in-memory stand-in was used; the real-Redis tests in `tests/isolated/` were skipped.
- PostgreSQL. Only SQLite was used.
- Truly concurrent workers. Stale claims were simulated by writing lease fields directly.

## Open questions

1. An application skipped by the F4 guard is only logged. It does not appear as `needs_attention` with a `waiting_on`/`blocking_reason` on the progress API. Should a persistent handoff failure be surfaced that way?
2. The start replay returns the full interview state, including `skills`, `overall_score` and `evaluation`, to the candidate. This was already true before an outcome existed. Is that exposure intended?
3. Applications are still accepted and screened after the campaign deadline. This was observed while fixing F3. Is that intended?
