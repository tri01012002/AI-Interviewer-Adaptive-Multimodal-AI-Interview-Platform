# Database Architecture

## Architecture

The active application path is:

```text
FastAPI -> SQLAlchemy session -> configured database
```

Interview routes retain the existing `InterviewStore` method names temporarily, but the implementation now uses SQLAlchemy and writes `InterviewRecord` plus normalized question and turn records. This compatibility layer is not the STEP 2 repository layer.

The previously documented `storage/interviews.db` file is absent from the current checkout.
The configured default database is `storage/ai_interviewer.db`; its read-only legacy-data
inventory is recorded below. Do not assume that similarly named database files in `storage/`
are authoritative application databases.

## SQLAlchemy Configuration

`services/database.py` reads `DATABASE_URL` through application settings. PostgreSQL and explicit SQLite URLs are used directly. The default local database is `storage/ai_interviewer.db` for development and tests.

Sessions are created per operation with `SessionLocal()`. `SessionLocal.begin()` is used for atomic interview writes and rolls back when an exception escapes the context. No global shared session is used.

## PostgreSQL Configuration

Set `DATABASE_URL` to a PostgreSQL URL, for example:

```text
postgresql://user:password@localhost:5432/ai_interviewer
```

The project currently uses `psycopg2-binary`. On 2026-10-13, tenant migration and API verification
was performed against a disposable PostgreSQL 15.19 container. The target was database
`tenant_verify_7d41a262bfe94604a50539e028ba018d`, role `tenant_verify`, published only at
`127.0.0.1:58601`; its data directory was container tmpfs, with no bind or persistent data volume.
The container was removed after verification.

Once Docker Engine is running, this creates a disposable PostgreSQL 15.19 container with its data
directory on tmpfs and publishes the database only on loopback. It waits for readiness and prints
the database identity before running migrations. Run from the repository root:

```powershell
$containerName = "ai-platform-tenant-migration-verify-$([guid]::NewGuid().ToString('N'))"
$password = [guid]::NewGuid().ToString("N")
$previousDatabaseUrl = $env:DATABASE_URL
docker run --rm --name $containerName `
  --tmpfs /var/lib/postgresql/data:rw,size=1g `
  -e POSTGRES_DB=tenant_migration_verify `
  -e POSTGRES_USER=tenant_verify `
  -e "POSTGRES_PASSWORD=$password" `
  -p 127.0.0.1:55432:5432 -d postgres:15.19
if ($LASTEXITCODE -ne 0) { throw "Disposable PostgreSQL container did not start" }
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
  docker exec $containerName pg_isready -U tenant_verify -d tenant_migration_verify | Out-Null
  if ($LASTEXITCODE -eq 0) { $ready = $true; break }
  Start-Sleep -Seconds 1
}
if (-not $ready) {
  docker stop $containerName
  throw "Disposable PostgreSQL did not become ready"
}
$env:DATABASE_URL = "postgresql+psycopg2://tenant_verify:$password@127.0.0.1:55432/tenant_migration_verify"
try {
  python -c "import os; from sqlalchemy import create_engine, text; e=create_engine(os.environ['DATABASE_URL']); row=e.connect().execute(text('select version(), current_database(), current_user, inet_server_addr()::text')).one(); print(row); assert row[1] == 'tenant_migration_verify' and row[2] == 'tenant_verify' and row[3] == '127.0.0.1'"
  if ($LASTEXITCODE -ne 0) { throw "Could not verify the disposable PostgreSQL target" }
  python -m alembic upgrade head
  if ($LASTEXITCODE -ne 0) { throw "Alembic upgrade failed" }
  python -m alembic current
  if ($LASTEXITCODE -ne 0) { throw "Could not verify the Alembic revision" }
}
finally {
  docker stop $containerName
  if ($null -eq $previousDatabaseUrl) {
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
  }
  else {
    $env:DATABASE_URL = $previousDatabaseUrl
  }
}
```

Do not point this command at a shared or developer database. The current pytest guard only permits
disposable SQLite databases; PostgreSQL migration and API checks need a separately isolated
PostgreSQL-aware test harness.

## Alembic Workflow

Schema changes are managed by Alembic. Application startup does not call `Base.metadata.create_all()`.

```powershell
python -m alembic upgrade head
python -m alembic current
# Only on disposable, unassigned fixtures; tenant data blocks downgrade.
python -m alembic downgrade -1
python -m alembic upgrade head
```

The current head is `g18_internal_interview_scheduling`, after
`g17_candidate_availability`, `g16_candidate_communications`, `g15_candidate_intake_screening`, `g14_recruitment_campaign_dispatcher`,
`g13_recruitment_campaign_orchestration`, `g12_recruitment_campaign_foundation`, and
`f29_required_audit_request_ids`. Downgrade is only for
disposable, unassigned fixtures: `d3f7_tenant_isolation` refuses to run when tenant ownership or
support-audit data exists.

## Schema Overview

- `users`: authentication records.
- `candidates`: candidate records with optional recruiter owner and candidate account link.
- `interviews`: core interview record; `state_json` remains as a compatibility snapshot.
- `interview_questions`: normalized question history.
- `interview_turns`: normalized answer-turn records with unique interview/turn and interview/sequence indexes.
- `interview_evidence`: minimal future evidence storage.
- `interview_competency_state`: minimal future competency state storage.
- `rag_documents`: versioned, active/inactive retrieval chunks, scope metadata, and optional embedding vectors.
- `access_audit`: append-only durable audit events for privileged access and significant campaign
  approval/execution transitions.
- `recruitment_campaigns`: tenant-scoped recruitment plans and explicit approval decisions.
- `recruitment_campaign_executions` and `recruitment_campaign_execution_steps`: durable campaign
  execution status, step inputs/results, bounded attempts, and restart-recovery leases.
- `recruitment_applications`: one application per campaign/candidate, tenant-consistent foreign
  keys, durable processing status, bounded attempts, retry scheduling, and lease fencing.
- `candidate_cv_documents`: private-storage metadata, content hash, parser status/version, parsed
  fields, and extracted text. The database never stores the uploaded binary.
- `candidate_screenings` and `candidate_criterion_assessments`: immutable rubric snapshot/hash and
  criterion-level evidence, uncertainty, scores, and human review state.
- `candidate_availability_requests`, `candidate_availability_responses`, and
  `candidate_availability_events`: tenant-consistent requests and versioned candidate preferences,
  with append-only state events and bounded worker expiration. Instants are stored in UTC;
  timestamps must carry an explicit offset that agrees with the retained IANA timezone. Intervals
  use half-open `[start, end)` semantics, and a submitted preference is never a booking.

Availability requests follow `pending_response -> responded -> reviewed`, with either pending or
responded requests expiring at their deadline and eligible open/reviewed requests cancellable by a
recruiter. Loss of application or campaign eligibility cancels the request. Candidate updates are
versioned and accepted only before review; each submission idempotency key is retained separately.
The existing campaign worker performs a bounded expiration/eligibility sweep only while unpaused.
Requests are limited to 10 windows, 15-240 minute interview durations, deadlines 5 minutes to 30
days ahead, and proposed slots no more than 90 days ahead. Recruiters create/list by campaign and
review/cancel by request; authenticated candidates read and submit only for their owned application.

`interview_schedules`, `interview_schedule_reservations`, and `interview_schedule_events` persist
internal interview proposals, active resource claims, and transition history. Their state flow is
`proposed -> pending_confirmation -> confirmed_internal`; rescheduling creates a separate
`reschedule_pending` replacement while the existing confirmed reservation remains active. On
successful replacement confirmation, the replacement claims are written before the old claims are
released, within one transaction. Failed replacement confirmation leaves the original reservation
unchanged. Cancellation immediately releases claims; an expired reschedule leaves the original
reservation active. Proposals and replacement requests expire after seven days unless confirmed.
`needs_attention` indicates that the expiration sweep found an inconsistent
active-claim set and will not silently free or overwrite it.

Only approved campaigns with eligible applications and recruiter-reviewed candidate preferences
can be scheduled. The chosen preference supplies the UTC interval, duration, and retained IANA
display timezone. Intervals are half-open `[start, end)`. Candidate, interviewer, and explicit
exclusive-resource claims are tenant-scoped; room/resource identifiers are not global. The
`interview_schedule_mutex` row is updated in the same transaction before checking overlapping
claims, serializing reservation, replacement, cancellation, and expiration writes across workers.
Database partial unique indexes additionally limit an application to one confirmed internal
schedule and one pending replacement. `confirmed_internal` is only an internal database
reservation, not an external calendar booking or meeting. APIs are available under
`/api/v1/interview-schedules`, campaign schedule routes, and the reviewed-availability
`schedule-slots` route. The existing campaign worker expires bounded pending schedules only while
unpaused. No calendar, email, meeting-provider, or live-AI integration is invoked.

Recruitment campaign execution transitions are deterministic:

```text
pending -> running -> completed
                   -> waiting_for_approval -> pending (after re-approval)
                   -> retryable_failure -> running
                   -> needs_attention
running (expired lease) -> running (resume)
```

A campaign has at most one campaign-plan execution record and one `job_design` step for that
execution. Candidate applications are separate durable work items claimed by the same database
dispatcher; their application-specific statuses do not create a second general-purpose workflow
engine. A database lease is claimed atomically before processing, and expired application leases
are eligible for recovery. Provider-created artifacts move the campaign back to pending approval.
`LLM_PROVIDER=none` never fills missing artifacts with fabricated text; it leaves an actionable
`needs_attention` result instead.

## Candidate CV intake and screening (implemented)

An invited candidate must redeem their invitation and be authenticated as the candidate linked to
an active organization membership. They may submit one PDF or DOCX CV to an approved campaign:

```text
POST /api/v1/recruitment-campaigns/{campaign_id}/applications/cv
GET  /api/v1/recruitment-applications/{application_id}
POST /api/v1/recruitment-applications/{application_id}/review
```

Uploads are size-limited, checked against the file signature and document structure, assigned a
generated storage key, and written under `CV_STORAGE_PATH`; the client filename is never used as a
path. The current local storage adapter is for development/testing only. A production deployment
must configure a persistent, private, access-controlled storage volume shared by the API and worker,
with its own backup, encryption, and retention controls. The CV directory is not mounted as public
static content. The API reads no more than `CV_MAX_UPLOAD_BYTES + 1` bytes; production ingress
must also enforce a multipart request-body limit before spooling uploads. Application audit records
contain identifiers and actions, not file content. No automatic CV deletion schedule is currently
implemented; deployments must define and enforce an approved retention/deletion policy before
production use.

The database worker parses PDF/DOCX text using deterministic local libraries and screens only
against a compatible approved rubric. A screening rubric criterion must provide a stable id/title,
explicit `evidence_terms`, and a numeric `score_scale` containing `minimum`, `maximum`, and
`score_when_evidenced`. The immutable rubric JSON is retained with a SHA-256 rubric version.
Explicit matching terms may produce a rubric score; absence produces a null score and is explicitly
not treated as proof that a candidate lacks a skill. Protected-characteristic or proxy criteria and
incompatible rubrics stop processing for correction. Every assessment requires human review; the
review endpoint can confirm/correct criterion assessments but does not make hiring or rejection
decisions.

The parser extracts skills/technology mentions, date-bearing employment lines, education lines, and
project-section text heuristically. This is not a full CV understanding system. Extracted text is
stored in the tenant-protected database for evidence offsets and audit reproducibility, and must be
handled as sensitive personal data under the deployment's retention policy. CV instructions are
treated as untrusted content; this baseline does not send CV text to an LLM or any external service.
The dispatcher is at-least-once: expired work may be parsed more than once, while claim-token
fencing and unique application/screening constraints prevent stale or repeated persisted results.
This does not guarantee exactly-once parsing.

## Candidate communications (R4.1)

Communications are durable, tenant-scoped records tied by composite foreign keys to the same
campaign and application. Drafts use an organization-scoped idempotency key, preserve the exact
plain-text template snapshot/version, derive the recipient from the application candidate, and do
not update application status, screening scores, or review outcomes.

```text
POST /api/v1/recruitment-applications/{application_id}/communications
GET  /api/v1/candidate-communications/{communication_id}
POST /api/v1/candidate-communications/{communication_id}/submit
POST /api/v1/candidate-communications/{communication_id}/approve
POST /api/v1/candidate-communications/{communication_id}/retry
POST /api/v1/candidate-communications/{communication_id}/cancel
POST /api/v1/candidate-communications/{communication_id}/reconcile
```

The communication state machine is `draft -> awaiting_approval -> approved -> sending`, then
`simulated`, `sent`, `retryable_failure`, or `needs_attention`. A definitive rejection ends in
`needs_attention` without an unknown delivery status; only reliably not-accepted outcomes can be
retried. Pre-send states and known non-accepted attention states can be cancelled; unknown outcomes
require reconciliation first. Only organization administrators (or an explicitly scoped platform
administrator) may approve. The existing database worker dispatches approved communications under
its pause, concurrency, and lease controls, and rechecks campaign approval immediately before
delivery.
Expired `sending` claims become `needs_attention` with an unknown delivery outcome; they are never
blindly resent. Transient failures use the bounded worker retry policy and retain the same
communication/idempotency identity.

The provider boundary defaults to `EMAIL_PROVIDER=offline_simulation`, which never makes network
calls and terminates in `simulated`; `sent_at` stays unset. `EMAIL_PROVIDER=smtp` explicitly selects
the STARTTLS SMTP adapter and requires `EMAIL_SMTP_HOST`, `EMAIL_SMTP_USERNAME`,
`EMAIL_SMTP_PASSWORD`, and `EMAIL_SMTP_FROM`. Real SMTP is rejected in `ENVIRONMENT=test`; missing
credentials and unknown provider names raise configuration errors rather than falling back. The
adapter has no provider idempotency support. It receives a stable internal communication UUID as
the logical key, but SMTP does not deduplicate it; provider-side key scope and retention do not
apply.

`sent` means the provider reported acceptance (for SMTP, acceptance of the message by the SMTP
server after submission), not delivery to the candidate's inbox. Explicit SMTP rejection before
acceptance is either retryable (`4xx`) or definitive (`5xx`). A connection failure before message
submission is retryable; timeout/disconnect or any other ambiguous exception after submission
begins becomes `needs_attention`/`unknown` and is never automatically retried. Expired leases use
the same unknown path and stale workers cannot commit after their token expires or is replaced.
Organization administrators can use `/reconcile` to record an authoritative `accepted`,
`not_accepted`, or `unresolved` determination. An accepted decision records `sent`; not-accepted
permits normal bounded retry only while campaign approval remains active; unresolved keeps the
record in `needs_attention`. This is an audited human reconciliation, not a provider lookup or
proof of exactly-once delivery.

Approval and tenant/application scope are rechecked immediately before the provider call, and
campaign approval is checked again before outcome persistence. Preflight failure cancels the
undelivered attempt. Cancellation is available only before a send is claimed; a sending attempt
cannot be recalled. An unknown outcome cannot be cancelled or retried until an administrator
reconciles it. If campaign approval is revoked after submission begins, an explicit provider
acceptance is still recorded as `sent` with an audit marker; ambiguous outcomes remain
`needs_attention`/`unknown`. Audit/events contain only allowlisted provider metadata and state; they
never include credentials, recipient addresses, or message bodies. Recipient addresses and rendered
messages are stored in the application database without field-level encryption; production use
requires database encryption at rest, strict access controls, and an enforced retention/deletion
policy. Templates use plain-text `string.Template` substitution with a strict variable allowlist; no
code evaluation, HTML rendering, or external resource loading occurs. SMTP is not enabled by
default and is disabled entirely in test mode.

## Retrieval corpus and embeddings

The RAG MVP stores document chunks and embedding vectors in the configured SQLAlchemy database.
Vectors are JSON-serialized float arrays with model, version, and dimension columns; dense retrieval
uses exact cosine scoring after SQL metadata filtering. This is durable and works with the existing
SQLite development database and PostgreSQL without requiring an extension, but it scans matching
vectors in application code and is intended for the small demo corpus, not a large production index.
Job and optional organization identifiers are metadata filters. When a request has no job or
organization scope, rows bound to either scope are excluded; only explicitly global corpus rows can
be used. Supplying one scope still excludes rows bound to a different scope.
Organization filters are now derived from verified tenant context or persisted interview tenant
state. Explicitly global corpus rows remain shareable; tenant-bound rows cannot be selected merely
by supplying an organization ID.
The configured backend name is `sqlalchemy_json`. The former `supabase` value is treated as a
deprecated alias and logs a warning; it does not mean that Supabase or pgvector is being used.

The checked-in demo corpus is synthetic and includes interview questions, competency descriptions,
rubrics, follow-up guidance, and general technical knowledge. To apply the schema and reproducibly
index it:

```powershell
python -m alembic upgrade head
python -m rag.ingest
python -m rag.run_evaluation
```

Set `EMBEDDING_PROVIDER=openai`, `OPENAI_API_KEY`, `EMBEDDING_MODEL` (default
`text-embedding-3-small`), and `EMBEDDING_DIMENSION` (default `1536`) to index real semantic
vectors. If credentials are absent, corpus text is still indexed for BM25 and retrieval results
explicitly report `semantic_embeddings_unconfigured_lexical_only`. An embedding timeout or invalid
vector also yields explicitly labeled lexical-only retrieval; it does not produce synthetic dense
scores. The benchmark reports Recall@5 and MRR@5 against
`rag/corpus/retrieval_eval_v1.json`; without credentials it measures only the lexical fallback and
does not claim semantic quality.

The normalized tables contain foreign keys for interview-to-question, interview-to-turn,
evidence-to-interview, evidence-to-turn, and competency-state-to-interview relationships. Tenant
ownership is represented by organizations, organization memberships, and nullable organization
references on candidates, interviews, invitations, and audit records. Composite foreign keys
constrain candidate/interview ownership and recruiter membership where applicable.

## Live AI validation and release boundary

Live provider checks are opt-in and use synthetic answers only. Configure `LLM_PROVIDER=openai`,
`OPENAI_API_KEY`, `OPENAI_MODEL`, `EMBEDDING_PROVIDER=openai`, `EMBEDDING_MODEL`, and
`EMBEDDING_DIMENSION`. Then run:

```powershell
$env:RUN_LIVE_AI_TESTS = "1"
python -m pytest -q tests/unit/test_live_provider_smoke.py
python -m rag.run_evaluation
```

The complete live interview workflow additionally requires `RUN_LIVE_AI_E2E=1` and
`LIVE_AI_E2E_DATABASE_URL` pointing to a dedicated SQLite database file whose name contains
`live_ai_e2e_`. It applies Alembic migrations, idempotently ingests the synthetic corpus, bounds
individual provider calls to ten seconds, and has a five-minute total budget:

```powershell
$env:RUN_LIVE_AI_E2E = "1"
python -m pytest -q tests/integration/test_live_interview_e2e.py
```

These live commands must not be used with production or shared databases. Credentials are read
from environment variables or an untracked local `.env`; they must not be added to source control.

## Resource authorization and release boundary

The `a4d2_resource_ownership` migration adds nullable `candidates.owner_user_id` for the recruiter
who created/manages a candidate, nullable unique `candidates.user_id` for an explicitly
self-created candidate profile, and durable `access_audit` rows. Existing records are deliberately
left unassigned by the migration; they are not attributed to a recruiter based on email or other
inferred identity and remain accessible only to an explicit admin until an operator completes a
separate, controlled assignment process.

Candidate profiles, interviews, reports, and invitation operations are organization-scoped. A
candidate can access only their own organization-private profile and associated interviews.
Recruiters can access organization resources only when the existing owner checks also authorize
them; they cannot submit candidate answers. Organization admins manage recruiter memberships but
do not access candidate/interview/report data. Platform-admin support access is read-only, requires
an explicit target organization and reason, and is audited. HTTP and WebSocket interview paths
require verified tenant context before returning interview data or accepting actions. Existing
per-user ownership checks remain an additional security layer. Public registration cannot assign
admin privileges, and production startup skips development database initialization and admin
bootstrap.

Legacy resources with no tenant assignment remain inaccessible to ordinary tenant users. No
shared default organization or automatic ownership inference was used.

## Multi-Tenant Architecture Audit and Implemented Model

The following access-path audit describes the pre-tenant implementation and records the controls
that now close those paths.

### Existing access paths and boundary

- `users` remains a global identity table. `organization_memberships` assigns a verified,
  organization-scoped role; users may have memberships in multiple organizations.
- `candidates` now has nullable `organization_id`, `owner_user_id`, and candidate-account
  `user_id`, with organization-scoped email uniqueness and membership constraints. Candidate
  profiles are created through invitation redemption, not direct profile creation.
- `interviews` now carries nullable `organization_id` and is constrained to the same organization
  as its candidate. Interview create/read/list/answer/result and report routes use verified tenant
  context plus the existing `InterviewService`/`InterviewRepository` ownership checks. WebSocket
  authorization resolves the same organization context before returning data or processing voice.
- Candidate invitations may be issued before a profile exists and are scoped to an organization,
  recruiter membership, and intended email. Tokens are hashed, expire after 72 hours, are consumed
  once, and reject reuse, revocation, expiry, and verified-email mismatch.
- Questions, turns, evidence, assessments, and competency state remain interview children.
  Report generation and export require an authorized interview and organization context; the
  report service also scopes persisted data by candidate, interview, and organization.
- The worker task module remains empty. No queued report or interview workflow was found.
- Recruitment campaign execution uses a standalone database-polling worker
  (`python -m services.recruitment_campaign_worker run`), not Celery or Redis. It scans
  approved campaigns, creates one persisted execution per campaign, and claims due work with
  the existing conditional lease update. The `g14_recruitment_campaign_dispatcher` migration
  adds a durable retry availability time and a singleton pause/heartbeat control row.
- Worker controls are local operational commands, not campaign-user APIs:
  `python -m services.recruitment_campaign_worker pause|resume|status`. `status` reports the
  last heartbeat, execution counts, due retries, and expired leases. Pause prevents both
  background dispatch and API-triggered execution; requests remain persisted as pending.
  SIGINT/SIGTERM stop new dispatch and wait for in-flight tasks; unfinished leases expire for
  recovery.
- Start after applying migrations with `python -m alembic upgrade head`, then run
  `python -m services.recruitment_campaign_worker run`. Configure
  `CAMPAIGN_WORKER_CONCURRENCY` (per process, 1-32) and
  `CAMPAIGN_WORKER_POLL_SECONDS` (0-60 seconds) through the environment. The Compose worker
  uses the same command and has a heartbeat-backed health check.
- Retryable failures use three total attempts with exponential delays (default 5s then 10s,
  capped at 300s). Permanent failures and exhausted retries enter `needs_attention`.
- Dispatch is at-least-once. Database claims prevent concurrent active processing and result
  plus campaign-artifact persistence is transactional and idempotent. A crash before commit can
  repeat a provider call after lease expiry; Job Design has no external side effects, but a paid
  LLM request may be duplicated. No exactly-once provider-call guarantee is made.
- Redis availability is not required by the campaign worker. Database outages are logged and
  retried at the bounded polling interval; worker status becomes unhealthy when its heartbeat
  goes stale.
- `rag_documents.organization_id` remains optional retrieval metadata, not an authorization
  boundary by itself. The application supplies verified tenant context and persisted interview
  scope; explicitly global rows remain shareable.
- `access_audit` records actor, target/requested organization, action, resource type, resource ID,
  outcome, and support reason. Support access fails closed if its audit write fails.
- Platform-admin support reads and report exports persist an `attempted` audit intent before
  protected resource reads or report generation. A failed audit commit stops the request before
  that work begins. The event means an attempt was durably recorded; it does not assert that
  authorization ultimately succeeded or that an export completed.

The original user/candidate/interview owner checks did not establish an organization boundary.
Knowing a resource ID still never substitutes for verified membership. Tenant authorization now
scopes candidate CRUD, interview lifecycle and history, invitation issue/revoke/redeem,
result/report retrieval, WebSocket actions, and interview RAG retrieval. Candidate email
verification is account-level; invitation redemption additionally validates the invitation's
organization. Platform-admin support access remains explicit and audited.

### Mandatory audit failure semantics

`access_audit` is mandatory before platform-admin HTTP support access, platform-admin WebSocket
interview access, and report export. HTTP support middleware writes and commits an `attempted`
record before calling the route. Report routes for other authorized tenant actors do the same
before loading interview data. WebSocket support persists the attempt after tenant/support context
is resolved and before loading the interview or sending interview content. If the preflight cannot
commit, HTTP requests return 503 and WebSockets close with code 4503; protected reads and report
generation are not entered. These statuses indicate refusal before protected work, not rollback of
work that already occurred.

An audit intent is deliberately not rewritten to `allowed`, `denied`, or `completed` afterward.
That avoids claiming a final outcome when the process can fail between the protected operation and
a second audit write. Consequently, an `attempted` row proves only that the preflight transaction
committed; it does not prove the route completed. Operational reporting must label it as an
attempt, and investigations that need final outcomes must correlate ordinary HTTP/WebSocket
telemetry without treating that telemetry as the durable audit record.

Organization creation, recruiter membership changes, and invitation lifecycle operations that
already add an audit row in the same database transaction as their mutation retain that boundary:
an audit insert/commit failure aborts the transaction and the mutation. A synthetic rollback test
guards this property. No HTTP 503 response is treated as proof that a previously committed
transaction or filesystem operation was undone.

Report files are not part of a SQL transaction. Their content is first written and flushed to a
temporary file in the report directory and then promoted with an atomic replace. A write/promotion
failure leaves no partial final report, but the durable audit row remains an `attempted` event.
Atomic rename is limited to the filesystem's same-directory guarantees; it does not make report
creation atomic with the database or with the HTTP response.

The optional `Idempotency-Key` header is scoped by actor, method, path, and organization, then
stored as a SHA-256 request identifier under a unique index. Reuse of a key returns 409 before
protected work; the server does not replay a prior report response. Requests without the header
receive unique audit request identifiers and are independently recorded. Clients that may retry
report exports should reuse the same key. Concurrent duplicate-key races fail closed if the unique
index rejects the second intent. The request identifier is a deduplication key, not proof of client
identity or authorization.

The additive `f29_required_audit_request_ids` migration must be applied before deploying this
application version. The code intentionally has no fallback when the column/index is absent:
required access and report operations will fail closed. Do not run this migration as part of the
test-only validation. Monitor required-audit 503 responses, WebSocket 4503 closes, database errors,
and rates of `attempted` events; alert on sustained failures and investigate audit-table growth
under the normal retention and access-control policy. Audit failure logs contain only a safe action
label and exception class, not resource identifiers, support reasons, or report content.

### Approved tenant model

The organization-level model below was approved before schema work and is the implemented
authorization boundary.

1. **Organizations and lifecycle:** Add an organization entity with a server-generated ID and
   explicit active/suspended state. Confirmed: only existing platform administrators may create
   organizations and grant the initial organization-admin membership. Perform both writes
   transactionally and audit them; enforce this in the backend and reject unauthenticated and
   ordinary-user attempts. Do not support self-service organization registration or
   client-supplied privilege assignment. Suspend rather than hard-delete an organization with
   retained business records.
2. **Membership:** Add a membership relation between users and organizations, with a
   membership-scoped role and active/revoked state. Confirmed: a user may have memberships in
   multiple organizations with a separate role in each; one membership has one effective role,
   and recruiter/candidate permissions are not implicitly combined. If multiple operational roles
   are needed in one organization later, use an explicit, audited role-assignment design.
   Organization admins may add/revoke recruiter memberships only in their own organization;
   they cannot grant platform-admin or organization-admin privileges. Recruiters cannot manage
   recruiter memberships. Candidate memberships and profiles are created only by successful,
   validated invitation redemption. Only platform administrators may create organizations and
   grant the initial organization-admin membership. Enforce and audit each operation on the
   backend. Authorization uses the selected organization's active membership, not the account's
   global recruiter/candidate role. Platform administrators retain support access only with an
   explicit target organization and recorded reason; provisioning and support permissions remain
   separate. Audit successful and denied support access with actor, target organization, action,
   timestamp, and reason. Do not give platform administrators implicit access to every tenant.
   Keep audit records append-only and protect them from unauthorized updates/deletes without
   recording tokens or sensitive interview content.
   Organization admins may not read candidate or interview data by default; they manage
   organization settings and recruiter memberships only. Candidate/interview/results/reports stay
   scoped to the assigned recruiter, candidate, or explicit platform-admin support access.
3. **Organization context:** Treat an `X-Organization-ID` header as a selector, never as proof of
   access. Confirmed: omission is allowed only when the authenticated user has exactly one active
   organization membership. With multiple active memberships, require an explicit selector and
   verify it against the authenticated user on every request. Reject inactive, revoked, or
   unauthorized memberships. Resolve tenant context before any tenant-scoped resource access;
   scope reads, writes, invitations, interview/evaluation operations, and reports. For WebSockets,
   use an equivalent selector and perform the same checks. Do not trust an organization ID from a
   request body, JWT claim, or interview `state_json`. Return the same not-found response for an
   absent resource and a resource outside the caller's tenant to avoid cross-tenant existence
   leaks.
4. **Candidates across organizations:** Confirmed: use one global login identity with a
   separate, private organization-owned candidate profile per organization. A candidate invited
   by two organizations has two isolated profiles and memberships; profile details, interviews,
   reports, and invitation state are not shared between organizations. A global account may be
   created before an invitation, but an organization-scoped candidate profile is created only
   after a valid invitation is redeemed and the authenticated account's verified email matches
   the intended identity. Redemption creates the profile and candidate membership atomically.
   Link each profile to the same global user only after verified invitation redemption. This
   requires replacing global candidate-email uniqueness with organization-scoped uniqueness and
   allowing one user to link to multiple candidate profiles. Do not create a profile in another
   organization based only on matching email.
5. **Interviews and invitations:** Store tenant ownership explicitly on candidate profiles,
   interviews, and invitations. An interview's organization must equal its candidate profile's
   organization, and its recruiter owner must have an active membership there. Keep existing
   owner-user checks as an additional requirement. Invitation issuance must be possible before a
   candidate profile exists: add a tenant-scoped issuance operation that records the organization,
   intended verified-account email, recruiter, expiry, and token hash. On redemption, revalidate
   the active organization and recruiter membership, verified account email, token expiration,
   revocation, and prior consumption. In one transaction, create the candidate membership, create
   and link the organization-scoped candidate profile, and consume the invitation. Preserve the
   existing candidate-ID redemption response where possible; replay and mismatched identity remain
   rejected. Invitation revocation must require the same organization and recruiter membership.
6. **Child resources, reports, and RAG:** Keep interview child rows reachable only through an
   authorized interview, and strengthen relational constraints for interview/candidate and
   interview/child consistency where supported by both databases. Scope report reads through the
   authorized interview and include organization context in audit records. Resolve RAG scope from
   the verified request context and persisted interview tenant, never caller-controlled state.
   Preserve only explicitly classified global corpus rows as shareable; validate organization
   corpus assignments during ingestion. There are no background jobs today; any future job must
   carry the organization and actor IDs and revalidate ownership before reading or exporting.
7. **Constraints and indexes:** Add foreign keys for membership-to-user/organization, candidate
   profile-to-organization, interview-to-organization/candidate, invitation-to-organization/
   recruiter membership, and tenant-aware audit rows. Add unique membership constraints,
   organization-scoped candidate email uniqueness, composite uniqueness needed by tenant-aware
   foreign keys, and indexes beginning with organization ID for tenant-scoped list/look-up paths.
   Add pending-invitation uniqueness on organization and normalized intended email. Preserve
   existing turn and invitation expiry/one-time-use semantics.

### Implementation and verification status

The approved model has been implemented in migration `d3f7_tenant_isolation` and the tenant-aware
authorization, organization-management, candidate-invitation, interview, report, RAG-context,
support-audit, and WebSocket paths. The migration adds organization/membership tables and nullable
tenant references with scoped constraints; it does not assign existing records to an organization.
Unassigned legacy rows remain inaccessible to ordinary tenant users.

Validation on this implementation:

- The full `tests/unit` suite passed: **154 passed, 4 skipped**.
- The suite used a fresh disposable SQLite database with offline AI defaults
  (`LLM_PROVIDER=none`, `EMBEDDING_PROVIDER=none`); no external AI API was called.
- Cross-tenant authorization, invitation, replay/expiry, and migration regression tests are
  included in the passing unit suite.
- PostgreSQL 15.19 verification on a fresh, disposable container completed. The full Alembic
  upgrade reached `d3f7_tenant_isolation (head)`. Inspection confirmed tenant tables, expected
  candidate/interview/invitation/audit foreign keys and constraints, tenant-scoped indexes, the
  pending-invitation partial unique index, and the append-only audit trigger; relevant tables were
  empty immediately after migration.
- **13 existing API test functions passed** against PostgreSQL through `TestClient` in a separate
  direct runner, plus **1 explicit cross-tenant invitation-denial scenario**. Coverage included
  organization provisioning and membership context; cross-tenant access; invitation redemption,
  expiry, reuse, revocation, and concurrent redemption; interview ownership and candidate access;
  report export; support audit; WebSocket authorization; and answer idempotency. The runner used
  `LLM_PROVIDER=none` and `EMBEDDING_PROVIDER=none`; no external AI API was called. It did not run
  pytest, and the SQLite-only `tests/conftest.py` safety gate was not changed or bypassed.
- PostgreSQL downgrade refusal and the append-only audit trigger were tested with populated tenant
  data: both behaved as intended and Alembic remained at head. The first downgrade probe exposed
  PostgreSQL syntax errors in two duplicate-detection subqueries that omitted aliases. Explicit
  aliases were added; the guarded downgrade then passed. PostgreSQL transactional DDL kept the
  migration revision at head after the failed probe.
- A disposable SQLite regression now verifies that downgrade refuses to remove populated
  organizations and memberships. The migration test module passes **2 tests** after this guard
  and the PostgreSQL SQL fix.
- Live-provider end-to-end validation remains opt-in and was not run.

### Legacy-data and deployment plan

No safe tenant assignment can be derived from the current schema alone. `owner_user_id` identifies
a recruiter, not an organization; one recruiter may serve multiple organizations. Candidate email
and interview IDs are not evidence of organizational ownership. Candidate, interview, invitation,
audit, and organization-tagged RAG records therefore require an explicit, reviewed mapping from
an authoritative source. Do not create a shared default organization or infer assignment from
email, names, or current owner IDs.

### Local legacy-data inventory (read-only snapshot, 2026-10-13)

This inventory covers only the configured default `storage/ai_interviewer.db`; the effective
application setting resolved to SQLite database `./storage/ai_interviewer.db`. The inventory
opened that explicit file using SQLite read-only URI mode and `PRAGMA query_only=ON`. The database
is pre-tenant: it has no
`alembic_version`, `organizations`, `organization_memberships`, or `candidate_invitations` table,
and candidates, interviews, and audit rows have no tenant columns. No migration or data update was
run against it. The absence of invitation tables means this snapshot contains no invitation rows;
it does not establish the state of other database files or deployment environments. Seven other
SQLite files are present under `storage/`; their provenance and relationship to deployed data were
not established, so they were not opened or included in these counts.

Aggregate counts only (no candidate or user values were read into the report):

- Users: **89** global identities; these are not organization memberships.
- Candidate profiles: **2**, with no tenant, recruiter-owner, or linked-user columns.
- Interviews: **301**, with no tenant assignment. Their **64** distinct `candidate_id` values
  have no match in the two candidate profiles (**301/301** interview-to-profile links are orphaned;
  the legacy schema declares no candidate foreign key). **299** interviews have a non-null
  `owner_user_id`: **281** resolve to a user and **18** do not; the other **2** have no owner.
  A valid user reference still does not establish that user's organization or active recruiter
  membership. Related rows comprise **502** questions, **236** answered turns, **244**
  assessments, **148** evidence records, and **216** competency-state records. Their checked
  parent links all resolve, so these children can be traced to an interview, but cannot be tenant
  mapped until that interview's candidate/profile and ownership relationships are reconciled.
- Access-audit rows: **16**, with no tenant metadata in this schema. All recorded actor references
  resolve to a user, but actor and resource IDs alone do not prove an organization or authorize
  tenant assignment.
- RAG documents: **18**; the table has no organization column and **0** non-empty embeddings.
  Their global-versus-tenant classification is therefore unverified and must come from approved
  corpus provenance, not be inferred from the missing column.
- Stored report exports: **70** valid `report_*.json` files in the configured storage directory.
  A read-only reconciliation parsed linkage fields in memory and emitted aggregate counts only:
  **16** `(interview_id, candidate_id)` pairs match fields on a persisted interview, **54** exports
  reference no persisted interview, and **0** have a mismatched pair among the 16 matches. None of
  the 16 matched interviews links to a candidate profile in this database. Thus **0** exports are
  currently proven to belong to an authorized tenant interview; do not serve or assign any export
  based on this structural match alone.
- Structural integrity: **18** SQLite foreign-key violations, all interview owner references to
  missing users. Additionally, all **301** interview-to-candidate links are orphaned because that
  legacy relationship has no declared foreign key. The checked question/turn/evidence/assessment/
  competency-state parent relationships and audit-actor references had **0** missing parents.
  The 18 owner violations and 301 candidate-link orphans are distinct findings.

No authoritative ownership mapping was available in the repository. The reusable dry-run tool
described below was added after this inventory and has been tested only with synthetic fixtures,
not against this database or a disposable copy. No candidate names, emails, answer text, report
contents, or record identifiers were emitted. Seven other SQLite files remain uninspected because
their provenance and relationship to deployed data are unknown. This is a scoped local snapshot,
not a complete production inventory.

### Mapping register and read-only dry-run contract

`tools/legacy_tenant_mapping_dry_run.py` is a standalone, aggregate-only SQLite inventory and
mapping-proposal checker. It requires `--source-db`, does not consult `DATABASE_URL` or application
defaults, opens the named source with SQLite read-only URI mode and `PRAGMA query_only=ON`, and has
no write, migration, or apply operation. It does not modify source files. It requires the supported
legacy table schema; schema and integrity failures produce a sanitized error and a non-zero exit
status. Output includes totals and status counts only; it does not print database paths, source
identifiers, candidate/user content, export contents, or mapping evidence references.

Create an explicitly named disposable copy from an approved, consistent snapshot in a protected
review location. For a live SQLite database, stop writes or use SQLite's online backup API so WAL
content is included; do not copy only the main database file while writers are active. The
following PowerShell example creates a new copy without opening the source for writes and refuses
to reuse an existing destination:

```powershell
$source = "D:\approved-snapshot\legacy.db"
$copy = "D:\tenant-review\legacy-copy.db"
if (Test-Path $copy) { throw "Choose a new disposable-copy path" }
python -c "import pathlib,sqlite3,sys; s=sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri()+'?mode=ro',uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $source $copy
if ($LASTEXITCODE -ne 0) { throw "SQLite backup failed" }
```

Run the inventory from the repository root, always naming the copy explicitly. Add
`--reports-dir` only when report exports from the same snapshot are available:

```powershell
python -m tools.legacy_tenant_mapping_dry_run `
  --source-db "D:\tenant-review\legacy-copy.db" `
  --reports-dir "D:\tenant-review\reports"
```

The JSON report contains `resources` totals and `mappable`, `unmapped`, `ambiguous`, and
`structurally_inconsistent` counts; `relationships` has aggregate broken-link/reconciliation
counts; `integrity` has the SQLite integrity result, foreign-key violation totals by table, and a
pass flag. `rag_provenance` stays `unknown` unless scope is established by an approved mapping.
`reports_scanned` indicates whether a report directory was supplied, while report counts are in
`resources.report_exports`. Exit status is `0` when integrity, relationship, and ambiguity checks
pass; expected unmapped rows alone do not fail integrity. A source/schema error or any failed
integrity check returns non-zero. A disposable-copy inventory is validated by reviewing its
aggregate totals and requiring `integrity.passed: true`; compare those totals with the approved
snapshot/backup inventory before using them for a mapping review.

An optional mapping proposal must be strict JSON; CSV is rejected. It includes format version 1,
approval metadata, organization references, and record entries containing a category, stable source
record ID, evidence reference, and organization ID (or a RAG scope). Membership entries also
identify the role. First run with `--mapping` only: the report returns the exact file's SHA-256
digest and leaves its records unapproved/unmapped. Review the actual source evidence and obtain
separate data-owner approval; record the approved digest in the controlled approval process, then
rerun with `--approved-mapping-sha256 <digest>`. A matching digest is only an exact-file
attestation: the tool cannot verify the authority, approval identity, or correctness of referenced
evidence. Text fields in the JSON do not establish approval by themselves. Do not provide the
digest until independent review and approval are complete. Duplicate/conflicting entries,
unresolved source IDs, unknown organizations when the source has an organizations table, and
incompatible candidate/interview/recruiter membership relationships cannot be counted as
`mappable`.

The tool traces normalized interview children through their interview but does not consider parent
linkage proof of tenant ownership. Report exports are read in memory and reconciled using their
interview/candidate linkage fields; matched fields alone do not prove authorization. RAG records
remain unknown unless approved corpus evidence explicitly marks them global or organization
specific. The tool inventories only the supported SQLite tables and `report_*.json` files in the
explicitly supplied export directory; it does not inspect arbitrary cache files, job queues,
external invitation providers, PostgreSQL, or undocumented resource stores. Do not use its output
as a backfill or production-readiness approval.

### Disposable-copy validation (2026-10-13)

The dry run was exercised against a newly created copy of the one documented local source,
`D:\cac\AI-Interviewer-Adaptive-Multimodal-AI-Interview-Platform\storage\ai_interviewer.db`.
No other SQLite files were opened. Python 3.11.9 opened the source with `mode=ro`, enabled
`PRAGMA query_only=ON`, verified `PRAGMA database_list` resolved to that exact path, ran
`PRAGMA integrity_check`, and copied it with SQLite's online `Connection.backup()` API to
`%TEMP%\ai-platform-legacy-tenant-verify-20261013-174503\legacy-copy.db`. The destination did not
exist before the run. The source hash stayed unchanged through backup and validation.

SHA-256 before inventory execution and after all runs:

- Source database: `546dd6cab8672e3f362bbbecaaa33e177ccd12ae1858e4ebbf0c2b1665da6735`
- Disposable copy: `8b1bfbb2e803380378130c786f526656d57c3e9467c2420b37278af9e82a870f`

The exact inventory invocation and its process exit status were:

```powershell
python -m tools.legacy_tenant_mapping_dry_run `
  --source-db "$env:TEMP\ai-platform-legacy-tenant-verify-20261013-174503\legacy-copy.db" `
  --reports-dir "D:\cac\AI-Interviewer-Adaptive-Multimodal-AI-Interview-Platform\storage"
# Exit status: 2 (expected: existing integrity/ownership findings)
```

The run used Python 3.11.9 explicitly and removed `DATABASE_URL` from the CLI child environment.
The independent read-only SQLite reconciliation ran `SELECT count(*) FROM <table>` for each
inventory table, plus:

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;
SELECT count(*) FROM interviews i
LEFT JOIN candidates c ON c.id = i.candidate_id WHERE c.id IS NULL;
SELECT count(*) FROM interviews i
WHERE i.owner_user_id IS NOT NULL AND i.owner_user_id <> ''
  AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = i.owner_user_id);
```

The separately executed report-file reconciliation parsed only linkage fields in memory and
counted exports with an absent interview, a matching interview/candidate pair, a mismatched pair,
or a missing candidate profile. It emitted aggregate counts only.

The tool and independent SQL totals agreed: **89** users, **2** candidate profiles, **301**
interviews, **502** questions, **236** turns, **244** assessments, **148** evidence rows, **216**
competency-state rows, **16** audit rows, **18** RAG rows, and **0** invitation rows (the table is
absent). The SQLite integrity check returned `ok`; both before and after checks found **18** foreign
key violations, all on interview owner references. Independent relationship checks confirmed
**301** interview-to-candidate profile orphans, **18** missing non-null owner references, **281**
resolved owners, **2** null owners, and **0** broken question/turn/assessment/evidence/competency
parent links. The tool reported all **301** interviews structurally inconsistent. Their children
inherit that classification because tenant ownership cannot be established through those
interviews; the corrected `broken_child_links` metric remains zero because the child relationships
themselves resolve. This distinction was regression-tested.

The report directory contained **70** exports: the independent link reconciliation and tool both
counted **54** exports without a source interview and **16** matching interview/candidate pairs;
all **16** lack a candidate profile, with **0** mismatched pairs. The tool classified all **70**
exports as structurally inconsistent for tenant assignment. Candidate profiles (**2**), users
(**89**), audit events (**16**), and RAG rows (**18**) had no authoritative mapping and remained
unmapped. RAG scope remained `unknown`.

Mapping-path simulations used temporary JSON proposals with synthetic organization names and
evidence references explicitly marked `TEST_ONLY_NO_AUTHORITY`. A structurally valid proposal
without a digest stayed `proposed_review_required` and yielded **0** mappable candidates. A
malformed digest was rejected with `approved_digest_invalid`. A conflicting two-organization
proposal was given its exact SHA-256 only to exercise the conflict-classification code path; the
result was **1 ambiguous**, **0 mappable** candidate. This synthetic digest is not approval or
evidence of authority. Malformed JSON was rejected with `mapping_schema_invalid`. These simulation
files were confined to the disposable verification directory and did not alter either database.
The exact CLI argument forms run by the Python 3.11.9 subprocess harness were:

```powershell
$copy = "$env:TEMP\ai-platform-legacy-tenant-verify-20261013-174503\legacy-copy.db"
$proposal = "$env:TEMP\ai-platform-legacy-tenant-verify-20261013-174503\synthetic-proposal.json"
$conflict = "$env:TEMP\ai-platform-legacy-tenant-verify-20261013-174503\synthetic-conflict.json"
$malformed = "$env:TEMP\ai-platform-legacy-tenant-verify-20261013-174503\malformed.json"
$syntheticDigest = (Get-FileHash $conflict -Algorithm SHA256).Hash.ToLowerInvariant()
python -m tools.legacy_tenant_mapping_dry_run --source-db $copy --mapping $proposal
python -m tools.legacy_tenant_mapping_dry_run --source-db $copy --mapping $proposal `
  --approved-mapping-sha256 not-a-sha256
python -m tools.legacy_tenant_mapping_dry_run --source-db $copy --mapping $conflict `
  --approved-mapping-sha256 $syntheticDigest
python -m tools.legacy_tenant_mapping_dry_run --source-db $copy --mapping $malformed
```

The third command's exact digest was computed in memory from the synthetic conflict file and
supplied as the final argument; the digest is intentionally not recorded as an approval.

Before/after source and copy hashes matched. `PRAGMA integrity_check` stayed `ok`, and the same 18
foreign-key violations remained. Hashes of report files were also unchanged. Captured CLI output
contained no source user, candidate, or interview IDs; a synthetic raw-report/email canary was not
emitted. The CLI child process received no `DATABASE_URL` or database credentials. The tool's
aggregate JSON and sanitized error codes were the only captured output.

No discrepancy remains in the aggregate totals. The first run exposed that `broken_child_links`
counted children inheriting a structurally inconsistent interview as though their own links were
broken. The metric was corrected to count only missing required/secondary parents; the focused
regression suite and disposable-copy rerun confirmed **0** such broken child links, matching
independent SQL. After this correction, the focused command
`python -m pytest -q tests\unit\test_legacy_tenant_mapping_dry_run.py` reported **19 passed**, with
**2 existing pytest configuration warnings** for unrecognized `timeout` and `timeout_method`
options; no tests were skipped. `git diff --check` passed. The test run used `ENVIRONMENT=test`
and the fresh URL `sqlite:///storage/ci_legacy_mapping_validation_20261013.db`, as required by the
existing pytest SQLite safety gate.

This validates only the documented local SQLite snapshot and the tool's read-only behavior. The
existing **18** owner foreign-key violations and **301** candidate-profile orphans remain unresolved.
Seven other SQLite files remain uninspected. No authoritative organization-ownership crosswalk was
available; an organization-approved roster/crosswalk and independently reviewed evidence are
required before any real mapping proposal may be approved. No tenant assignment, migration, or
backfill was performed, and production rollout is not approved.

Classify each in-scope row as **mappable** only when an exact, approved authoritative mapping and
all relationship checks pass; **unmapped** when evidence is absent; **ambiguous** when evidence
conflicts or identifies multiple organizations; or **structurally inconsistent** when required
source relationships are absent or invalid. Missing, ambiguous, and inconsistent rows remain
quarantined and tenant-null. A count summary must reconcile to the source total for every category.
Use a versioned mapping register keyed by source system, record type, and stable source record ID;
each proposed assignment must include the target organization, authoritative evidence reference,
approver, approval time, and mapping revision. The approval record must not contain candidate
content or secrets.

For the inventory snapshot above, **0 legacy rows are confirmed mappable** because no authoritative
tenant mapping was found. All **301** interviews are structurally inconsistent on candidate-profile linkage;
their child records remain unmapped through those interviews. The **2** candidate profiles, all
**89** user identities' organization memberships, **16** audit events, and **18** RAG chunks
remain unmapped. All **70** exports are structurally inconsistent for tenant assignment: **54**
have no source interview, and the other **16** match interview fields but have no candidate-profile
or tenant-authorization proof. There are no invitation rows in this database. These
classifications are overlapping integrity/status findings where stated, not additive counts
across resource types.

Resource-specific evidence and handling:

- **Users and memberships:** Legacy users have global IDs and roles but no organization membership.
  Create memberships only from an organization-approved roster or contract-to-user crosswalk
  reviewed by the data owner. One user may belong to multiple organizations. A matching email,
  recruiter role, or account existence alone is not approval.
- **Candidate profiles:** The two legacy rows lack recruiter owner, candidate-account link, and
  organization. Map each only from an authoritative ATS/profile identifier and organization
  record, independently approved by that organization's data owner. Do not create or link a
  profile by email/name matching. Keep conflicting and unverified profiles unassigned.
- **Interviews:** Require a verified source interview identifier, its authoritative organization,
  and a reconciled candidate profile in that same organization. The current **301/301** orphaned
  candidate links block profile-based mapping; investigate them against the source system rather
  than substituting a nearby profile. The recruiter must also be an approved, active recruiter
  member of the target organization. Existing owner IDs, including the **18** dangling references,
  are not tenant evidence. Resolve the **2** null owners from independent evidence or keep those
  interviews quarantined.
- **Interview children and evaluations:** Questions, turns/answers, evidence, competency states,
  and assessments inherit organization only through a successfully approved owning interview.
  Preserve their existing parent linkage; do not map child rows independently. Reconcile each
  category's child count and foreign-key relationships before and after any approved batch.
- **Invitations:** This snapshot has no invitation table, so there are no database invitation rows
  to map. Check any separately retained mail/provider records under their own authority and
  retention controls. Do not recover or import raw tokens. If valid pending invitations cannot be
  independently verified, revoke where possible and reissue through the current tenant-scoped
  invitation flow after membership approval.
- **Audit events:** The 16 legacy events have actor references but no organization or requested-
  organization context. Map an event only where an authoritative support/access log or approved
  case record resolves its resource and target organization. Preserve unresolved events as
  tenant-unassigned historical records; do not infer scope from actor, action, or resource ID.
- **RAG documents and embeddings:** The 18 legacy chunks have no organization field and no
  non-empty embeddings. Require a reviewed corpus catalog establishing source, license/data
  classification, intended audience, and whether the document is approved global material or
  organization-confidential. The checked-in demo corpus is explicitly synthetic, but that does not
  prove that the legacy database rows came from it. Unknown provenance remains quarantined; do not
  label it global by default. Rebuild embeddings only after corpus ownership approval and with the
  configured offline provider unless separately authorized.
- **Report exports:** Treat report files as separately persisted sensitive artifacts, not as
  authorized merely because their IDs match. Require a manifest linking each file hash to a
  reconciled source interview, candidate profile, tenant, and approval state. Regenerate eligible
  reports from authorized persisted records; quarantine the **54** exports without a source
  interview and all remaining exports until a candidate/profile and tenant are proven. Do not
  expose flat storage paths as a download authorization mechanism.
- **Snapshots, jobs, and caches:** `interviews.state_json` is a persisted interview snapshot and
  inherits ownership only from its verified interview. No background job queue or durable
  application cache was found; Redis is used for short-lived interview-processing locks, not as
  an ownership source. Any future asynchronous job must carry actor, organization, and resource
  identifiers and revalidate membership/ownership when it runs and before it exports data.

RAG scope and write isolation are now explicit in the ORM and vector-store code. Tenant identity
includes organization scope, and source replacement is constrained by scope and organization.
Retrieval excludes unclassified rows by default. Global retrieval requires an explicit
include-global policy and the exact allowlisted reference for the checked-in synthetic demo
corpus. Global ingestion additionally requires the complete corpus to match its pinned SHA-256;
arbitrary documents cannot self-assert global scope through a caller-supplied reference. The
digest establishes byte integrity against the reviewed repository fixture, not external identity,
approval authenticity, licensing, or ownership. Do not extend the allowlist without a separate
trusted approval process. Existing rows must remain unclassified; do not infer scope from a null
or non-null `organization_id`.

Alembic revision `e84_rag_tenant_scope` adds the scope columns and constraints, replaces the
global chunk identity with tenant-aware partial unique indexes, and preserves legacy organization
IDs as untrusted hints without treating them as authorization evidence. Read paths retain such
hints only on `unclassified` records and never use them to authorize retrieval; write paths reject
unclassified input carrying an organization hint. Its downgrade refuses to discard classified
scope metadata or to restore the old global identity if duplicate keys have been introduced. The
revision is authored but has not been run. Apply it only through the separately approved migration
process; until then, the ORM expects columns not present in databases at the prior head.

Synthetic regression tests in `tests/unit/test_rag_retrieval.py` and
`tests/unit/test_rag_semantic_retrieval.py` cover unclassified-row exclusion, rejection of
unapproved global references and altered demo corpus bytes, explicit global retrieval,
overlapping tenant identities, organization-bounded source replacement, conflicting replacements,
and repeated-ingestion idempotency. The RAG semantic test file is already included in the CI RAG
group.

### Authoritative evidence assessment and reconciliation contract

The evidence considered for this reconciliation design is limited to the previously inventoried
`storage/ai_interviewer.db` snapshot, this architecture record, the current SQLAlchemy models in
`services/database.py`, account-linking/invitation code, report-generation code, and the checked-in
RAG demo-corpus loader. This task did not reopen or modify the source database, inspect other
SQLite files, or retrieve records from external systems.

These sources establish only the following:

- The legacy database establishes which rows and relationships exist in that snapshot, and
  aggregate counts and integrity results. The ownership fields needed for tenant attribution are
  absent or unresolved.
- A `users.id` identifies a local account. Email uniqueness or `email_verified_at` may support
  account identity/verification, but proves neither employer nor organization membership or
  recruiter authority.
- Current organization membership rows and role/status, where present, establish current
  application state. They do not establish membership at the historical interview time. Do not
  backdate current membership to infer historical authority.
- Interview `candidate_id` and report interview/candidate fields establish only recorded linkage.
  Matching IDs or report content do not establish that a source record is correct or tenant
  authorized. The inspected report-generation path writes report files but provides no signed
  manifest tying each artifact hash to a reviewed tenant decision.
- The checked-in RAG demo corpus is explicitly synthetic and loaded as a configured corpus. The
  legacy RAG rows do not identify that corpus; `source`, filename, document/chunk ID, text, or
  embedding alone does not prove source ownership, data rights, or intended global sharing.
- Audit actor/resource references identify IDs recorded in an event. They do not establish the
  resource's tenant, the requested organization, or whether the event is tenant-scoped or
  platform-wide.

No organization-approved user roster, HRIS/IdP-to-organization roster, ATS/customer-contract
crosswalk, invitation-provider record set, signed export manifest, audit case register, or RAG
corpus ownership/license catalog was present in the inspected repository or prior snapshot audit.
These authoritative sources must be supplied by their custodians; application records must not be
used to manufacture them.

#### Ownership graph and acceptance requirements

Treat every edge below as an independent assertion. A valid identifier at one step does not prove
the next:

1. **User identity:** reconcile a legacy account key to one canonical application identity through
   the identity provider/account-linking authority. Email matching alone is insufficient.
2. **Organization membership and role:** prove membership in the exact organization and the
   required role over the relevant effective interval. Users may have multiple organizations.
   Current role or membership does not prove historical membership.
3. **Candidate profile:** reconcile an authoritative ATS/profile key to the candidate profile and
   organization; independently establish any candidate-account and recruiter-owner links. Do not
   match by name/email or proximity to an interview.
4. **Interview:** reconcile the source interview to exactly one candidate profile in the same
   organization, and establish recruiter identity plus recruiter membership effective at the
   relevant time. Missing, duplicate, or contradictory candidate/owner evidence quarantines the
   interview. The **301** unresolved candidate links, **18** missing owner references, and **2**
   null owners cannot pass this gate using current evidence.
5. **Interview children/evaluations:** inherit tenant scope only from an accepted interview after
   child-to-interview and applicable secondary-parent relationships are verified. Do not map child
   records independently.
6. **Invitation:** none exist in the inspected snapshot. Any provider records must use stable
   provider invitation IDs and verified organization/recruiter relationships, and prove current
   expiry/revocation/redemption state. Never import raw tokens. If validity is unproven, quarantine
   and reissue through the tenant-scoped flow after approval.
7. **Report export:** establish a stable export event/manifest and file SHA-256, then link it to the
   canonical interview, candidate, organization, generating actor, and authorization at generation
   time. Filename, path, content, or matching IDs alone are insufficient. Regenerate from
   authorized source rows where feasible; quarantine otherwise.
8. **Audit event:** establish event semantics and link its resource key to a separately accepted
   tenant-owned resource. Confirm organization scope against that resource and requested context
   where available. Platform-wide/support events remain platform-scoped; do not force them into a
   customer tenant.
9. **RAG document/chunk/embedding:** reconcile source/document/version/chunk to an approved corpus
   catalog establishing source owner, license/data classification, intended audience, and explicit
   `global` or organization scope. Embeddings inherit that exact document/version's approved scope;
   they are not independent ownership evidence. Unknown provenance stays quarantined, not global.

#### Future human-approved crosswalk contract

Use a versioned, access-controlled register with one adjudication per source resource or
relationship. The stable key is the tuple
`(source_system, source_snapshot_id, record_type, source_record_id)`; a bare database ID is not
globally stable across systems or snapshots. Treat source IDs and evidence locators as sensitive:
keep row-level data in a restricted review location and never emit it in logs or aggregate reports.
Do not include names, emails, answers, report contents, invitation tokens, credentials, or raw
evidence.

Each row must contain:

- `crosswalk_schema_version`, an immutable `mapping_revision`, source system and immutable
  snapshot identifier/digest;
- the namespaced source key and a disposition: `approved`, `unmapped`, `ambiguous`, or
  `quarantined`;
- for `approved` only, the target `organization_id` (or explicit RAG `scope=global`) and the
  namespaced related keys needed to prove the ownership graph (candidate, recruiter, interview,
  report source, audit resource, or RAG source as applicable);
- one or more evidence references: controlled case/artifact locator, immutable evidence digest,
  evidence type, issuing system/custodian, and effective interval. The checker must not retrieve
  or print evidence content;
- separate approval attestations over the exact crosswalk digest: source custodian validates
  identity/linkage, the target organization's authorized data owner approves ownership, and the
  designated security/data steward reviews report, audit, and RAG classifications. Record
  reviewer identity/role, UTC time, decision, and approval case. The preparer must not self-approve.
  Preserve revisions and superseding decisions in an append-only audit trail.

Illustrative format only; it contains synthetic placeholders and is **not a complete serialized
fixture for the v2 validator**:

```json
{
  "crosswalk_schema_version": 2,
  "mapping_revision": "review-2026-001",
  "source_snapshots": [
    {
      "source_system": "approved-source-name",
      "snapshot_id": "controlled-snapshot-reference",
      "snapshot_sha256": "sha256-of-read-only-source-snapshot"
    }
  ],
  "records": [
    {
      "source_key": {
        "source_system": "approved-source-name",
        "source_snapshot_id": "controlled-snapshot-reference",
        "record_type": "interview",
        "source_record_id": "restricted-stable-key"
      },
      "decision": "approved",
      "organization_id": "approved-organization-id",
      "relationships": {
        "candidate_source_key": "namespaced-key-reference",
        "recruiter_source_key": "namespaced-key-reference"
      },
      "evidence_refs": [
        {
          "locator": "restricted-case-or-artifact-reference",
          "sha256": "sha256-of-authoritative-evidence-artifact",
          "type": "source-system-record",
          "issuer": "source-system-owner",
          "effective_from": "2026-01-01T00:00:00Z",
          "effective_to": null
        }
      ]
    },
    {
      "source_key": {
        "source_system": "approved-source-name",
        "source_snapshot_id": "controlled-snapshot-reference",
        "record_type": "candidate",
        "source_record_id": "restricted-stable-key"
      },
      "decision": "ambiguous",
      "evidence_refs": []
    }
  ],
  "approval_record": {
    "crosswalk_sha256": "sha256-of-exact-crosswalk-bytes",
    "case_reference": "controlled-approval-ticket",
    "approvals": [
      {
        "reviewer_id": "restricted-reviewer-identity",
        "reviewer_role": "source_custodian",
        "decision": "approved",
        "approved_at": "2026-10-13T00:00:00Z"
      },
      {
        "reviewer_id": "restricted-reviewer-identity",
        "reviewer_role": "organization_data_owner",
        "decision": "approved",
        "approved_at": "2026-10-13T00:00:00Z"
      }
    ]
  }
}
```

Conflicting approved rows for a source key, overlapping/expired effective periods, multiple
candidate or organization matches, missing evidence or approval, and digest mismatch must block
application and remain ambiguous, unmapped, or quarantined. Never resolve conflict by
last-write-wins. A digest binds approval to exact bytes; it does not prove evidence authority,
authenticity of a reviewer, or correctness of an ownership decision. Do not treat an `"approved"`
field or matching SHA-256 as evidence by itself.

#### Current dry-run validation limits

The current CLI accepts mapping format version 1. It validates JSON structure, nonempty evidence
reference strings, target organization references, source-row existence where supported tables
exist, exact-file SHA-256 equality, duplicate/conflicting assignments, and selected membership,
candidate, interview, invitation, and RAG scope relationships. Its reports expose aggregate counts
and sanitized status codes rather than source identifiers or evidence locators.

It does **not** implement the proposed namespaced source key/snapshot/revision contract; verify
evidence artifacts or digests; authenticate approvers; enforce separation of duties; prove
historical membership/effective dates; map source identity to canonical user identity; or establish
all audit-event resource-to-tenant relationships. Specifically, audit mapping checks that an actor
reference exists and accepts an event's approved mapping but do not prove that the event's
`resource_type`/`resource_id` resolves to the same tenant. If the source has no organizations table,
the tool cannot validate that a target organization exists. `approved_digest_matched` means only that the caller supplied the exact file digest. Use the
current CLI for aggregate inventory and v1 proposal review only; do not use it as an authoritative
approval or backfill gate. A separate v2 structural validator is described below. It does not
change v1 dispatch, inventory behavior, or the fact that no write process is authorized. Until the
external evidence and authorization gates are met, unresolved records remain
unmapped/quarantined.

#### Standalone v2 crosswalk validator

`tools/legacy_tenant_crosswalk_v2.py` provides a read-only structural validator for a proposed v2
crosswalk. It is separate from `legacy_tenant_mapping_dry_run.py`: the existing CLI remains v1,
does not silently dispatch to v2, and retains its existing behavior. Call
`validate_crosswalk_v2(bytes_or_text)` for in-memory validation or
`validate_crosswalk_v2_file(path)` for one explicitly named JSON file. Neither function opens a
database, writes a file, migrates data, or applies assignments. The 10 MiB input limit and strict
JSON parsing reject duplicate JSON properties and non-standard JSON constants.

The validator's exact top-level structure is:

- `crosswalk_schema_version`: integer `2`;
- `mapping_revision`: `revision_id`, positive integer `sequence`,
  `supersedes_revision` (null for sequence 1; otherwise a different revision ID), and
  `superseded_by_revision` (must be null for the current proposal). Sequence 1 has
  `supersession_decision: null`; a later sequence requires a decision object naming the previous
  revision, an approving reviewer, timestamp, controlled case reference, and evidence references;
- `prepared_by`: opaque `identity_id` and timezone-aware `prepared_at`;
- `source_snapshots`: a non-empty list of unique `(source_system, snapshot_id)` descriptors, each
  with lowercase 64-character `snapshot_sha256`; this permits a relationship graph to include
  independently snapshotted identity, membership, ATS, and legacy sources;
- `records`: record adjudications described below;
- `approval_record`: proposal content digest, restricted approval case reference, and approval
  attestations.

Each `records[]` entry has a namespaced `source_key` with exactly
`source_system`, `source_snapshot_id`, `record_type`, and `source_record_id`; its source/snapshot
pair must be registered in `source_snapshots`. Keys are unique within the proposal. The supported
record types cover users, organization memberships, candidates, interviews and child records,
invitations, report exports, audit events, and RAG documents/chunks/embeddings. Each row has one
of `approved`, `unmapped`, `ambiguous`, or `quarantined`, a `relationships` array, and
`evidence_refs`. A non-approved row cannot carry `target_organization_id` or scope. Approved
organization-owned rows require a target organization; user identities and interview children do
not acquire a tenant assignment directly. Approved RAG documents require explicit `scope` of
`global` or `organization`; approved audit events require `organization` or `platform` scope.
RAG and interview children inherit scope only through their approved parent.

Relationship entries contain a controlled `relation_type` and a complete namespaced `target_key`.
The validator checks record-type compatibility, uniqueness of each relationship type, required
parent links, target existence, parent approval, organization consistency, and key graph rules:
interviews must link to one candidate, recruiter, and recruiter membership in the same
organization; the membership must link to that recruiter and be effective at the interview's
`relevant_at`. Invitation and report-export actor membership is checked at their relevant time.
Report exports must resolve to the same candidate as the linked interview. Child records resolve
only through their approved interview or RAG document parent. For questions, turns, assessments,
and evidence, each supplied secondary question/turn relationship must resolve to the same
interview as the child's `child_interview`; a cross-interview secondary link is rejected.
Organization audit events must agree with an approved linked resource; platform events remain
platform-scoped.

The validator cannot prove that `records[]` is a complete inventory of the source snapshot merely
because every listed key is unique and every referenced key resolves. A missing row which is not
referenced by any supplied row is undetectable from the proposal alone. Completeness requires an
independently generated, read-only source inventory and a reconciliation of that inventory's
namespaced keys and aggregate counts against the crosswalk; the source custodian must attest to
that reconciliation outside this structural validator. Do not use this validator's valid result as
proof that all source records were reviewed.

Every evidence reference has a controlled `locator`, immutable lowercase SHA-256 `sha256`,
`type`, `issuer`, timezone-aware `effective_from`, and nullable timezone-aware `effective_to`.
Intervals are half-open (`[effective_from, effective_to)`), and a non-null end must follow the
start. Approved interviews, invitations, report exports, and audit events require timezone-aware
`relevant_at`; at least one evidence interval must cover that timestamp. Applicable membership
evidence must also cover the interview/invitation/report actor's relevant time. Unresolved
dispositions may omit `relevant_at` when the historical event time is not known.

`approval_record.approvals` requires exactly one `approved` `source_custodian` attestation and
exactly one `organization_data_owner` attestation for each organization assigned to an approved
record. A `security_data_steward` attestation is additionally required when the crosswalk contains
report-export, audit-event, or RAG records. Each attestation includes opaque `approver_id`,
`approver_role`, `decision`, timezone-aware `decision_timestamp`, `decision_revision` matching
`mapping_revision.revision_id`, and one or more evidence references; organization-owner attestations
also name their `organization_id`. A reviewer cannot fill more than one role, and no approver may
be `prepared_by`. Sequence 1 cannot supersede another revision; later sequences must name a
different predecessor and provide a supersession decision; a proposal explicitly marked as
superseded is rejected. Approval attestations bind to a canonical
SHA-256 of the proposal JSON with `approval_record` omitted (UTF-8, sorted keys, compact JSON,
non-ASCII preserved). This avoids a self-referential digest while binding approval metadata to the
exact canonical proposal content. Changing any proposal field invalidates that digest.

The result contains only total record count, four disposition counts, aggregate safe error-code
counts, and `authorization_status: not_authenticated_by_validator`. It does not return source
keys, identifiers, locators, evidence contents, or raw parser errors. Structural validity means
only that this file conforms to the validator's schema and internal graph checks. In particular,
the validator does **not** authenticate reviewer identities or roles, fetch or authenticate the
evidence, establish the authority of an issuer, prove ownership truth, prove an external revision
ledger has no later superseding revision, or confirm an organization exists in a target database.
A matching digest is not authentication or evidence authority. The JSON alone can never authorize
a backfill.

Run the synthetic v2 and existing v1 regression tests from PowerShell with a fresh permitted
disposable test database path:

```powershell
$env:ENVIRONMENT = 'test'
$env:DATABASE_URL = 'sqlite:///storage/test_crosswalk_v2_validation.db'
python -m pytest -q tests/unit/test_legacy_tenant_crosswalk_v2.py tests/unit/test_legacy_tenant_mapping_dry_run.py
```

The test database name must be fresh and satisfy the repository pytest safety gate (`test_*.db`
or `ci_*.db`). Remove only that exact generated test file after pytest exits. These tests use
synthetic fixtures; do not substitute a legacy source database.

The v2 validator improves structural review but does not resolve the missing authoritative
crosswalk or make a legacy row mappable on its own. Human review must still verify source
artifacts, identity, effective membership, organization ownership, resource relationships, and
append-only revision state outside this validator. The current snapshot therefore remains **NO-GO**
for production rollout and all migration/backfill activity.

For the next dry-run revision, retain the four status totals per category and add aggregate-only
reason counters with stable blocker codes, for example: `AUTHORITY_EVIDENCE_MISSING`,
`IDENTITY_UNRESOLVED`, `MEMBERSHIP_UNVERIFIED`, `CANDIDATE_PROFILE_LINK_MISSING`,
`CANDIDATE_ORG_MISMATCH`, `INTERVIEW_OWNER_MISSING`,
`INTERVIEW_OWNER_MEMBERSHIP_UNVERIFIED`, `SOURCE_KEY_NOT_FOUND`,
`EVIDENCE_DIGEST_MISMATCH`, `APPROVAL_MISSING`, `APPROVAL_DIGEST_MISMATCH`,
`REPORT_SOURCE_UNRESOLVED`, `AUDIT_RESOURCE_SCOPE_UNRESOLVED`,
`RAG_PROVENANCE_OR_RIGHTS_UNKNOWN`, and `CHILD_PARENT_LINK_BROKEN`. Counters must never include
the source key, email, file path, evidence locator, report text, or error message from source
content. The current CLI already returns aggregate relationship counters for several structural
cases; it does not yet provide this complete evidence/approval blocker taxonomy.

### Staged rollout and batch approval gates

Do not expose unresolved legacy records through tenant APIs. Before tenant production traffic:

1. Back up the exact target database. Rehearse both restore and migration on a representative,
   isolated copy. Obtain data-owner sign-off on the target and approved mapping revision.
2. Review dry-run totals and unresolved counts. Do not proceed while ownership conflicts,
   structural inconsistencies, or report/RAG scope are unexplained. Keep unresolved records
   quarantined and excluded from tenant APIs, reports, retrieval, and exports.
3. Apply the tenant migration to the approved target and verify Alembic head and schema
   constraints. Do not create a shared default organization or assign unclassified legacy data to
   any organization.
4. Backfill only explicitly approved, unambiguous mappings in bounded, organization-scoped
   transactions. Order dependencies (approved users/memberships, candidate profiles, interviews,
   then dependent records); children inherit only through interviews. Make batches idempotent and
   on retry compare the stored result with the approved mapping rather than blindly updating.
5. Before committing each batch, verify candidate/interview organization equality, active recruiter
   membership, invitation/profile consistency, all child-to-interview links, report-to-authorized-
   interview links, and approved RAG scope. Reconcile `source total = updated + unchanged +
   rejected/quarantined` by category before and after. Record mapping revision, source/evidence
   references, approver, batch ID, aggregate counts, and outcome in an append-only audit record;
   never copy candidate content or secrets into that record.
6. Regenerate only approved report exports from authorized source rows. Quarantine unresolved
   reports, invitation records, and RAG documents outside normal retrieval/export paths. Reissue
   invitations only after confirming the intended recipient and organization through approved
   evidence.
7. Enable tenant traffic only after cross-tenant API/WebSocket tests, ownership constraints,
   migration checks, backup restore rehearsal, export/RAG tests, and dry-run reconciliation pass.
   Monitor unassigned-row access denials, authorization failures, export counts, and tenant-scoped
   retrieval; pause rollout on any unexpected cross-tenant read/write or count discrepancy.
8. If rollback is required after tenant writes begin, pause writes and restore the verified,
   pre-migration backup using the rehearsed procedure, then reconcile the restored target before
   reopening traffic. Do not attempt a destructive downgrade after tenant ownership or support
   audit data has been written. PostgreSQL downgrade refusal and a failed migration probe leaving
   Alembic at head were verified on the disposable PostgreSQL 15.19 database; SQLite DDL rollback
   is not assumed safe and requires backup restoration.
9. Run existing workflow tests and tenant regression tests against isolated SQLite and PostgreSQL
   databases before enabling traffic. Do not relax the pytest database safety gate to run these
   checks.

The invite-first profile rule and tenant-scoped invitation issuance are implemented. The recruiter
flow issues an organization-scoped email invitation first and creates the profile only when the
intended, verified candidate redeems it. Redemption retains its token input and candidate-ID
response shape. `POST /candidate-invitations` derives organization context from verified backend
membership. The candidate-ID invitation route is retained only for explicitly mapped profiles.
`POST /candidates` rejects direct profile creation with HTTP 409. Regression tests cover
invitation authorization, cross-tenant access, replay/expiry/revocation, and invite-first profile
creation. The separate `AI_platform_UI` repository was not modified.

Platform-admin support uses `X-Support-Reason` for HTTP. For WebSockets, require the approved
initial support-authorization control frame with the explicit organization selector and non-empty
reason before sending interview data or accepting interview actions; do not put the reason in a
URL. Extend audit records to capture actor (nullable for unauthenticated attempts),
target/requested organization, action, resource, outcome, timestamp, and reason. Persist denied
attempts independently of the rejected request transaction. Make audit writes append-only at the
application layer and prevent update/delete through database controls; do not record invitation
tokens, passwords, or interview answer content.

Rollback must restore the pre-migration backup or use reviewed reverse migrations while writes are
paused. Dropping tenant columns after tenant-scoped writes begin would erase ownership information;
rollback must not merge tenants or reassign data. Keep the expanded schema and disable tenant
traffic if application rollback is needed, then reconcile data before re-enabling writes.

### Remaining legacy-data and deployment decisions

- Identify the authoritative candidate/interview source system and obtain organization-owner
  approval. Reconcile the **301** orphaned candidate links, **18** missing interview owner
  references, and **2** null interview owners before mapping dependent records.
- Obtain an organization-approved user membership roster for the **89** global users. Do not use
  email, recruiter role, or owner ID as a substitute for an approved organization membership.
- Determine whether valid pending invitations exist in any external mail/provider records. The
  inspected SQLite database has no invitation table; unverified invitations must not be imported
  or reused.
- Establish approved global-versus-tenant provenance for the **18** RAG chunks and resolve the
  tenant-insensitive upsert/replacement identity behavior before onboarding tenant-specific
  corpora.
- Reconcile the **70** report exports: find an authorized source for the **54** missing interviews
  and establish candidate-profile/tenant ownership for the remaining **16** before regenerating or
  retaining them for tenant use.
- Establish provenance for the seven uninspected SQLite files and build/review the read-only
  dry-run utility before inventorying a representative deployment copy. Until these gates and a
  backup/restore rehearsal pass, production tenant rollout is blocked.

## Timestamps

Application timestamps use UTC-aware `datetime` values. Database columns use SQLAlchemy
`DateTime(timezone=True)`. SQLite does not preserve timezone metadata in storage. Earlier
migrations and API workflows were verified against PostgreSQL before tenant isolation; the
`d3f7_tenant_isolation` migration was also verified on the disposable PostgreSQL 15.19 database
described above.

## Legacy `state_json` Transition

`state_json` has not been deleted. New interview writes create normalized interview, question, and turn records and retain the JSON snapshot for compatibility with the current response shape and report exporter. The current compatibility layer still reads the snapshot to reconstruct fields not yet represented by the normalized schema.

The safe next transition is:

1. Add complete normalized fields needed by the application.
2. Verify normalized reads against existing snapshots.
3. Stop reading `state_json`.
4. Remove the column in a later migration.

The previously documented `storage/interviews.db` file is absent in the current checkout. The
configured default database snapshot and its aggregate counts are described in the local
legacy-data inventory above; no migration or backfill was performed against it.

## Local Development and Tests

Run migrations before starting the application:

```powershell
python -m alembic upgrade head
```

To run the unit suite without AI credentials, point it at a disposable SQLite database and use
offline AI defaults:

```powershell
$env:ENVIRONMENT = "test"
$env:DATABASE_URL = "sqlite:///./storage/ci_tenant_tests.db"
$env:LLM_PROVIDER = "none"
$env:EMBEDDING_PROVIDER = "none"
python -m pytest tests/unit -q
```

Interview processing tests also require local Redis for their locking behavior. The unit suite and
migration tests must use disposable databases, never developer or production data. Integrity tests
use isolated SQLite databases with foreign-key enforcement enabled.

## Migration Procedure

1. Back up the target database.
2. Set `DATABASE_URL` for the target environment.
3. Run `python -m alembic upgrade head`.
4. Confirm `python -m alembic current` reports the expected revision.
5. Run application and integrity tests.

## Rollback Procedure

The disposable legacy-profile migration test confirms that an unassigned legacy fixture can
downgrade and re-upgrade. A separate regression confirms that downgrade is refused before schema
changes when tenant organizations and memberships exist. Do not use
`python -m alembic downgrade -1` as a production rollback: restore the verified pre-migration
backup with writes paused, or follow a separately reviewed reverse-migration procedure. The
initial migration rollback drops the schema and is not data-preserving.
