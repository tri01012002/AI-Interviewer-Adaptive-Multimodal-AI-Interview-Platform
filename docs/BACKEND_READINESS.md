# Backend Readiness Assessment

**Scope:** Backend-only runtime verification without external AI and a focused readiness-probe fix.
**Overall:** Offline API workflows and a limited PostgreSQL smoke passed; multi-tenant and production readiness remain **BLOCKED**.

## Executive summary

- The backend and UI are separate Git repositories. Work was confined to the backend repository on `p0-11-authorized-ci-verification`; the UI remained clean on `master`. No commits were created.
- Existing uncommitted provider and readiness work was preserved. This phase added only the health-readiness fix and regression coverage.
- A real Uvicorn process started without an AI key. Authentication, ownership, invitation redemption, interview answer/replay, result, and report APIs passed against an isolated PostgreSQL database and Redis.
- The first live `/health/ready` check returned `503` because the probe treated SQLAlchemy's `Engine` as a context manager. The probe now opens a connection and disposes the engine explicitly; a regression test covers both successful and failing database probes.
- Database-filtered per-recruiter and candidate-self authorization, report gating, and HTTP/WebSocket interview ownership checks exist and passed the focused offline regression run.
- There is no organization membership model and no production recruiter-to-candidate account-linking workflow. Do not use the system for multi-company customer data.
- `LLM_PROVIDER=none`, `EMBEDDING_PROVIDER=none`, and an empty `OPENAI_API_KEY` were used. No external AI-provider request was made; the result was labeled `deterministic`.
- The complete suite and production deployment were not verified. The PostgreSQL checks were bounded to a fresh migration and local API smoke, not a production or multi-instance certification.

## Repository boundary and change preservation

| Check | Result |
|---|---|
| Backend repository | `D:\cac\AI-Interviewer-Adaptive-Multimodal-AI-Interview-Platform`, branch `p0-11-authorized-ci-verification`. |
| UI repository | `D:\cac\AI_platform_UI`, branch `master`; clean and not modified. |
| Existing backend work | Preserved, including pre-existing offline-provider edits; this phase added the readiness fix and its tests. |
| Existing backend work | P0.1–P0.6 changes remain in the worktree and were not reverted. |
| Commits or pushes | None. |

## Verified behavior

### Authentication, ownership, reports, and WebSockets

- Protected API routes use the authenticated database user and deny anonymous access.
- Candidate reads and writes are filtered to the authenticated candidate or owning recruiter. Candidate update fields are allow-listed; candidate and interview ownership are server controlled.
- Interview reads and turns are checked using database-side ownership queries. Recruiter access requires the interview owner and candidate owner to match the recruiter; candidate access requires the candidate profile to link to that candidate user.
- Report JSON/text export first obtains the interview through the same authorized interview service query before loading report data.
- Both interview WebSocket routes authenticate the token, resolve the user from the database, and authorize interview access before accepting/starting the session.
- Admin-wide reads are an explicit role and are recorded in `access_audit`; public registration creates candidate accounts, not admins.
- Deterministic offline authorization tests cover anonymous access, cross-owner access, forged ownership, reports/downloads, WebSockets, and successful authorized requests.

### Tenant and account-linking boundary

- There is no organization/company or membership table. Organization identifiers appear as RAG document metadata only.
- Candidate accounts now default to unverified. Since the application has no email-delivery or verification provider, an authenticated admin must attest that email was verified out of band at `POST /api/v1/admin/users/{user_id}/verify-email` with `{"confirmation":"confirmed_out_of_band"}`. The operation is one-way and recorded in `access_audit`; public registration cannot set this state.
- A recruiter can create an invitation only for an owned, unlinked profile whose normalized email matches an existing verified candidate account. `POST /api/v1/candidates/{candidate_id}/invitations` returns an unpredictable token once, valid for 72 hours. The database stores only its SHA-256 digest. The recruiter must deliver the token to the candidate through a controlled channel associated with the already verified address; the API does not send email.
- The candidate must authenticate as the matching verified account and redeem with `POST /api/v1/candidate-invitations/redeem` and `{"token":"<one-time token>"}`. In one transaction the service conditionally assigns the profile, consumes the invitation, and writes the candidate-link audit event. An invitation can be revoked by its owning recruiter with `DELETE /api/v1/candidates/{candidate_id}/invitations`. Creation is limited to one per profile per minute and 20 per recruiter per hour; redemption is limited to 20 attempts per account per 15 minutes.
- Unknown, malformed, expired, consumed, revoked, and email-mismatched tokens return the same safe `400` response. Ownership violations return `404`; unauthorized roles return `403`; eligibility conflicts return `409`; bounded-rate violations return `429`. Public endpoints do not expose whether an unrelated user's email exists.
- The existing unique candidate `user_id` constraint prevents one account being linked to multiple profiles. Candidate email remains unique, and the redemption transaction also requires the stored profile email, invitation email, verified account email, and recruiter ownership to agree. A second claim of a profile is rejected by a conditional update.
- The new migration adds nullable `users.email_verified_at` and invitation storage without assigning owners or links to legacy candidates. No admin API for legacy ownership assignment is provided. For each legacy row, an operator must independently establish the correct recruiter through authoritative records, take a backup, use a reviewed maintenance transaction to set `candidates.owner_user_id` and insert a matching `access_audit` event, and verify both values before restoring service. Never infer ownership from name or email alone; leave unresolved rows unassigned.
- The authentication and profile-link flow is tested with SQLite and received a limited isolated PostgreSQL API smoke. Email verification is an explicit manual dependency, token delivery is operational/out-of-band, and production PostgreSQL transaction/locking behavior still requires controlled verification.
- RAG filters exclude scoped documents when a request is unscoped, but they are retrieval filters, not an API authorization boundary. Organization metadata must not be treated as authenticated tenant identity.
- **Supported boundary:** controlled, isolated single-tenant synthetic demo only. **Not supported:** multi-tenant demo or production customer data.

#### Candidate invitation API contract

| Operation | Authentication | Request | Success |
|---|---|---|---|
| `POST /api/v1/admin/users/{user_id}/verify-email` | Admin | `{"confirmation":"confirmed_out_of_band"}` | `200`, user ID and verification timestamp; records admin actor in `access_audit`. |
| `POST /api/v1/candidates/{candidate_id}/invitations` | Owning recruiter | No body; the address is taken from the stored profile and must match a verified candidate account. | `201`, `{invitation_id, token, expires_at}`. Token is returned only at creation and must not be logged. |
| `DELETE /api/v1/candidates/{candidate_id}/invitations` | Owning recruiter | No body. | `200`, `{revoked:true}`; one pending invitation is revoked and audited. |
| `POST /api/v1/candidate-invitations/redeem` | Verified candidate account | `{"token":"<invitation token>"}` | `200`, `{candidate_id}`. The token is single-use and expires after 72 hours. |

All payloads reject extra fields. Verification succeeds only when an admin explicitly attests to out-of-band verification; no registration email is trusted as verified. Invitation delivery is not implemented. Send tokens only through an approved controlled channel, never log them or include them in support diagnostics. Expired, used, revoked, unknown, malformed, and mismatched invitations share the same `400` response.

### Persistence, evaluation, RAG, and reports

- The active path is FastAPI → SQLAlchemy → configured database. Alembic owns schema changes; development startup initializes metadata only in development/test. P0.12 adds `b6c9_candidate_invitations` after `a4d2_resource_ownership`; migration preservation and downgrade/re-upgrade are covered by a disposable SQLite test.
- Interview turn writes use transactions and preserve duplicate-turn replay and conflicting-answer rejection. The cross-process idempotency regression passed in the focused run.
- Structured evaluation is schema-validated and persisted with the interview turn. Controlled offline tests exercise malformed output, provider timeout handling, retryable state, and persistence failure paths; these do not establish provider quality.
- RAG stores document metadata and optional vectors in SQLAlchemy. When embeddings are unavailable or invalid, retrieval is explicitly labeled lexical-only rather than silently substituting hash vectors.
- Prior P0.6 lexical-only benchmark: 8 queries; Recall@5 0.893; MRR@5 0.929; 0 dense candidates; 8 lexical fallbacks; forbidden-scope checks 1/1. This is not semantic retrieval evidence.
- No Celery task decorators, shared-task entrypoints, or `.delay()` enqueue calls were found in the Python sources searched. The API's synchronous request paths were covered; a production worker deployment was not verified.

### Production/bootstrap safety

- `Settings` now defaults `ENABLE_DEV_ADMIN_BOOTSTRAP` to false and both bootstrap credentials to empty.
- `UserStore.ensure_default_admin` no longer has known-value default arguments. Explicitly enabling development/test bootstrap without credentials raises an error.
- Production startup validates `SECRET_KEY` (configured, non-placeholder, and at least 32 characters) and refuses enabled development-admin bootstrap. Production skips development database initialization and admin creation.
- The default local API host is now `127.0.0.1`. Docker Compose separately binds its container server to `0.0.0.0`; do not expose it publicly without deployment-specific controls.
- `.env.example` documents the disabled bootstrap defaults and local-only opt-in. No secrets were printed or added to source.
- `/health/ready` now disposes its database probe, closes its Redis probe, and returns HTTP 503 when a dependency is unavailable. Both ready and failed dependency behavior are covered by focused tests.

## Findings and priorities

| Priority | Finding | Status / required action |
|---|---|---|
| P0 | No organization-level authorization or tenant membership model. | **Open release blocker.** Keep use to an isolated single-tenant synthetic demo until the model, migrations, and cross-tenant tests exist. |
| P0 | Organization-level authorization and tenant membership are not implemented. | **Open release blocker.** Keep use to an isolated single-tenant synthetic demo until the model, migrations, and cross-tenant tests exist. |
| P0 | Automated email verification/delivery, operator-managed legacy assignment, and PostgreSQL verification are not implemented. | **Manual dependency / not production-ready.** The new account-link API requires verified accounts and supports only out-of-band admin attestation and controlled token delivery. Legacy records remain unassigned. |
| P1 | Live model output, real embeddings, live E2E, PostgreSQL, and production deployment have not been verified in this environment. | **Blocked/unverified.** Run only with authorized credentials, isolated databases, operator-controlled spend caps, and human review. |
| P2 | Full-suite completion and provider-backed semantic quality remain unknown. | **Not run / blocked.** Do not infer a pass from focused tests or lexical metrics. |

## Current environment and live-provider status

Readiness was checked by printing only presence/absence, never values:

| Variable/resource | Status |
|---|---|
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | Absent |
| `RUN_LIVE_AI_TESTS`, `RUN_LIVE_AI_E2E` | Absent |
| `LIVE_AI_E2E_DATABASE_URL`, `DATABASE_URL` | Absent in the process environment during preflight |
| `LLM_PROVIDER`, `EMBEDDING_PROVIDER`, `LIVE_AI_SPEND_CAP_USD` | Absent |
| Backend `.env` file | Absent |

**Live provider calls:** none. The live smoke/E2E tests were not run as live tests. Prior P0.6 evidence recorded all four as skipped with opt-in flags unset; a skip is not a pass. No live cost was incurred in this assessment.

Before an authorized live run, follow the setup/runbook and the USD $5 maximum provider-side spend limit documented in [P0_6_LIVE_AI_VERIFICATION.md](./P0_6_LIVE_AI_VERIFICATION.md). Use a fresh dedicated SQLite database for E2E, bounded provider timeouts and no automatic retries, synthetic data only, and human review of all six actual model outputs. Never target a shared or production database.

## Validation performed in this task

Tests used the project's Python 3.11 virtual environment. The grouped API/regression command used a unique SQLite file under `storage/` and a 110-second subprocess timeout; the standalone security-default test and skipped opt-in test command used 30- and 60-second subprocess timeouts, respectively. The whole test suite was not run.

| Command / check | Result |
|---|---|
| Reproduction: `python -m pytest tests/unit/test_security_defaults.py -q` | **Exit 1**, 1 failed in 0.13s (0.94s wall). The new test observed that admin bootstrap defaulted to enabled. |
| Focused regressions: `python -m pytest tests/unit/test_security_defaults.py tests/unit/test_api_readiness.py tests/unit/test_api_interview.py tests/unit/test_auth_candidate_db.py tests/unit/test_production_pipeline.py tests/unit/test_resource_authorization.py tests/unit/test_report_service.py tests/unit/test_semantic_evaluation.py tests/unit/test_evaluation_domain.py tests/unit/test_adaptive_graph.py tests/unit/test_rag_retrieval.py tests/unit/test_rag_semantic_retrieval.py tests/unit/test_voice_websocket.py tests/unit/test_interview_hardening.py tests/integration/test_phase115c_application_concurrency.py::test_cross_process_interview_service_commits_one_turn_and_replays_idempotently -q -rs` | **Exit 0**, 80 passed, 1 skipped; pytest 16.83s, wall 17.77s. Skip: live embedding test requires credentials/provider configuration. |
| Recheck: `python -m pytest tests/unit/test_security_defaults.py -q` | **Exit 0**, 2 passed; pytest 0.79s, wall 1.70s. |
| Opt-in tests without live configuration: `python -m pytest tests/unit/test_live_provider_smoke.py tests/integration/test_live_interview_e2e.py -q -rs` | **Exit 0**, 4 skipped; pytest 0.26s, wall 1.14s. Three smoke cases required `RUN_LIVE_AI_TESTS=1`; E2E required `RUN_LIVE_AI_E2E=1`. No live request was made. |
| Live readiness preflight | **BLOCKED**, all required credentials/flags/database configuration listed above were absent; no request was attempted. |
| `ruff check services/user_store.py tests/unit/test_api_readiness.py tests/unit/test_security_defaults.py tests/unit/test_api_interview.py tests/unit/test_production_pipeline.py` | **Exit 0**. |
| `ruff check config/settings.py services/user_store.py apps/api/main.py tests/unit/test_api_readiness.py tests/unit/test_security_defaults.py tests/unit/test_api_interview.py tests/unit/test_production_pipeline.py` | **Exit 1**, five pre-existing findings: `W293` at `apps/api/main.py:72,74`; `UP007` at `config/settings.py:140,141`; `UP011` at `config/settings.py:166`. These unrelated lines were not changed. |
| `git diff --check` | **Exit 0**. |
| Pylance `checkSignatureCompatibility` for `UserStore.ensure_default_admin` | All 1 call site compatible with the explicit `email` and `password` parameters. |
| Full test suite | **NOT RUN**, to avoid the previously observed unbounded/hanging workflow. |

The focused regression command was run after changing startup defaults and explicitly seeding test admins. It covers the production bootstrap guard, API authentication, ownership authorization, reports, WebSockets, RAG fallback/scope, evaluation persistence, adaptive graph, and cross-process idempotency. It does not prove live provider behavior, production PostgreSQL operation, or multi-tenant isolation.

## Safe operator commands

Run offline tests from the backend repository using its `.venv` and a fresh, dedicated SQLite file. In PowerShell:

```powershell
$runId = [guid]::NewGuid().ToString("N")
New-Item -ItemType Directory -Force storage | Out-Null
$dbFile = "storage\test_backend_readiness_$runId.db"
if (Test-Path -LiteralPath $dbFile) { throw "Refusing to reuse test database: $dbFile" }
$env:DATABASE_URL = "sqlite:///./" + ($dbFile -replace '\\', '/')
$env:ENVIRONMENT = "test"
$env:LLM_PROVIDER = "none"
$env:EMBEDDING_PROVIDER = "none"
$env:OPENAI_API_KEY = ""
$env:RUN_LIVE_AI_TESTS = "0"
$env:RUN_LIVE_AI_E2E = "0"
$env:ENABLE_DEV_ADMIN_BOOTSTRAP = "false"
.\.venv\Scripts\python.exe -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "Isolated test database migration failed." }
.\.venv\Scripts\python.exe -m pytest tests\unit\test_security_defaults.py tests\unit\test_api_readiness.py tests\unit\test_api_interview.py tests\unit\test_auth_candidate_db.py tests\unit\test_production_pipeline.py tests\unit\test_resource_authorization.py tests\unit\test_report_service.py tests\unit\test_semantic_evaluation.py tests\unit\test_evaluation_domain.py tests\unit\test_adaptive_graph.py tests\unit\test_rag_retrieval.py tests\unit\test_rag_semantic_retrieval.py tests\unit\test_voice_websocket.py tests\unit\test_interview_hardening.py tests\integration\test_phase115c_application_concurrency.py::test_cross_process_interview_service_commits_one_turn_and_replays_idempotently -q -rs
```

The pytest safety guard accepts this `storage\test_*.db` URL and rejects missing, default, and non-SQLite database URLs. The command refuses to reuse an existing file before migration. Before migrations or live E2E, confirm the database URL is a new SQLite test path and not equal to any shared or production URL. Preserve the live-run limits and spend cap in the P0.6 runbook. Never print secret values, put them in command history, or commit `.env`.

## Changed files in this assessment

- `config/settings.py`: safe local bind and opt-in/empty development bootstrap defaults.
- `services/user_store.py`: require explicit admin bootstrap credentials.
- `.env.example`, `README.md`: describe explicit local bootstrap and loopback behavior.
- `tests/unit/test_security_defaults.py` (new): test safe defaults and explicit credential requirement.
- `tests/unit/test_api_interview.py`, `tests/unit/test_production_pipeline.py`: create isolated test admins rather than relying on a default account.
- `apps/api/main.py`, `tests/unit/test_api_readiness.py` (already changed during this assessment): readiness status and probe cleanup fix with regression tests.

## Next steps

1. Verify the P0.12 changes on the published CI workflow across Python 3.11 and 3.12, including the new account-linking and migration tests.
2. Add an approved email-verification and invitation-delivery provider, or define the production operational controls for the manual path.
3. Complete organization membership, tenant-scoped database queries, migrations, and cross-organization tests before any multi-tenant use.
4. Have an authorized operator run P0.6 live smoke/E2E verification with a dedicated SQLite database and the documented spend cap; require human review of the six real outputs.
5. Verify PostgreSQL and deployment configuration in a controlled environment before production use.

## Offline provider implementation follow-up

### Audit findings and priorities

The active branch had a clean worktree before this follow-up. The current request-path architecture
already includes interview ownership checks, durable/idempotent turn processing, candidate
invitation redemption, reporting, and deterministic evaluation; those features were preserved.
`CURRENT_STATE_AUDIT.md` is an older snapshot and does not reflect these completed changes. The
current readiness evidence and source/tests were used instead. The separate `AI_platform_UI`
repository was not modified.

Prioritized work based on that audit:

1. **P0 — Make provider selection fail closed.** An explicit OpenAI selection without a key
   previously returned no provider and silently enabled deterministic behavior. OpenAI embeddings
   similarly defaulted on while an absent key quietly disabled dense retrieval.
2. **P1 — Make no-key operation explicit and safe.** Default both model and embedding providers to
   `none`; provide an opt-in deterministic fixture provider for offline integration work.
3. **P1 — Label fixture outputs.** Identify question source in interview state and label mock
   evaluations and their rationale so synthetic scores/feedback cannot be mistaken for AI output.
4. **P1 — Regression-test offline/provider configuration and preserve core flows.** Keep tests
   independent of credentials and external AI requests.
5. **Open release blockers — Complete organization/tenant authorization and operational
   verification/delivery for email and invitations; run controlled PostgreSQL and production
   verification.** These are not safe to solve by changing existing auth or schema in this scoped
   offline-provider change. Frontend loading/error/expired-session work belongs to the separate UI
   repository and remains out of scope.

### Implemented behavior

- `LLM_PROVIDER=none` remains the default and uses the existing deterministic graph and evaluator.
- `LLM_PROVIDER=mock` selects fixed structured fixtures; mock questions are visibly prefixed,
  interview state includes `question_source=mock`, and mock evaluations use
  `evaluator_type=mock` and state that the score is not a candidate assessment.
- `LLM_PROVIDER=openai` without `OPENAI_API_KEY`, an unknown LLM provider, and
  `EMBEDDING_PROVIDER=openai` without `OPENAI_API_KEY` now raise explicit configuration errors.
  They do not silently switch to local/mock behavior.
- `EMBEDDING_PROVIDER=none` is the default and preserves the already-labeled lexical-only RAG
  fallback. Voice provider defaults remain fake. No API key is needed by default.
- No authentication, database schema, invitation API contract, or UI repository was changed.

### Validation for this follow-up

All runs use the Python 3.11.9 environment, an isolated SQLite test database, `LLM_PROVIDER=none`,
`EMBEDDING_PROVIDER=none`, empty `OPENAI_API_KEY`, and disabled live-test flags. No external AI
request is made.

| Command | Result |
|---|---|
| Baseline: `pytest tests/unit/test_llm_provider.py tests/unit/test_adaptive_graph.py tests/unit/test_semantic_evaluation.py tests/unit/test_rag_semantic_retrieval.py -q` | **37 passed, 1 skipped** before changes. The skip is the opt-in live embedding check. |
| Post-change core offline regressions: 16 focused unit files covering provider, graph, evaluator, RAG, bootstrap defaults, API, auth, candidate invitations, authorization, reporting, turn state, and database integrity | **98 passed, 1 skipped** in 25.54s. The skip is an opt-in live embedding test. |
| Live-provider smoke/E2E with `RUN_LIVE_AI_TESTS=0`, `RUN_LIVE_AI_E2E=0`, and empty `OPENAI_API_KEY` | **4 skipped** in 0.26s; three smoke tests and one E2E test skipped before any provider request. |
| Pylance Problems for all changed Python files | **No errors found.** |
| `git diff --check` | **Passed.** |
| Full suite and PostgreSQL verification | Not claimed by this follow-up. |

Pytest also emitted existing environment/configuration warnings for the missing `pytest-timeout`
plugin, Pydantic class-based settings, `python-multipart` import naming, and FastAPI `on_event`
deprecation. These are not test failures and were not changed here.

The exact offline test setup and no-key API commands are documented in the root README and this
assessment's safe-operator commands. Remaining blockers above must be resolved before production
or multi-tenant use.

## Runtime Verification and Workflow Hardening (2026-10-12)

### Runtime configuration and result

- Environment: Windows, repository `.venv`, Python 3.11.9; no repository `.env` was used.
- Providers: `LLM_PROVIDER=none`, `EMBEDDING_PROVIDER=none`, `OPENAI_API_KEY=""`, and live AI flags disabled.
- Dependencies: Redis 7.2.16 and PostgreSQL 15.19. The API used a newly created, isolated PostgreSQL database migrated to `b6c9_candidate_invitations`; that database was removed after verification.
- The local development admin bootstrap was explicitly enabled with throwaway credentials in the Uvicorn process environment. No credentials or invitation tokens were written to this document or logged.
- Uvicorn command:

  ```powershell
  .\.venv\Scripts\python.exe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
  ```

- Startup logs showed `Application startup complete` and the loopback Uvicorn listener.

| Check | Result |
|---|---|
| `GET /health` | `200`, status `ok` |
| `GET /health/live` | `200`, alive |
| `GET /health/ready` before fix | `503`; the database probe raised `TypeError` by using `with create_engine(...)`. |
| `GET /health/ready` after fix | `200`, database and Redis both `true` |
| Alembic on fresh PostgreSQL database | Passed through revision `b6c9_candidate_invitations` |
| Live PostgreSQL/Redis API smoke | Passed: admin/candidate/recruiter auth, anonymous denial, public role-escalation rejection, recruiter candidate creation, interview creation, pre-link denial, manual email attestation, invitation creation/redemption/reuse rejection, cross-user denial, answer submission/replay, persisted result, and report retrieval |
| Concurrent duplicate answer requests against PostgreSQL | Both requests returned `200`; a database query found exactly one durable matching turn |

The readiness fix explicitly opens and closes the SQLAlchemy connection and disposes the engine
in a `finally` block. The live smoke used the application's deterministic evaluator and lexical
retrieval only; it did not contact an AI provider.

### Tests and limits

The final focused command covered readiness, invitation expiry/reuse/ownership, interview
idempotency/recovery, resource authorization, API results, reports, migration safety, and security
defaults:

```powershell
python -m pytest tests\unit\test_api_readiness.py tests\unit\test_candidate_invitations.py tests\unit\test_interview_hardening.py tests\unit\test_resource_authorization.py tests\unit\test_api_interview.py tests\unit\test_report_service.py tests\unit\test_candidate_invitation_migration.py tests\unit\test_security_defaults.py -q -rs
```

Result: **41 passed, 0 failed, 0 skipped**, with six existing deprecation warnings. The test harness
requires SQLite, so this was not presented as a PostgreSQL pytest run. PostgreSQL validation here
consisted of the real Alembic upgrade and live API/concurrent-submission smoke above. The full
repository suite and production/multi-process deployment were not run.

### Frontend/API boundary

The separate `AI_platform_UI` repository was not changed. Frontend callers should account for:

- `GET /api/v1/interviews` returns an empty array when there are no visible interviews.
- Missing, invalid, or expired bearer authentication returns `401`; an authenticated but
  role-forbidden operation returns `403`. Cross-owner or otherwise invisible resources return
  `404` rather than disclosing their existence.
- Invalid payloads return `422`; turn conflicts or an in-progress duplicate return `409`.
  Retry an interrupted answer with the **same** `turn_id` and answer. A completed duplicate replays
  the stored result.
- Temporary evaluation or Redis-lock unavailability returns `503`; bounded invitation throttles
  return `429`.
- Unknown, malformed, expired, consumed, revoked, and account-mismatched invitation tokens all
  return the same `400` response: `Invitation is invalid or unavailable`. The API intentionally
  does not distinguish an expired invitation from a bad or already-used token.
- Interview and report requests are synchronous; the UI needs its own loading state. The API
  provides no distinct invitation-expired response or server-driven loading state. The configured
  access-token expiry is 30 minutes and should be handled as an authentication `401`.
- Offline evaluations are deterministic development signals, not real AI assessments. Keep the
  returned evaluator/question-source labels visible wherever scores, feedback, or questions are
  shown.

### Remaining priority

Per-user ownership is exercised, but there is no authenticated organization membership or tenant
identity in the schema. The supported boundary remains an isolated, single-tenant synthetic demo;
cross-company use is unsafe. The next security task should define the organization/membership model,
tenant identity source, migration/backfill policy, and cross-tenant authorization tests before
changing schema or route contracts. Production email verification and controlled invitation delivery
also remain operational dependencies.
