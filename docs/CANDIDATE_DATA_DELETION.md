# Candidate personal-data erasure (scope `candidate-erasure-v1`)

Implementation: `services/candidate_data_deletion.py`; route
`DELETE /api/v1/candidates/{candidate_id}/personal-data`; tests:
`tests/unit/test_candidate_data_deletion.py`,
`tests/unit/test_candidate_report_export_erasure.py`.

This document records what the erasure does today. It does **not** define a
retention period or a legal basis; those need product-owner/legal approval
(see "Open policy decisions").

## API contract

- Auth: bearer token + `X-Organization-ID`, resolved server-side by
  `tenant_context`. Only an active `org_admin` of that organization may erase.
  Recruiters, candidates and platform support (`platform_admin`) get `403`.
  Platform-support attempts are still recorded by the support-access preflight.
- The candidate must belong to the caller's organization; otherwise `404`.
- `200` `{"status": "completed", ...}`: database redacted and every CV file and
  report export verified absent.
- `202` `{"status": "deletion_pending", "retryable": true, "pending_file_count": n}`:
  the database is redacted but at least one CV file or report export is still
  present (`n` counts both). Repeat the
  same request to finish the work. The response never says `completed` while a
  file remains.
- `409` `{"detail": {"code": "candidate_erasure_blocked", "reason": ...}}` with
  reason `cv_processing_in_progress`, `communication_send_in_progress`,
  `interview_schedule_active` (pending/rescheduling, or a confirmed slot that has
  not ended yet; cancel it first), `interview_turn_in_progress`, or
  `legal_hold` (the candidate is under legal hold; see below).
- Repeating a finished erasure returns `200` with `already_erased: true`.

Response fields: `candidate_id`, `status`, `already_erased`, `retryable`,
`pending_file_count`, `scope_version`, `redacted_records` (row counts per table).

## Ordering and failure handling

1. One transaction (candidate row locked `FOR UPDATE` on PostgreSQL): checks for
   in-flight work, redacts all database fields below, cancels queued work, and
   writes audit `candidate_personal_data_erasure_started` / `pending`.
2. CV files are deleted from private storage (`CV_STORAGE_PATH`) and each one is
   re-checked for existence. Report exports of the candidate's interviews in
   this organization are deleted and re-checked (see "Report exports").
3. Audit `candidate_personal_data_erasure_completed` / `completed`, or
   `candidate_personal_data_erasure_incomplete` / `partial` when a file or
   export remains.

## Report exports

`POST /api/v1/interview/{id}/report` writes the report (evidence quotes,
strengths/gaps, AI recommendation) to disk. There is no database record, so
the location is deterministic:

- New exports: `STORAGE_PATH/reports/<organization_id>/<interview_id>/report_<uuid>.json|txt`
  (an identifier that is not `[A-Za-z0-9_-]` is replaced by `h-<sha256 prefix>`).
  The organization is read from the persisted interview row, not the request.
  The API response is unchanged (`report_file` is still `report_<uuid>.<ext>`).
- Legacy exports written before this layout: `STORAGE_PATH/report_<uuid>.json|txt`
  (and orphaned `.report-*.tmp` files). Erasure lists only the top level of
  `STORAGE_PATH`, skips symbolic links and non-regular files, reads at most the
  first 64 KiB, and deletes a file only when its top-level `"interview_id"`
  (JSON, two-space indented key) or first line `Interview: <id>` (text) names
  one of the candidate's interviews in this organization, and any top-level
  `"candidate_id"` equals the candidate. Interview ids are globally unique
  primary keys taken from the organization-scoped database query, so other
  organizations' exports are never matched. An unreadable legacy file counts
  as pending (ownership unknown).
- Erasure deletes every entry of the candidate's interview directories
  (symbolic links are unlinked, never followed), re-checks existence, and
  removes the empty directory. Missing files count as already deleted. Failures
  are logged with the exception type and counts only.
- After erasure the interview state carries `personal_data_erased`; building or
  writing a report for it returns `410` `{"detail": {"code":
  "candidate_personal_data_erased"}}`. `write_report` re-checks the flag just
  before publishing the file, so a report built before erasure cannot be written
  afterwards. Erasure only covers `settings.STORAGE_PATH`; a `ReportService`
  constructed with an explicit `output_dir` (tests, tooling) is outside scope.

The CV document row (with its random `storage_key`) is kept so a retry knows
which file to delete. Blocked attempts are audited as
`candidate_personal_data_erasure_blocked` / `blocked`. Audit rows contain only
identifiers (actor, organization, candidate id), never names, emails or text.

## Legal hold

Migration `g21_candidate_legal_hold` adds `candidates.legal_hold` (boolean,
default false), `legal_hold_reason`, `legal_hold_set_by_user_id`,
`legal_hold_set_at`. `PUT /api/v1/candidates/{id}/legal-hold` `{"reason": ...}`
sets it and `DELETE` releases it (org_admin only; recruiters, candidates,
platform support `403`; other organizations `404`; audited as
`candidate_legal_hold_set` / `candidate_legal_hold_cleared`, without the reason
text). While held, erasure returns `409` reason `legal_hold` (audited as
`blocked`) and retention rules never select the candidate. Releasing the hold
clears the reason.

## Data inventory and treatment

| Location | Personal data | Treatment |
|---|---|---|
| CV files (`CV_STORAGE_PATH/<uuid>.pdf/.docx`) | full CV | **deleted** |
| Report exports (`STORAGE_PATH/reports/<org>/<interview>/`, legacy `STORAGE_PATH/report_<uuid>.*`) | evidence quotes, strengths/gaps, recommendation | **deleted**; new exports refused (410) |
| `candidates` | name, email, phone, resume_url, user_id | name `[erased]`, email `erased-<id>@erased.invalid`, phone/resume_url/user_id NULL |
| `organization_memberships` (candidate role, this org) | account-to-org link | revoked (a new invitation can reactivate it) |
| `users` | email, password hash | **kept**: global account, may belong to other organizations |
| `candidate_invitations` | candidate_name, email | name NULL, email placeholder, pending invitations revoked |
| `recruitment_applications` | failure_message | NULL; open applications set to `failed` / `candidate_data_erased` so the worker stops |
| `candidate_cv_documents` | extracted_text, parsed_output_json, sha256 | NULL, NULL, zero hash; row kept for file retry |
| `candidate_criterion_assessments` | evidence quotes, explanation, uncertainty, review_note | `[]` / `[erased]` / NULL; numeric score kept |
| `candidate_screenings` | rubric snapshot (campaign data) | kept |
| `candidate_communications` | recipient, subject, body, template snapshot | placeholders; draft/awaiting/approved/retryable sends cancelled |
| `candidate_communication_events` | delivery metadata only | kept |
| `candidate_availability_requests` | campaign-proposed windows | open requests cancelled |
| `candidate_availability_responses` | candidate's availability windows | `windows_json` set to `[]` |
| `interview_schedules`, reservations, events | interviewer time slots | kept (erasure blocked while a slot is pending or still in the future) |
| `interviews` | state_json (history, answers, evidence), current_question | state reduced to non-text keys, marked `personal_data_erased`, status `completed`; question `[erased]` |
| `interview_turns` | candidate_answer | `[erased]` |
| `interview_questions` | generated question text (may echo answers) | `[erased]` |
| `interview_evidence` | evidence quotes | `[erased]`; competency/strength metadata kept |
| `interview_assessments` | strengths, gaps, rationale | `[]` / `[]` / `[erased]`; numeric score kept |
| `interview_competency_state` | numeric only | kept |
| `recruitment_campaign_hiring_outcomes` | free-text reason | `[erased]`; outcome value kept (target counts depend on it) |
| `access_audit` | identifiers only | kept |

Rows are redacted in place, not deleted: composite foreign keys chain
applications, CV documents, screenings, communications, availability,
schedules, interviews and hiring outcomes, and campaign headcount/target logic
counts hiring outcomes.

## Storage inventory (all locations found by code inspection)

Method: every file write (`open(..., "w")`, `write_text`, `os.open`,
`tempfile`, `NamedTemporaryFile`, `shutil`), Redis usage, RAG ingestion and
logging setup were inspected. "Immediate" means within the erasure request.
Legal hold is a single flag on `candidates` (see "Legal hold"); it blocks the
whole erasure, not individual locations.

| Location | Tenant / owner | Candidate data | Deletion method | Dependencies | Timing | Audit retained |
|---|---|---|---|---|---|---|
| Database tables (see table above) | `organization_id` columns | yes | in-place redaction, one transaction | composite FKs; campaign counts | immediate | `access_audit` rows (ids only) |
| CV files `CV_STORAGE_PATH/<hex>.pdf/.docx` | via `candidate_cv_documents.organization_id` | full CV | unlink + existence check; key kept for retry | CV document row | immediate; `202` + retry on failure | started / completed / partial |
| Report exports `STORAGE_PATH/reports/<org>/<interview>/` | org + interview in path | evidence, recommendation | unlink + check; directory removed | interview ids from DB | immediate; `202` + retry on failure | same rows |
| Legacy report exports `STORAGE_PATH/report_<uuid>.*`, `.report-*.tmp` | header `interview_id` | as above | bounded header match, unlink + check | interview ids from DB | immediate; `202` + retry | same rows |
| Upload bytes during CV intake | request | full CV | in memory only (`UploadFile.read`, `BytesIO` parsing); no temp files written | none | n/a | n/a |
| Redis (`interview:processing:<id>` locks; rate-limit counters keyed by actor id / client IP) | none | no candidate content; ids/IP with TTL | expire by TTL | none | TTL (seconds to minutes) | n/a |
| `rag_documents` | `organization_id` / scope | none from the platform: only the synthetic demo corpus (`rag/ingest.py`) and admin-ingested documents; CV and interview content is never ingested | n/a | n/a | n/a | n/a |
| `rag/documents/data_parser/cv/*.md`, `rag/documents/indexed/cv/*.json` (offline CLIs `rag/chunking/run_indexing.py`, `extract_with_docling.py`, `context_builder.py`, `core/test*.py`) | none (developer corpus) | sample CVs in the repository, not platform candidates | manual (repository files) | none | n/a | n/a |
| Application logs (stdout JSON via `utils/logger.py`; no file handler) | n/a | identifiers only by design | log-platform retention | external | outside this operation | n/a |
| Backups, replicas, email-provider copies, LLM/STT provider retention | external | yes | not covered | external | outside this operation | n/a |

## Open policy decisions (need product-owner/legal approval)

- Retention period and any automatic time-based purge. A policy-neutral rule
  engine exists (`services/retention_policy.py`) but ships with **no rules** and
  execution disabled; see `docs/RETENTION_POLICY_DECISIONS.md`.
- Whether hiring outcomes, numeric screening/interview scores and competency
  states may be retained after erasure (currently retained, without free text).
- Whether access-audit rows and event rows are retained, and for how long.
- Whether platform support may erase on an organization's behalf (currently denied).
- Legal hold: who may set it, whether it also freezes retention of audit rows,
  and whether erasure requests received during a hold are queued (today they
  are refused with 409 and must be repeated after release).
- Whether the global user account should also be deleted when the candidate has
  no other memberships.
- Whether availability windows and interviewer schedule slots count as personal data.

## Known limitations

- Report exports are found by path convention, not a database record. Copies
  made elsewhere (downloads, other directories, a previously used
  `STORAGE_PATH`) are not found. Legacy JSON exports whose identifiers are not
  in the first 64 KiB, or that were edited, are not matched. The legacy scan
  reads the header of every top-level `report_*` file on each erasure (linear
  in the number of legacy files).
- `tools/legacy_tenant_mapping_dry_run.py` only globs top-level
  `report_*.json`; new exports live under `STORAGE_PATH/reports/`.
- Race: an export whose erased-flag check passed just before the erasure commit
  can still be published after the export scan. Repeating the erasure removes it.
- Database backups, replicas, log aggregation, email provider copies of sent
  messages, and any LLM/STT provider retention are outside this operation.
- Application logs use identifiers only, but older logs written before this
  change are not scrubbed.
- Race: the in-flight checks run inside the redaction transaction, but a turn or
  send that starts concurrently on another connection under READ COMMITTED can
  still finish after the check. Interview status is set to `completed` and queued
  sends are cancelled, so no new work can start after commit. Repeating the
  erasure request redacts anything a racing writer left behind.
- A stale `processing` turn (older than `TURN_PROCESSING_LEASE_SECONDS`) does
  not block erasure. Its replay is rejected afterwards because the stored answer
  no longer matches.
