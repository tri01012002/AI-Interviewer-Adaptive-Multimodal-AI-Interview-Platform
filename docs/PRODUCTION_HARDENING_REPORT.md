# Production Hardening Report

Date: 2026-11-24. Branch policy: work only on `main`; no branches created, switched, merged or cherry-picked. Nothing committed or pushed.

Evidence labels used below:
- **Implemented**: code changed in this pass.
- **Tested offline**: covered by tests run here (SQLite file DB, fakeredis TCP server, `httpx.MockTransport`; `LLM_PROVIDER=none`, `EMBEDDING_PROVIDER=none`).
- **Verified with real infrastructure**: none in this pass. No PostgreSQL, Redis, STT, TTS or LLM provider was available or authorized.

## 1. Branch, HEAD and working tree

| | Value |
|---|---|
| Branch | `main` |
| HEAD | `4d2e7ad44d11e798404716a6d7dbd2908e13bc73` (= `origin/main` after `git fetch`; 0 ahead / 0 behind) |
| Before this pass | `M services/support_access_audit.py`; untracked `docs/PROJECT_CONTINUATION_STATUS.md`, `tests/unit/test_support_access_audit_coverage.py` (previous audit, uncommitted) |
| After this pass | Modified: `agents/interview_agent/graph/__init__.py`, `config/settings.py`, `core/configs.py`, `core/llm.py`, `integrations/llm_providers/provider.py`, `rag/service.py`, `services/interview_service.py`, `services/support_access_audit.py` (unchanged from previous audit), `tests/unit/test_rag_semantic_retrieval.py`, `tests/unit/test_voice_phase10.py`, `voice/real_providers.py`. New: `docs/PRODUCTION_HARDENING_REPORT.md`, `tests/unit/test_secret_redaction.py`, `tests/unit/test_tenant_interview_retrieval.py`, `tests/unit/test_stt_assemblyai_offline.py`. `docs/PROJECT_CONTINUATION_STATUS.md` updated. |

## 2. Existing changes preserved

The previous support-access audit fix was inspected (`git diff services/support_access_audit.py`) and left untouched. It is still uncommitted. Its 22 tests pass in the full run below.

## 3. Security issues discovered

| # | Issue | Evidence | Status |
|---|---|---|---|
| S1 | Groq API key printed to stdout on import of the offline LLM config | `core/configs.py:105-106` (HEAD) `print("api_key : ", env_config.groq_api_key)`; imported by `rag/chunking/run_indexing.py:35` | Fixed |
| S2 | Anthropic API key printed (twice, incl. `repr`) by the `core/llm.py` self-test | `core/llm.py:477-478` (HEAD) under `if __name__ == "__main__"` | Fixed |
| S3 | Secrets visible in `repr()` of config objects (tracebacks with locals, debug prints, error reporters) | `config/settings.py` `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `SECRET_KEY`, `BOOTSTRAP_ADMIN_PASSWORD`, `ELEVEN_LABS_API_KEY`, `ASSEMBLY_AI_API_KEY`, `LANGWATCH_API_KEY`, `SENTRY_DSN`; `core/configs.py` key fields; `OpenAISettings.api_key` dataclass field | Fixed (`repr=False`) |
| S4 | Interview-time RAG ran without tenant identity | Graph state had no `organization_id` (`agents/interview_agent/graph/__init__.py` HEAD:51-75, 187-210) so `_retrieve_graph_context` passed `None` | Fixed (section 5) |
| S5 | Real STT failure modes could escape normalization or submit a non-final transcript | `voice/real_providers.py` HEAD:51-56, 93-96 | Fixed locally (section 7); real API still unverified |

### Credential exposure check
- Tracked files scanned for key shapes (`gsk_…`, `sk-…`, `sk-ant-…`, `sk-proj-…`, `AKIA…`, `xox?-…`, `ghp_…`) with `git grep`: **no matches**.
- Full history of all branches scanned with `git log --all -G` for those shapes and for `*_API_KEY = <20+ chars>`: **no matches**.
- `.env.example`: all secret values are placeholders (`your-…`, `sk-...`, `sk-ant-...`, empty). `.env` is not tracked (`.gitignore:100`).
- Tracked artifacts `pytest-output.txt` (UTF-16) and `rag_evaluation_results.json`: no key material.
- **Rotation**: no real credential is present in the repository. However, S1 printed the Groq key to the console of anyone who ran the offline indexing scripts with a real key. If those runs happened in CI, shared terminals or captured logs, **rotate the Groq key** (and the Anthropic key if `python core/llm.py` was run with a real key). I did not rotate anything.

## 4. Secret leakage fix (P0.1)

**Implemented**
- `core/configs.py`: removed both import-time `print` calls; key fields (`groq_api_key`, `openai_api_key`, `openrouter_api_key`, `anthropic_api_key`, `qdrant_api_key`) now `Field(default=None, repr=False)`.
- `core/llm.py` `__main__`: prints `configured`/`missing` instead of the key.
- `config/settings.py`: secret fields use `Field(default=…, repr=False)`; types unchanged (`str` / `Optional[str]`), so every existing caller (`settings.OPENAI_API_KEY.strip()`, provider construction, voice factory) works unchanged.
- `integrations/llm_providers/provider.py`: `OpenAISettings.api_key = field(repr=False)`.
- Provider error messages were already generic (`provider.py:70, 95-131, 256`); no change needed.

**Tested offline**: `tests/unit/test_secret_redaction.py` (9 tests, synthetic sentinel only)
- subprocess import of `core.configs` with sentinel keys: stdout/stderr contain no sentinel;
- `repr`/`str` of `EnvConfig`, `Settings`, `OpenAISettings` contain no sentinel while values remain usable;
- `OpenAIProvider` errors for HTTP 401/403/429/500 (MockTransport) never contain the key;
- AST guard over all non-test sources: no `print`/logger call passes a variable whose name contains `api_key`, `secret`, `password`, `credential` or `dsn` (presence checks like `"configured" if key else "missing"` are allowed).
- Against HEAD code: 5 of 9 fail; with the fix: 9 pass.

**Found, not changed**: the offline stack shares the `LLM_PROVIDER` env var with the app but only accepts `groq|anthropic|openai|openrouter`, so `import core.configs` fails under the app default `LLM_PROVIDER=none`. Also `core/llm.py:65` makes every provider except Groq unreachable. Both are offline-indexing defects, outside this pass.

## 5. Tenant-aware interview retrieval (P0.2)

### Trace (after fix)
1. **Origin**: `tenant_context` (`services/tenant_authorization.py:111`) resolves `organization_id` from the authenticated user's *active membership*. `X-Organization-ID` only selects among memberships; an unknown or foreign org returns 404 (`:79-92`). Platform admins need an org + support reason and are audited.
2. **Interview session**: `submit_answer` → `_process_turn` loads the interview with `get_authorized(session, id, user, role, organization_id)` (`services/interview_service.py`), which filters by org.
3. **Server-controlled identity**: `_state()` (`interview_service.py:~697-713`) overwrites `organization_id` from the persisted `InterviewRecord` column. `campaign_id` is set from the column for campaign interviews and removed for non-campaign interviews, so a tampered `state_json` cannot change either. Non-campaign API responses keep their original shape (no new `campaign_id: null` key).
4. **Graph state**: `InterviewGraphState` declares `organization_id` and `campaign_id` (`agents/interview_agent/graph/__init__.py:55-56`); `handle_answer` copies them from the service state (`:194-195`). Before the fix LangGraph dropped them because they were not declared.
5. **Retrieval callback**: `_retrieve_graph_context` (`interview_service.py:672`) raises `InterviewContextUnavailableError` when `organization_id` is missing/blank (fail closed; the graph node catches it and continues with no context), else calls `retrieve_context(..., organization_id=…, campaign_id=…)`.
6. **Follow-ups**: `retrieve_relevant_questions(..., organization_id=state["organization_id"], campaign_id=state.get("campaign_id"))` (`interview_service.py:638-643`); new keyword-only params in `rag/service.py:771`.
7. **Data layer filter** (`rag/service.py` `_filtered_query` SQL and `_matches` in-memory): `organization_id is None` → only approved global demo chunks (`scope='global'`, `organization_id IS NULL`, pinned approval reference); otherwise own-org chunks OR approved global. Global ingestion is restricted to the pinned demo corpus (`_validate_corpus_ingestion_scope`), and a DB check constraint forbids `scope='global'` with an org. So global documents cannot carry tenant content.
8. **Campaign scope (new)**: `campaign_scope_key(campaign_id)` → `job_id = "campaign:<id>"` (`rag/service.py:98`). `RetrievalFilters.campaign_id` adds that key to the allowed job ids (`allowed_job_ids`, `:86`; SQL `job_id IN (...) OR job_id IS NULL`, `:526`; in-memory `_matches_job_scope`, `:895`). Campaign chunks are returned only for that campaign *and* still only within their organization.
9. **Prompt**: retrieved chunks are formatted by `format_context` with `[UNTRUSTED_CONTEXT source=… chunk=…]` attribution. Isolation does not depend on prompt text.

`campaign_id` association: set once by `start_campaign_interview` (`interview_service.py:176+`) after verifying campaign approval, reviewed application, the candidate's own user and a confirmed schedule; stored in `InterviewRecord.campaign_id` (migration `g20`).

### Tests — `tests/unit/test_tenant_interview_retrieval.py` (11 tests, real SQLite retrieval, assertions on returned document IDs)
| Requirement | Test |
|---|---|
| A gets A's docs; B gets B's; neither gets the other's | `test_each_organization_retrieves_only_its_own_documents` |
| Missing org returns no private docs (context + follow-up questions) | `test_missing_organization_returns_no_private_documents` |
| Campaign docs restricted to the authorized campaign (and tenant) | `test_campaign_documents_are_restricted_to_the_authorized_campaign` |
| End-to-end interview turn uses only its tenant's docs | `test_interview_turn_retrieves_only_its_tenant_documents` |
| Forged `X-Organization-ID` rejected; no retrieval runs | `test_forged_organization_header_cannot_reach_another_tenant` |
| Forged `organization_id`/`campaign_id` in persisted `state_json` ignored | `test_forged_state_organization_is_ignored_in_favor_of_persisted_tenant` |
| Failed-then-retried turn and completed-turn replay keep filtering; replay runs no new retrieval | `test_replayed_and_retried_turns_keep_tenant_filtering` |
| Missing tenant fails closed before any retrieval | `test_graph_context_retrieval_fails_closed_without_tenant` |
| Worker context bound to the execution tenant; mismatched tenant gets no context | `test_worker_context_is_bound_to_the_execution_tenant` |
| Worker-prepared campaign → campaign interview retrieves its campaign docs, not another campaign's or another tenant's with the same campaign id | `test_worker_prepared_campaign_interview_uses_its_campaign_documents` |

Existing test updated on purpose: `tests/unit/test_rag_semantic_retrieval.py::test_question_retrieval_reads_persisted_vectors_and_respects_job_scope` called `_retrieve_graph_context` with no tenant and expected retrieval to run. Under the new fail-closed rule that raises, so the test state now includes an `organization_id` that owns no private documents; its assertions (approved global chunks returned with attribution) are unchanged.

Negative check: with only the propagation change reverted (graph + interview service at HEAD), 5 of 11 fail. The other 6 passed already, because the data-layer filter, the tenant header check and the worker context were already correct; the tests now lock that in.

Note: the background worker never calls interview retrieval. Its tenant context comes from DB rows (`RecruitmentCampaignOrchestrator._worker_context`, which joins execution and campaign on organization).

## 6. RAG findings (P1 review — no ranking behavior changed)

Current pipeline: pinned 18-chunk demo corpus (`rag/corpus/demo_v1.json`, pre-chunked, competencies `python|databases|distributed_systems|machine_learning`, jobs `ai_engineer|software_engineer`) → `SQLAlchemyVectorStore.upsert` (JSON-text vectors) → `HybridRetriever`: BM25Okapi over the filtered rows (`rank_bm25`), optional dense cosine (OpenAI embeddings only, full Python scan), RRF (k=60), `DeterministicReranker` (0.7 × term coverage + 0.3 × score). Default `EMBEDDING_PROVIDER=none` → lexical only. Attribution: document/chunk ids in the formatted context.

Measured weaknesses (in-memory probe over the demo corpus, lexical only):

| Input (position, competency, gap) | Chunks returned | Cause |
|---|---|---|
| Software Engineer, `databases`, "concurrency" | 1 | works |
| AI Engineer, `python`, "concurrency" | 4 | works |
| Software Engineer, `databases`, "transactions" | 0 | no stemming/normalization (`transaction` ≠ `transactions`) |
| Senior Software Engineer, `databases`, "transactions" | 0 | job scope is an exact slug (`senior_software_engineer` ≠ `software_engineer`) |
| Software Engineer, `general`, … | 0 | first-turn competency `general` matches no chunk |
| Software Engineer, `Python experience`, … | 0 | campaign rubric labels never match corpus competency ids |

Confirmed defect: the SQL filter treats `competency IS NULL` chunks as generic, but `BM25Retriever`/`DenseRetriever` re-apply in-memory `_matches`, which requires an **exact** competency match. Untagged tenant documents therefore never surface once a competency is set. The isolation tests tag fixtures per competency because of this.

Other gaps: no tenant document ingestion path (API or job); offline chunking output (`rag/chunking/run_indexing.py`) is never loaded into `rag_documents`; `RETRIEVAL_STRATEGY`, `RERANKER_THRESHOLD`, `VECTOR_SIMILARITY_THRESHOLD` settings are unused; retrieved chunk ids are not persisted per turn, so evidence cannot be traced back to sources.

**Proposed bounded task (R1)**: align the NULL-competency semantics of `_matches` with SQL; normalize competency labels (map rubric competencies to corpus ids, treat `general` as unfiltered); add simple token normalization (lowercase + plural stripping); relax job scope to "exact slug OR untagged" plus a role-family alias table; persist retrieved chunk ids on the turn. Acceptance: a fixed probe set (the table above plus tenant docs) reaches ≥1 relevant chunk per row, with no tenant-isolation test regressions. No vector DB or new reranker needed for this.

## 7. STT findings (P1)

Provider selection: `voice/factory.py` → `get_provider(settings.STT_PROVIDER, "stt")`; default `fake`; `assemblylabs` → `AssemblyAISTTProvider`; unknown names raise `ValueError` (no silent fallback; tested).

Confirmed defects (HEAD):
1. `send_audio` was `async def` with no `yield`, so it returned a coroutine; `VoiceSession` does `async for … in self.stt.send_audio(...)` (`voice/session.py:78, 98`), which raises `TypeError` on the first chunk. Two existing tests had encoded the wrong contract by `await`-ing it.
2. Sessions were never removed after `end_utterance` (audio retained until socket close).
3. HTTP 403 and other 4xx escaped as `httpx.HTTPStatusError` (not normalized); invalid JSON escaped as `ValueError`.
4. A queued/processing job or missing `text` could produce a "final" `STTEvent` with empty/None text.
5. **Request shape (not fixed, unverified)**: `end_utterance` POSTs hex-encoded raw PCM as `audio_data` to `/v2/transcript`. AssemblyAI's public async API expects an uploaded `audio_url` (upload → create transcript → poll). The class docstring claimed WebSocket streaming; it is utterance-buffered HTTP. Hex encoding doubles the payload.

**Implemented** (`voice/real_providers.py`): `send_audio` is an async generator (no partials); `end_utterance` delegates to `_transcribe` and always pops the session in `finally`; 401/403 → `STT_AUTH_ERROR`, other 4xx → `STT_ERROR`; invalid JSON → `STT_ERROR`; non-`completed` status or non-string text → `STT_ERROR` (fails instead of submitting an empty answer); docstring states it is unverified. 5xx/timeouts still retried as before.

**Tested offline**: `tests/unit/test_stt_assemblyai_offline.py` (18 tests, MockTransport, no network). Against HEAD: 17 of 18 fail. Two existing tests in `tests/unit/test_voice_phase10.py` were updated from `await send_audio(...)` to `async for`. All voice suites (`test_voice_phase10`, `test_voice_session`, `test_voice_websocket`) pass.

**Blocked**: correct AssemblyAI request flow, real latency/timeout tuning and WebSocket streaming require an authorized AssemblyAI environment. The real STT integration is **not** claimed to work.

## 8. PostgreSQL and worker reliability (review)

| Area | Finding | Evidence |
|---|---|---|
| Migration chain | Linear, single head `g20_campaign_interview_linkage` (`alembic heads`, SQLite) | `alembic/versions/*` |
| PostgreSQL verification | **Not possible here**: no PostgreSQL server, `psql` or Docker on this machine. `tests/postgres/` (migrations, multi-worker reliability, harness) were not run. The last real-PG evidence is historical (`docs/TASK2_POSTGRES_MULTI_WORKER_VERIFICATION.md`, PG 16). | — |
| Claim + fencing | CAS claim issues `lease_token` + `lease_expires_at`; completion requires the same token and an unexpired lease; stale results are discarded | `recruitment_campaign_orchestrator.py:1104-1176, 1282-1313`; `candidate_communication_service.py:778-822` |
| Lease renewal | **None at the time of this report; implemented in Milestone 12** (`services/lease_renewal.py`). Lease is set only at claim (`orchestrator.py:1167`, `candidate_communication_service.py:814`). A step longer than `CAMPAIGN_WORKER_LEASE_SECONDS` (120s) is reclaimed by another worker; the first result is fenced away, so LLM work runs twice. | — |
| Retry | Max 3 attempts, exponential backoff capped; LLM timeout/rate-limit retryable, config/invalid-output not | `orchestrator.py:63, 1254-1273`; `candidate_communication_service.py:1114-1120` |
| Crash recovery | Expired leases are reclaimed; after max attempts → `needs_attention`. Worker heartbeat row exposes staleness; graceful shutdown waits for in-flight futures | `orchestrator.py:1070-1092`; `recruitment_campaign_worker.py:316-329, 371-378` |
| Idempotency | Deterministic keys `r53:invitation|availability|schedule:{application_id}`; `expected_version` optimistic checks | `orchestrator.py:529, 660, 708` |
| Unknown external outcome | A send whose lease expired mid-flight → `delivery_status=unknown` / `needs_attention`, never auto-resent; org admin reconciles. **Correction (Milestone 12):** the earlier claim of an ambiguity window was wrong. `SmtpEmailProvider._submit_message` wraps `send_message`, so any timeout/disconnect while sending is classified `unknown` → `needs_attention` (never retried); only failures before sending (connect/EHLO/STARTTLS/login) are `retryable_not_accepted`. Milestone 12 additionally classifies `SMTPSenderRefused` (pre-DATA) as rejected/retryable instead of unknown. Stable `Message-ID <communication_id@host>` is preserved. | `email_provider.py:88-139`; `candidate_communication_service.py:651-680` |
| Delivery guarantee | At-least-once processing with fencing for DB state; at-most-once-plus-reconciliation for email except the window above. **Not exactly-once.** | — |

## 9. Verification (exact commands)

Environment: Python 3.11.15 (64-bit) venv in the session scratchpad; packages from `requirements.txt` minus `sentence-transformers` and dev-only tools (`sentence-transformers` 2.2.2 pins a torch build with no Python 3.11 wheel); `fakeredis` TCP server on 127.0.0.1:6379 standing in for Redis; per-run SQLite file under `storage/` (gitignored) migrated with `python -m alembic upgrade head`; env `ENVIRONMENT=test ENABLE_DEV_ADMIN_BOOTSTRAP=false LLM_PROVIDER=none EMBEDDING_PROVIDER=none`.

| Command | Result |
|---|---|
| `pytest tests/unit/test_secret_redaction.py` | 9 passed (HEAD code: 5 failed, 4 passed) |
| `pytest tests/unit/test_tenant_interview_retrieval.py` | 11 passed (propagation reverted: 5 failed, 6 passed) |
| `pytest tests/unit/test_stt_assemblyai_offline.py tests/unit/test_voice_phase10.py tests/unit/test_voice_session.py tests/unit/test_voice_websocket.py` | 52 passed (new STT tests vs HEAD provider: 17 failed, 1 passed) |
| `pytest tests/unit -m "not live"` (full offline suite, final change set) | see section 9a |
| `ruff check` on all modified/new Python files | No new findings. All remaining findings are pre-existing at HEAD (same rule counts per file, some lower); the 4 new test files are clean. |

### 9a. Full suite
Command: `python -m pytest tests/unit -m "not live" -q -p no:cacheprovider -W ignore::DeprecationWarning --timeout=300` on a fresh migrated SQLite file.

Result: **365 passed, 4 deselected (live), 0 failed** in 381s. That is the 305-test baseline on unmodified `main` plus 22 support-audit, 9 secret-redaction, 11 tenant-retrieval and 18 STT tests.

An interim run before the final fixes had 2 failures, both caused by this pass and both resolved: (1) `_state()` added `campaign_id: null` to non-campaign responses (`test_resource_authorization`), so the code was changed to only set it for campaign interviews; (2) the fail-closed rule broke a RAG test that called retrieval without a tenant, so that test now supplies a tenant (see section 5).

## 10. Remaining production blockers

1. PostgreSQL: migrations and multi-worker tests not re-run on real PostgreSQL in this pass; CI runs SQLite only.
2. Redis: only fakeredis used here; the interview turn lock depends on Redis.
3. Worker lease renewal missing (duplicate LLM spend, longer reclaim latency).
4. ~~SMTP duplicate window~~ — corrected in Milestone 12: no such window exists; ambiguous send failures already go to manual reconciliation.
5. Real STT/TTS unverified; AssemblyAI request flow likely wrong.
6. RAG grounding effectively empty for realistic inputs (section 6); no tenant ingestion path.
7. CV retention/deletion not implemented (`candidate_store.py:129-138`).
8. CI omits campaign, scheduling, communication, availability, tenant and PostgreSQL suites.
9. Credential rotation decision for the Groq/Anthropic keys (section 3).

## 11. Recommended next milestone

**"Verified core on real infrastructure"**: add a CI job with PostgreSQL 15/16 and Redis 7 service containers that runs migrations from empty plus `tests/postgres`, the campaign/tenant/scheduling suites and the new tenant-retrieval tests. Then implement worker lease renewal (heartbeat-extended lease, still fenced by token) with a PostgreSQL multi-worker test. Retrieval quality task R1 (section 6) follows, since grounding has measurable zero-hit cases.


> **Milestone 13 note:** later status and corrections are in `docs/MILESTONE_12_REPORT.md` and `docs/MILESTONE_13_REPORT.md`; this report is a historical snapshot.
