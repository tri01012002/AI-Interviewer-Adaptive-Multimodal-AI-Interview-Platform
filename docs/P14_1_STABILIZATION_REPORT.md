# P14.1 Stabilization Report

Date: 2026-11-25  
Readiness: **NOT_CUSTOMER_READY**

P14.1 stabilized reproducible offline test regressions. It did not establish
deployment readiness: Docker image verification and current-branch PostgreSQL,
Redis 7, and GitHub Actions verification remain blocked or unavailable.

## 1. Git state

- Branch: `main`
- HEAD and `origin/main`: `71a3314edd7962ed0f6629291b403c5e6ef2ff37`
- P14 and P14.1 changes remain uncommitted; no branch operation, commit, or
  push was performed.
- `apps/api/v1/routes/account_linking.py` and frontend repositories were not
  modified.
- Complete outputs/reports are retained under ignored `storage/`:
  `p14_original_full_pytest.log`, `ci_p141_broad_pytest.log`,
  `ci_p141_broad_junit.xml`, `p141_campaign_group2.log`,
  `p141_campaign_group2.xml`, `p141_final_full_unit.log`, and
  `p141_final_full_unit.xml`.

## 2. Original P14 broad-run failures

The original broad run reported **572 passed, 8 failed, 19 errors, 4
deselected**. A later system-Python reproduction reported **577 passed, 3
failed, 19 errors, 4 deselected**. The project virtualenv uses FastAPI 0.104.1,
Starlette 0.27.0, and pytest 7.4.3; the system Python used FastAPI 0.151.0,
Starlette 1.9.0, and pytest 9.1.1 and did not have `prometheus_client`.

### Eight failures

| Test | Exception / observed failure | Root cause and affected capability |
|---|---|---|
| `test_failure_on_one_application_does_not_block_unrelated_applications` | `AssertionError`: application remained `pending`, expected `needs_review` | Test dispatched only one global queue item. Older eligible work from other tests could be ahead of its application. Test-order/isolation assumption; campaign dispatch capability. Fixed to dispatch through the target's eligible queue position. |
| `test_log_records_emitted_during_request_carry_request_id` | `AssertionError`: expected `/api/v1/interview/{interview_id}/answer`, observed `/interview/{interview_id}/answer` | System FastAPI/Starlette route-template behavior differed from the pinned project environment. The test passes in the project virtualenv and full-suite rerun; no application behavior change was needed. |
| `test_every_tenant_scoped_http_route_is_support_audited` | `AssertionError`: expected tenant-scoped routes, got `set()` | Route introspection under the system dependency set did not expose the expected routes. The project-virtualenv full suite passes this test. Support-audit route coverage; dependency/environment mismatch, not a confirmed authorization regression. |
| `test_authenticated_voice_websocket_final_flow_and_duplicate_final` | `WebSocketDisconnect`, close code 4403: “Voice interviews are disabled” | `VOICE_ENABLED=false` in that run. Re-run with voice enabled in test configuration; voice WebSocket flow. |
| `test_socket_stays_responsive_while_answer_blocked` | Same 4403 disconnect | Same explicit voice-disabled test configuration; async voice responsiveness. |
| `test_disconnect_during_answer_processing_completes_turn_quietly` | Same 4403 disconnect | Same explicit voice-disabled test configuration; disconnect lifecycle. |
| `test_cancel_resets_binary_utterance` | Same 4403 disconnect | Same explicit voice-disabled test configuration; utterance cancellation. |
| `test_normal_disconnect_is_not_logged_as_error` | Same 4403 disconnect | Same explicit voice-disabled test configuration; clean shutdown logging. |

### Nineteen setup errors

All were in `tests/unit/test_observability_metrics.py`. Each raised an
`AssertionError` at fixture setup (`assert registry.enabled`); the
`MetricsRegistry.enabled` value was false because the system Python environment
lacked `prometheus_client`. They affected HTTP/readiness/rate-limit metrics,
LLM/RAG/lease/worker/voice telemetry, metrics endpoint behavior, label
protection, and optional-dependency behavior. All 19 passed in the full
project-virtualenv run:

- `test_http_requests_are_counted_by_route_template_and_status_class`
- `test_readiness_updates_dependency_gauges`
- `test_rate_limit_decisions_are_counted_including_429`
- `test_rate_limit_backend_error_is_counted_when_failing_open`
- `test_llm_tokens_and_outcome_are_recorded_from_response_usage`
- `test_llm_cost_is_estimated_only_from_configured_prices`
- `test_llm_missing_usage_and_failures_are_categorized`
- `test_rag_retrieval_records_hit_miss_and_error`
- `test_lease_renewal_outcomes_are_counted`
- `test_worker_items_bind_log_context_and_record_outcomes`
- `test_stt_and_tts_failures_are_counted_by_provider_and_code`
- `test_metrics_endpoint_is_open_in_test_env_without_token`
- `test_metrics_endpoint_requires_configured_bearer_token`
- `test_metrics_endpoint_hidden_in_production_without_token`
- `test_metrics_endpoint_404_when_disabled`
- `test_no_ids_emails_or_raw_paths_reach_label_values`
- `test_label_guard_normalizes_unexpected_values_and_rejects_unknown_names`
- `test_api_key_never_appears_in_exposition_or_logs`
- `test_app_works_when_prometheus_client_is_unavailable`

Separately, the P13 GitHub Actions run found RAG corpus digest mismatches on
LF/CRLF checkouts and a campaign queue-order assumption. These were addressed
in P14.1; the current uncommitted changes have not been run by GitHub Actions.

## 3. Fixes applied

- `rag/corpus_loader.py` normalizes CRLF to LF before validating the pinned
  approved demo-corpus digest. A regression test verifies both checkout
  newline styles. This preserves the content-integrity check while making it
  checkout-independent.
- Campaign lifecycle tests now dispatch the target application through its
  actual position in the eligible-work queue and assert exclusion of the
  target campaign's pending work, rather than assuming the global queue is
  empty.
- The worker-crash E2E regression likewise dispatches through the target's
  eligible queue position.
- No production business semantics or test thresholds were weakened.

## 4. Verification evidence

### Full offline unit suite — TESTED_OFFLINE

Executed with the project virtualenv, a fresh disposable SQLite database,
`ENVIRONMENT=test`, disabled live-provider opt-ins, and the disposable local
Redis 3.0.504 instance on database 1:

```powershell
$env:DATABASE_URL='sqlite:///./storage/ci_p141_final_full.db'
$env:ENVIRONMENT='test'
$env:LLM_PROVIDER='none'
$env:EMBEDDING_PROVIDER='none'
$env:VOICE_ENABLED='true'
$env:REDIS_URL='redis://127.0.0.1:16379/1'
$env:REDIS_INTEGRATION_URL=''
$env:RUN_POSTGRES_TESTS='0'
$env:RUN_REDIS_INTEGRATION_TESTS='0'
$env:RUN_LIVE_PROVIDER_TESTS='0'
$env:ALLOW_LIVE_PROVIDER_TESTS='0'
.\.venv\Scripts\python.exe -m pytest tests\unit -m 'not live' `
  --timeout=90 --timeout-method=thread -q -ra `
  --junitxml=storage\p141_final_full_unit.xml `
  *> storage\p141_final_full_unit.log
```

Result: **600 passed, 0 failed, 0 errors, 4 deselected**, 5 warnings, in
673.47 seconds. The warnings are existing LangGraph/FastAPI lifecycle
deprecations. No live provider tests were selected or contacted.

### Focused and static checks

- Campaign lifecycle/workflow group: **TESTED_OFFLINE** — 173 passed, 0 failed,
  0 skipped.
- RAG suite after newline fix: **TESTED_OFFLINE** — 23 passed, 1 opt-in live
  test skipped.
- Request-context/support-audit/voice focused checks: **TESTED_OFFLINE** — 7
  passed.
- Worker-crash regression: **TESTED_OFFLINE** — 1 passed.
- Fresh disposable SQLite empty-to-head migration: **VERIFIED** — passed;
  `g21_candidate_legal_hold (head)`.
- `alembic heads`: **VERIFIED** — exactly one head,
  `g21_candidate_legal_hold (head)`.
- `tools/ci_check_test_coverage.py`: **VERIFIED** — every `tests/unit` file is
  referenced by CI.
- Ruff on `rag/corpus_loader.py`,
  `tests/unit/test_rag_semantic_retrieval.py`, and
  `tests/unit/test_e2e_recruitment_journey.py`: **VERIFIED** — passed.
- Ruff including `tests/unit/test_campaign_lifecycle.py`: reports three
  pre-existing F401 imports (`sqlalchemy.update`,
  `RecruitmentCampaignExecutionStepRecord`, and
  `RecruitmentCampaignHiringOutcomeRecord`) on unchanged import lines. They
  were not introduced or fixed as unrelated.
- `git diff --check`: **VERIFIED** — passed.

## 5. Infrastructure and CI

### Docker — BLOCKED

`docker info --format '{{.ServerVersion}}'` failed because
`npipe:////./pipe/dockerDesktopLinuxEngine` does not exist. Neither image was
built or inspected. Do not treat Docker context exclusions, UID/GID 10001, or
storage ownership as runtime-verified.

Once Docker is available, run from the repository root:

```powershell
docker build -f docker/Dockerfile.api -t ai-interviewer-api:p14.1 .
docker build -f docker/Dockerfile.worker -t ai-interviewer-worker:p14.1 .
docker image inspect ai-interviewer-api:p14.1 --format '{{.Config.User}}'
docker image inspect ai-interviewer-worker:p14.1 --format '{{.Config.User}}'
docker run --rm --entrypoint sh ai-interviewer-api:p14.1 -c 'id; test -w /app/storage/private_cv'
docker run --rm --entrypoint sh ai-interviewer-worker:p14.1 -c 'id; test -w /app/storage/private_cv'
docker run --rm --entrypoint sh ai-interviewer-api:p14.1 -c 'find /app -type f \( -name ".env" -o -name "*.db" -o -name "*.log" -o -path "/app/storage/*" -o -path "/app/tests/*" \) -print'
docker run --rm --entrypoint sh ai-interviewer-worker:p14.1 -c 'find /app -type f \( -name ".env" -o -name "*.db" -o -name "*.log" -o -path "/app/storage/*" -o -path "/app/tests/*" \) -print'
```

The `id` output must show UID/GID 10001; `find` must not report secrets,
candidate files, local databases, logs, or test artifacts. Also inspect image
metadata and layers before accepting the image.

### PostgreSQL — BLOCKED for P14.1 local verification

No disposable PostgreSQL service was available locally, so P14.1 did not
verify PostgreSQL migrations, tenant isolation, multi-worker concurrency,
lease renewal, erasure, legal hold, or export erasure. Historical P13 GitHub
Actions evidence for the baseline commit recorded **18 PostgreSQL tests
passed**; it does not validate these uncommitted changes.

### Redis — BLOCKED for P14.1 supported-version verification

The disposable local Redis responded to `PING`, but reports version
**3.0.504**, below the integration suite's Redis 6+ requirement. No P14.1
Redis integration suite was claimed as passed. Historical P13 GitHub Actions
evidence recorded **21 Redis tests passed against Redis 7** on the baseline
commit only.

When disposable Docker infrastructure is available:

```powershell
docker run --rm --name p141-redis -p 16379:6379 redis:7-alpine --save "" --appendonly no
$env:REDIS_INTEGRATION_URL='redis://127.0.0.1:16379/0'
.\.venv\Scripts\python.exe -m pytest tests\redis -q -ra
```

### GitHub Actions — current changes NOT VERIFIED

The P13 workflow run on the exact baseline commit is available at
[Actions run 45571865439](https://github.com/tri01012002/AI-Interviewer-Adaptive-Multimodal-AI-Interview-Platform/actions/runs/45571865439).
Its overall workflow failed: RAG matrix jobs failed on the digest mismatch,
and the campaign workflow failed on the queue-order assertion. PostgreSQL,
Redis, quality, and other listed jobs succeeded; the PostgreSQL, Redis, and
campaign evidence above belongs to that baseline run only. No Actions run
covers the current uncommitted P14.1 fixes.

## 6. Remaining blockers and actions

| Owner | Status | Required action |
|---|---|---|
| Release engineering | BLOCKED | Restore Docker; build and inspect both images; verify UID/GID 10001, runtime storage writes, health/readiness, upload/export/deletion paths, and absence of sensitive files. |
| DevOps / CI | BLOCKED | Run the complete workflow on the P14.1 changes after they are committed/pushed by the owner; record actual job results. |
| Database owner | BLOCKED | Run disposable PostgreSQL integration and migration suites on the P14.1 revision. |
| DevOps | BLOCKED | Run Redis lock, duplicate-answer, rate-limit, expiry, reconnect, failure-mode, and multi-process suites against disposable Redis 7. |
| Product / legal | REQUIRES_CUSTOMER_DECISION | Approve retention periods and scope, hired/unsuccessful-candidate handling, score/audit retention, legal-hold governance, and candidate report-export content. No retention period or automatic deletion was invented or enabled. |
| Provider owner | BLOCKED | Explicitly authorize spend-capped synthetic smoke tests before any live OpenAI, AssemblyAI, ElevenLabs, or SMTP request. No live requests were made. |

The full offline unit suite is green, but Docker, PostgreSQL, supported-version
Redis, current-branch CI, live providers, and customer/legal decisions remain
unverified or unresolved. Therefore the release remains **NOT_CUSTOMER_READY**.
