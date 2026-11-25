# Candidate data retention: decisions needed

Audience: product owner and legal counsel. Status of every section:
**REQUIRES_PRODUCT_OWNER_DECISION**. Nothing here is a decided policy. Where a
"proposed default" is shown it is an engineering proposal for discussion only.

This document does not state legal requirements. Consult counsel regarding
GDPR/PDPA-type obligations (lawful basis, purpose limitation, storage
limitation, data-subject rights, cross-border transfer) for every
jurisdiction the platform serves.

Related: `docs/CANDIDATE_DATA_DELETION.md` (what erasure does today),
`services/candidate_data_deletion.py` (erasure), `services/retention_policy.py`
(policy-neutral rule engine, legal hold).

## What exists today (engineering facts)

- **Erasure on request**: `DELETE /api/v1/candidates/{id}/personal-data`
  (org_admin only). Redacts in place (rows are kept as non-PII tombstones
  because composite foreign keys chain applications, CV documents, screenings,
  communications, availability, schedules, interviews and hiring outcomes),
  deletes CV files and report exports, and is idempotent/retryable.
- **Legal hold**: `candidates.legal_hold` (+ reason, actor, timestamp; migration
  `g21_candidate_legal_hold`). Set/release via
  `PUT|DELETE /api/v1/candidates/{id}/legal-hold` (org_admin, audited). A held
  candidate cannot be erased (409 `legal_hold`) and is never selected by
  retention rules.
- **Retention rules**: none configured. `RETENTION_RULES_JSON` is empty by
  default, so nothing is selected. Execution is off
  (`RETENTION_EXECUTION_ENABLED=false`) and additionally needs `--execute`. No
  scheduler runs it. Rule categories available: `unsuccessful_candidate`,
  `candidate_without_hire`; age bases: `hiring_outcome_decided_at`,
  `application_last_activity`. A "campaign closed" basis is **not** available:
  campaigns have no closed state today (`recruitment_campaigns.workflow_state`
  ends at `approved`/`rejected`).
- Retention erasure uses the same erasure scope as a manual request. There is
  no partial ("CV only") erasure yet; any per-category option below that keeps
  some data and deletes other data needs additional engineering.

---

## 1. Unsuccessful candidate CVs

- **Business purpose**: re-contact for future roles; defend a hiring decision
  if challenged; audit of screening fairness.
- **Sensitivity**: high. Full CV (contact details, employment and education
  history, possibly photos or special-category data supplied by the candidate).
- **Options**:
  - A. Erase a fixed period after the rejection/withdrawal decision
    (rule `unsuccessful_candidate` + `hiring_outcome_decided_at` +
    `min_age_days` = the approved period). *Proposed default for discussion.*
  - B. Keep until the candidate withdraws consent for a talent pool; erase
    on request only (current behaviour).
  - C. Erase a fixed period after the last application activity, regardless of
    outcome (rule `candidate_without_hire` + `application_last_activity`).
- **Deletion consequences here**: CV file in `CV_STORAGE_PATH` deleted;
  `candidate_cv_documents.extracted_text`/`parsed_output_json` nulled and
  `sha256` zeroed (row kept for retry); screening evidence/explanations in
  `candidate_criterion_assessments` redacted (numeric scores kept). The whole
  candidate profile is erased too (today's erasure is all-or-nothing).
- **Anonymization feasibility**: good for aggregates (scores, outcome counts
  are already kept without text). CV-only deletion that keeps the profile is
  not implemented.
- **Legal-hold need**: yes (disputed rejection, discrimination claim).

## 2. Hired candidate records

- **Business purpose**: onboarding hand-off, proof of the selection process,
  campaign headcount/target reporting.
- **Sensitivity**: high (same CV data, plus offer/decision).
- **Options**:
  - A. Out of scope for this platform after hand-off: erase a fixed period
    after the `hired` decision (would need a new rule category; none exists,
    by design).
  - B. Keep for the duration of employment plus a period, managed by the
    employer's HR system; the platform keeps only the tombstone.
  - C. Erase on request only (current behaviour).
- **Deletion consequences here**: as section 1. `recruitment_campaign_hiring_outcomes.outcome`
  is kept (campaign headcount and target-achievement counts depend on it);
  only `reason` is redacted. No retention category selects hired candidates.
- **Anonymization feasibility**: good; outcome value and timestamps carry no
  personal text.
- **Legal-hold need**: yes (employment disputes).

## 3. Interview transcripts and answers

- **Business purpose**: reviewer re-assessment, calibration, appeal handling.
- **Sensitivity**: high. Free-text and transcribed voice answers can contain
  anything the candidate said.
- **Options**:
  - A. Same period as the candidate's outcome (sections 1/2).
  - B. Shorter, fixed period after the interview completes (needs a new age
    basis on `interviews`; not implemented).
  - C. Keep only until the hiring decision is recorded.
- **Deletion consequences here**: `interview_turns.candidate_answer`,
  `interview_questions.question_text`, `interview_evidence.evidence_text` set
  to `[erased]`; `interviews.state_json` reduced to non-text keys and marked
  `personal_data_erased` (status forced to `completed`). Audio is not stored by
  the platform; provider-side retention (STT/LLM) is outside this operation.
- **Anonymization feasibility**: low for free text (redaction, not
  anonymization, is what is implemented).
- **Legal-hold need**: yes.

## 4. Evaluation scores and evidence

- **Business purpose**: decision justification, fairness monitoring, model and
  rubric calibration.
- **Sensitivity**: medium for numeric scores; high for evidence quotes and
  rationales.
- **Options**:
  - A. Keep numeric scores indefinitely as de-identified statistics; erase
    evidence text with the candidate (current erasure behaviour).
  - B. Erase scores together with evidence (would require deleting or nulling
    `candidate_criterion_assessments.score`, `interview_assessments.score`;
    not implemented).
  - C. Keep scores for a fixed period, then aggregate and drop per-candidate rows.
- **Deletion consequences here**: evidence/explanations/strengths/gaps/rationale
  redacted in `candidate_criterion_assessments`, `interview_evidence`,
  `interview_assessments`; numeric scores and `interview_competency_state`
  kept.
- **Anonymization feasibility**: medium. Scores remain linked to the candidate
  id tombstone; true anonymization would require breaking that link.
- **Legal-hold need**: yes.

## 5. Candidate communications

- **Business purpose**: proof that a message (invitation, rejection) was sent;
  delivery troubleshooting.
- **Sensitivity**: medium-high (recipient email, names in subject/body).
- **Options**:
  - A. Same period as the candidate's outcome.
  - B. Keep delivery metadata only (status, timestamps) indefinitely; erase
    content with the candidate (current erasure behaviour).
  - C. Short fixed period for content after sending, independent of outcome
    (not implemented).
- **Deletion consequences here**: `candidate_communications` recipient,
  subject, body, template snapshot replaced by placeholders; queued sends
  cancelled; `candidate_communication_events` (metadata) kept. Copies held by
  the email provider are outside this operation.
- **Anonymization feasibility**: good for metadata.
- **Legal-hold need**: yes.

## 6. Audit logs (`access_audit`)

- **Business purpose**: security investigations, proof of who accessed or
  erased data, support-access accountability.
- **Sensitivity**: low-medium. Identifiers only (actor id, organization id,
  candidate id, action, outcome, support reason); no names, emails or text.
  Retention rows store `retention_rule:<rule id>` in `support_reason`.
- **Options**:
  - A. Keep for a fixed security-retention period, then delete.
  - B. Keep indefinitely (identifiers only).
  - C. Keep erasure/legal-hold events longer than ordinary access events.
- **Deletion consequences here**: no deletion path exists. Deleting erasure
  audit rows would also remove the evidence that erasure happened and would
  make retention retries (`retention_erasure_started`) undetectable.
- **Anonymization feasibility**: possible by nulling `actor_user_id`; the
  candidate id is already a tombstone after erasure.
- **Legal-hold need**: likely; decide whether a hold also freezes audit rows.

## 7. Report exports

- **Business purpose**: offline sharing of an interview report with reviewers.
- **Sensitivity**: high (evidence quotes, strengths/gaps, AI recommendation).
- **Options**:
  - A. Same period as the candidate's outcome (current: deleted with erasure).
  - B. Short fixed period after export (exports are files without a database
    record; would need an age scan of `STORAGE_PATH/reports/`).
  - C. Do not persist exports at all; generate on demand.
- **Deletion consequences here**: files under
  `STORAGE_PATH/reports/<org>/<interview>/` and matching legacy
  `STORAGE_PATH/report_<uuid>.*` deleted; new exports of an erased interview
  refused (410). Downloaded copies are outside the platform.
- **Anonymization feasibility**: none (documents are free text).
- **Legal-hold need**: yes.

## 8. Legal hold

- **Business purpose**: preserve data relevant to a dispute, investigation or
  regulator request, overriding retention and erasure.
- **Sensitivity**: the hold reason is free text and may itself be sensitive.
- **Options**:
  - A. Org admin sets/releases (current implementation), audited without the
    reason text.
  - B. Only a designated legal role (does not exist; would need a new role).
  - C. Platform support may also set holds on behalf of an organization
    (currently denied).
- **Deletion consequences here**: while held, manual erasure returns 409 and
  retention skips the candidate. Releasing clears the reason; the audit rows
  remain. Holds have no expiry.
- **Open points**: hold expiry/review cadence; whether erasure requests made
  during a hold are queued (today: refused, must be repeated); whether holds
  should exist per campaign/organization rather than per candidate.

## 9. Deletion (erasure) requests

- **Business purpose**: honour data-subject requests and organization
  instructions.
- **Sensitivity**: n/a (process).
- **Options**:
  - A. Org admin performs erasure on request (current).
  - B. Candidate self-service request with admin approval (not implemented).
  - C. Platform support may erase on behalf of an organization (currently
    denied).
- **Deletion consequences here**: see `docs/CANDIDATE_DATA_DELETION.md`. The
  global `users` account is not deleted (it may belong to other
  organizations); backups, replicas, log platforms, email and AI providers are
  outside the operation.
- **Open points**: response-time target; verification of requester identity;
  whether the global user account is deleted when no memberships remain;
  backup expiry alignment.

---

## Operating the rule engine (once a policy is approved)

```
RETENTION_RULES_JSON='[{"id":"unsuccessful-v1","category":"unsuccessful_candidate",
  "age_basis":"hiring_outcome_decided_at","min_age_days":<approved value>}]'
python -m services.retention_policy --organization-id <org>          # dry run
RETENTION_EXECUTION_ENABLED=true \
python -m services.retention_policy --organization-id <org> --execute
```

Output contains counts and candidate ids only. Exit codes: 0 complete, 1 some
erasures pending/blocked/failed (rerun to retry), 2 invalid rules, 3 execution
disabled. Each run is audited (`retention_preview`, `retention_erasure_started`
/ `_completed` / `_pending` / `_blocked` / `_failed`). At most
`RETENTION_MAX_CANDIDATES_PER_RUN` candidates are erased per organization per
run; the rest are reported as `deferred_count`.
