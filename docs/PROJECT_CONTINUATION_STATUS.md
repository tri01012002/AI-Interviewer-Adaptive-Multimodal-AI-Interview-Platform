# Project Continuation Status

Snapshot date: 2026-11-24. Written from source inspection of `main` @ `4d2e7ad`.
Historical reports in the repo root and `docs/` were treated as evidence to check, not as truth.

Status vocabulary: IMPLEMENTED_AND_VERIFIED, IMPLEMENTED_BUT_NOT_FULLY_VERIFIED, PARTIALLY_IMPLEMENTED,
MOCK_OR_DETERMINISTIC_ONLY, DOCUMENTED_BUT_NOT_IMPLEMENTED, NOT_IMPLEMENTED, BLOCKED.
"Verified" here means covered by offline tests on SQLite unless PostgreSQL is stated explicitly.

---

## 1. Product purpose

A campaign-oriented recruitment platform. A recruiter defines a hiring objective; the platform helps turn it
into an approved plan (objective → requirements → JD → rubric → application form), takes CV applications,
screens them, drives candidate communication, availability and scheduling, runs an adaptive text/voice
interview, and records hiring outcomes until the target or deadline is reached. High-impact actions
(campaign approval, outbound communication, schedule confirmation, `hired` outcomes) stay behind explicit
human approval. The long-term vision (autonomous intake → publishing → continuous processing) is only
partly built; see section 5.

## 2. Current architecture

| Layer | Where | Notes |
|---|---|---|
| API | `apps/api/main.py`, `apps/api/v1/routes/*` | FastAPI. Campaign routes mounted at `/api/v1` (`main.py:94`), others under `API_PREFIX` (`main.py:243-248`). Health: `/health`, `/health/live`, `/health/ready` (DB + Redis). |
| Tenant boundary | `services/tenant_authorization.py` | `TenantContext` resolved from active membership; platform admins (`role == "admin"`) need `X-Organization-ID` + `X-Support-Reason`. |
| Support-access audit | `services/support_access_audit.py` | Fail-closed preflight middleware for platform-admin requests on listed path prefixes. |
| Campaign domain (deterministic) | `services/recruitment_campaign_service.py`, `candidate_*_service.py`, `interview_scheduling_service.py` | Versioned objective/plan, approval pinning, idempotency keys, optimistic versions. |
| Orchestration | `services/recruitment_campaign_orchestrator.py`, `services/recruitment_campaign_worker.py` | DB-polling worker, CAS claims with lease token + expiry, stale-claim fencing, lifecycle reconciler. Redis is not used for campaign coordination. |
| Interview engine | `agents/interview_agent/graph/__init__.py`, `services/interview_service.py` | LangGraph `StateGraph`: analyze → extract evidence → update competencies → gaps → decide → (retrieve context → generate question → validate) or finish. |
| Evaluation | `evaluation/service.py` | `LLMEvaluator` (Pydantic structured output, evidence must be exact quotes) or `DeterministicEvaluator` (keyword heuristic). |
| LLM boundary | `integrations/llm_providers/provider.py` | `OpenAIProvider` (raw httpx), `DeterministicMockProvider`; `LLM_PROVIDER=none` default. |
| RAG | `rag/service.py` | BM25 + optional dense cosine (RRF), deterministic reranker, embeddings stored as JSON text in `rag_documents`. |
| Voice | `voice/`, `apps/api/websocket/` | Utterance-buffered pipeline; fake STT/TTS/VAD by default. |
| Infra | `infrastructure/` | Redis distributed lock (used for interview turns), Redis store, rate limiter and circuit breaker (not wired). |
| Persistence | `services/database.py`, `alembic/versions/` | Linear chain, single head `g20_campaign_interview_linkage`. |

Interview turn trace: `InterviewService.submit_answer` (`interview_service.py:431`) → Redis lock (required;
no Redis = `InterviewUnavailableError`) → turn idempotency by `turn_id` → evaluator (LLM call #1 if configured)
→ `agent.handle_answer` (analysis maps the evaluation; decision is rule-based; question generation is LLM call
#2 if configured, template fallback otherwise) → RAG follow-ups → `save_result` + `save_state` in one transaction.
Failure marks the turn `FAILED_RETRYABLE`.

Campaign → interview: candidate calls `POST /interview/campaigns/{cid}/applications/{aid}/start`
(`routes/interview.py:78`) → `start_campaign_interview` (`interview_service.py:176`) requires current approval,
reviewed application, the candidate's own user, and a `confirmed_internal` schedule; snapshots rubric
competencies; idempotent per (org, campaign, application). The rubric snapshot is used by the evaluator
(`interview_service.py:591-600`). The reconciler reads interview `status == completed` to move the
application to `awaiting_hiring_decision` (`orchestrator.py:1778`).

## 3. Current branch and HEAD

- Local `main` @ `4d2e7ad44d11e798404716a6d7dbd2908e13bc73`, up to date with `origin/main` after `git fetch` on 2026-11-24. Working tree was clean before this work.

## 4. Changes not yet merged

| Branch | vs `origin/main` | Notes |
|---|---|---|
| `interview-agent` | 9 commits ahead, 0 behind | Job Intake / JD generation (`/jobs`, `/jobs/generate`, `/jobs/generate-from-request`, `/jobs/{id}/approve`), `SupervisorAgent`, `JobStore`. **Not merged; incompatible as-is** (below). |
| `Phuoc`, `ha-vy`, `p0-11-authorized-ci-verification`, `test/task2-postgres-multi-worker`, `fix/r54-campaign-lifecycle` | 0 ahead | Fully contained in `main`. |

`interview-agent` incompatibilities (from `git diff origin/main...origin/interview-agent`):
1. **Second Alembic head.** `c8d1_jobs.down_revision = "b6c9_candidate_invitations"`, but `main` already continues `b6c9 → d3f7 → … → g20`. Merging creates two heads.
2. **Not tenant-scoped.** `jobs` is keyed by `owner_user_id` with no `organization_id`, unlike every campaign table.
3. **Authorization gap.** `POST /jobs/{job_id}/approve` calls `SupervisorService.approve_job(job_id)` with no ownership or role check; any authenticated user could approve any job.
4. **Duplicates campaign concepts.** Campaigns already hold JD, requirements, approval and an LLM `JobDesignAgent` (`orchestrator.py:192-264`). The useful new piece is the *intake* step (parse a free-text request, list missing fields, ask clarifying questions), which overlaps `update_objective` + objective clarifications (`recruitment_campaign_service.py:517,631`).

Recommendation: do not merge. Port the intake parser/missing-field logic onto the campaign objective (product-owner decision needed on whether `/jobs` survives as a separate entity).

## 5. Capability status

| Capability | Status | Evidence |
|---|---|---|
| Campaign create/objective/plan/approval | IMPLEMENTED_AND_VERIFIED | `recruitment_campaign_service.py:347-903`; `tests/unit/test_recruitment_campaign*.py` |
| LLM job design for missing artifacts | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | `orchestrator.py:192-264`; only mock provider in tests; rarely triggered because approval already requires all artifacts |
| CV intake + parse | IMPLEMENTED_AND_VERIFIED | `candidate_application_service.py`, `candidate_cv_parser.py` (pypdf/python-docx) |
| Screening | MOCK_OR_DETERMINISTIC_ONLY | `candidate_screening.py:46` keyword-based; human review gate |
| Communication (email) | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | Lease/fencing in `candidate_communication_service.py:778-822`; SMTP path not live-tested; duplicate window on SMTP `OSError` (`email_provider.py:124-139`) |
| Availability + scheduling | IMPLEMENTED_AND_VERIFIED | Worker proposes/submits; only the assigned interviewer confirms (`interview_scheduling_service.py:723-729`) |
| Campaign → interview linkage | IMPLEMENTED_AND_VERIFIED | `interview_service.py:176-395`, migration `g20` |
| Hiring outcome + progress | IMPLEMENTED_AND_VERIFIED | `record_hiring_outcome` `:943`; reconciler `orchestrator.py:1499-1688` |
| Worker claims, fencing, retry, pause | IMPLEMENTED_AND_VERIFIED (PostgreSQL multi-worker per `docs/TASK2…`, real PG 16) | `orchestrator.py:1104-1313`. No lease renewal. |
| Interview graph (LangGraph) | IMPLEMENTED_AND_VERIFIED | `StateGraph` in `graph/__init__.py:253-276` |
| LLM evaluation / question generation | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | Real OpenAI path only in opt-in `live-provider` CI job |
| Deterministic evaluation | MOCK_OR_DETERMINISTIC_ONLY | `evaluation/service.py:176-243` |
| Anthropic provider | DOCUMENTED_BUT_NOT_IMPLEMENTED | Settings exist (`settings.py:86-87`), no runtime provider |
| RAG hybrid retrieval | PARTIALLY_IMPLEMENTED | BM25 + optional dense, in-Python full scan; only demo corpus ingestable globally |
| Tenant-scoped RAG in interviews | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED (fixed 2026-11-24, SQLite only) | Graph state now carries `organization_id`/`campaign_id`; missing tenant fails closed; campaign-scoped chunks via `campaign_scope_key`. `tests/unit/test_tenant_interview_retrieval.py`. See `docs/PRODUCTION_HARDENING_REPORT.md` §5 |
| Secret redaction | IMPLEMENTED_AND_VERIFIED (offline) | Import-time key prints removed; secret config fields `repr=False`; `tests/unit/test_secret_redaction.py` |
| pgvector / vector DB | DOCUMENTED_BUT_NOT_IMPLEMENTED | No `vector(` column; `rag/vector_store/__init__.py` empty |
| Cross-encoder reranker | DOCUMENTED_BUT_NOT_IMPLEMENTED | `DeterministicReranker` only (`rag/service.py:241-253`) |
| Voice pipeline | MOCK_OR_DETERMINISTIC_ONLY by default | Fake STT returns a fixed sentence (`voice/providers.py:81-96`) |
| Real STT (AssemblyAI) / TTS (ElevenLabs) | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED; STT request flow BLOCKED on an authorized provider environment | Local protocol defects fixed 2026-11-24 (`send_audio` async generator, session cleanup, error normalization; `tests/unit/test_stt_assemblyai_offline.py`). The AssemblyAI request shape (raw hex PCM to `/v2/transcript`) is still likely wrong; never live-tested |
| Distributed lock | IMPLEMENTED_AND_VERIFIED (real Redis 7.2, PHASE_11_5B/C) | `infrastructure/distributed_lock.py` |
| Rate limiter / circuit breaker | NOT_IMPLEMENTED in request path | Classes exist; not wired |
| Metrics / tracing / Sentry | DOCUMENTED_BUT_NOT_IMPLEMENTED | No imports, no `/metrics` |
| CV retention / deletion | NOT_IMPLEMENTED | `CandidateStore.delete` returns False (`candidate_store.py:129-138`); no retention job |
| Platform-admin support audit | PARTIALLY_IMPLEMENTED → **fixed by this change** | `_AUDITED_PREFIXES` missed 4 tenant route families |

## 6. Known limitations

- No lease renewal: an LLM call longer than `CAMPAIGN_WORKER_LEASE_SECONDS` (120s) is retried by another worker (two LLM calls). Email is at-most-once only up to the SMTP duplicate window.
- Interview start time is not checked against the confirmed schedule window.
- `_waiting_on("awaiting_schedule_confirmation")` returns `"candidate"`, but only the interviewer can confirm (`orchestrator.py:1789`).
- `record_hiring_outcome` does not require a completed interview (policy question).
- Any recruiter in an org can read/mutate any campaign in that org (`_assert_authorized`), while list shows only their own.
- `recruitment_campaign_executions.campaign_id` FK is single-column; `execution_steps` has no `organization_id`; `interviews.organization_id` and `candidates.organization_id` are nullable.
- ~~`core/configs.py:105-106` prints the Groq API key at import~~ fixed 2026-11-24 (rotate the Groq key if indexing ran with a real key in shared logs). Still open: `core/llm.py:65` makes every provider except Groq unreachable, and `core.configs` rejects the app default `LLM_PROVIDER=none`.
- RAG grounding returns 0 chunks for realistic inputs (title variants, rubric labels, `general` competency, plurals); in-memory competency filter is stricter than SQL. See hardening report §6, task R1.
- Voice: `submit_answer` runs synchronously inside the async handler; TTS chunks are sent after `handle` returns.
- CI runs only SQLite and does not include the campaign, scheduling, communication, availability, tenant or `tests/postgres` test files.
- `services/interview_store.py` is a broken legacy facade (calls without `organization_id`).

## 7. Outdated documentation

| Claim | Where | Reality |
|---|---|---|
| Supabase/pgvector `vector(1536)` store | README.md:249, ARCHITECTURE.md:380-381, 477, 839 | No vector column; JSON-text embeddings + Python cosine |
| Cross-encoder/ColBERT reranker | ARCHITECTURE.md:501-505 | Deterministic term-coverage reranker |
| Real-time Whisper/ElevenLabs speech | README.md:23, 244-245 | Fake by default; no Whisper; STT is buffered, not streaming |
| `kubectl apply -f kubernetes/` | README.md:376-383 | No Kubernetes manifests |
| Prometheus/Grafana/Sentry | README.md:256, ARCHITECTURE.md:206 | Not imported; compose mounts a missing `docker/prometheus.yml` |
| Celery workers (`apps.worker.celery_app`) | Makefile, compose `flower` | Module does not exist; worker is DB-polling |
| LangGraph "mocked / not used" | CURRENT_STATE_AUDIT.md:37-41, IMPLEMENTATION_AUDIT.md | LangGraph `StateGraph` is real |
| "Delivered exactly once" | docs/TASK2…:95 | Observation from one 8s test, not a guarantee |
| Real-Redis lock verification blocked | PHASE_11_5A:9 | Later verified in PHASE_11_5B/C |
| `pytest-output.txt` (2 failed / 230 passed) | repo root | Stale, recorded on another machine |
| `make install` / `db-seed` | Makefile | `requirements-dev.txt` and `integrations.supabase.seeds` missing |

## 8. Top five next tasks

| # | Task | Current state / evidence | Missing behavior | Impact / risk | Priority | Acceptance criteria |
|---|---|---|---|---|---|---|
| 1 | **Close support-access audit gaps** (done in this change) | `_AUDITED_PREFIXES` (`support_access_audit.py:24-31`) omits `/recruitment-applications`, `/candidate-communications`, `/candidate-availability-requests`, `/interview-schedules` | Platform-admin reads of CV text, messages, availability and schedules leave no fail-closed audit row | Compliance / privacy: unaudited cross-tenant support access to candidate PII | P0 | Every tenant-scoped route family produces an `attempted` audit row for platform-admin requests and fails closed (503) when the audit cannot be written |
| 2 | Pass tenant + campaign identity into interview RAG (**done 2026-11-24**, offline) | Fixed; see `docs/PRODUCTION_HARDENING_REPORT.md` §5 | — | — | — | Met on SQLite; PostgreSQL run pending |
| 3 | CV retention and deletion | No delete path, no retention (`candidate_store.py:129-138`) | Retention policy, deletion of file + extracted text, audit | GDPR/PDPA exposure | P1 (needs product-owner retention policy) | Policy documented; admin-only deletion removes file and `extracted_text`; audit row; tests |
| 4 | Worker lease renewal / heartbeat for long LLM steps | No renewal (`orchestrator.py:1104-1176`) | Extend lease while a step runs | Duplicate LLM spend; repeated side effects | P2 | Long step keeps lease; stale worker still fenced; PostgreSQL multi-worker test |
| 5 | Add campaign/tenant/scheduling suites to CI, plus a PostgreSQL service job | `.github/workflows/test.yml` groups | CI does not run most campaign tests or `tests/postgres` | Regressions in the core business flow go unnoticed | P2 | New CI groups green on SQLite; opt-in PG job using disposable `test_` DB |

Deferred, with reason: real STT/TTS (provider credentials/authorization needed; current AssemblyAI client likely broken), observability stack (deployment target undecided), Job Intake merge (product-owner decision on `/jobs` vs campaign objective), hiring-outcome-requires-interview and interview-start-window (policy decisions).

## 9. Recommended first task

**Task 1: close the platform-admin support-access audit gaps.** It is a confirmed security/compliance defect
with direct evidence, a small and well-contained change (one constant + resource mapping), it reuses the
existing fail-closed mechanism instead of adding new logic, needs no product decision (the platform already
requires an audit row for platform-admin access to campaigns and interviews), and is testable offline.
Task 2 is the next recommendation.

## 10. Update 2026-11-24 (production hardening pass)

Done on `main` (uncommitted): secret leakage fix, tenant-aware interview retrieval, local STT protocol fixes. Reviewed: RAG quality, worker/PostgreSQL reliability. Full details, evidence and test results: `docs/PRODUCTION_HARDENING_REPORT.md`.
Next milestone: CI with real PostgreSQL + Redis services, then worker lease renewal, then retrieval-quality task R1.

## 11. Update 2026-11-24 (Milestone 12)

Implemented on `main` (uncommitted): PostgreSQL/Redis CI jobs with skip guards, worker lease renewal, RAG retrieval quality + probes (hit-rate@5 0.1 → 1.0), candidate personal-data erasure, SMTP phase classification, request IDs + rate limiting, AssemblyAI documented flow, doc corrections, Alembic logger fix.
Verified: 504 offline tests (SQLite); 18 `tests/postgres` + 233 workflow tests on real PostgreSQL 16.2 (local). Not verified: real Redis, GitHub CI execution, external providers, deployment.
Current status table: `docs/PRODUCTION_READINESS.md`. Full report: `docs/MILESTONE_12_REPORT.md`.

## 12. Update 2026-11-24 (Milestone 13 — Release Candidate)

Implemented (uncommitted, `main`): CI coverage guard + Redis rate-limit suite, report-export erasure, voice off-loop/streaming/cancellation, candidate allowlist views, retention dry-run + legal hold (migration g21), metrics + `/metrics`, opt-in provider smoke tests, deployment runbook, end-to-end journey test.
Verified: 599 offline tests; PostgreSQL 16 suites (see `docs/MILESTONE_13_REPORT.md`). Blocked: real Redis, GitHub CI run, live providers, image build. Product decisions: `docs/RETENTION_POLICY_DECISIONS.md`.
