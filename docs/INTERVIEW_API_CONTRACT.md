# Interview API response contract (Milestone 13, Phase E)

Interview state persisted in `interviews.state_json` is an internal working
document. Since Milestone 12 it carries per-turn retrieval provenance
(`retrieved_sources`: `document_id`, `chunk_id`, `scope`) plus adaptive-graph and
evaluation internals. Candidate-facing responses are therefore an explicit
allowlist. Implementation: `services/interview_views.py`, applied in
`apps/api/v1/routes/interview.py`.

## View selection

- Selected server-side from `TenantContext.role` (resolved from the token and
  active membership). No request parameter or header selects the view.
- `recruiter`, `org_admin`, `platform_admin` receive the **internal view**.
- Every other role (including `candidate` and any unknown role) receives the
  **candidate view** (fail closed).
- Authorization, tenant scoping and support-access auditing are unchanged:
  org_admin still gets 404 / `[]` on interview reads; platform_admin requires
  `X-Organization-ID` + `X-Support-Reason` and is audited by the support-access
  preflight.

## Candidate view (allowlist)

| Field | Why the candidate needs it |
| --- | --- |
| `interview_id` | Submit answers, reconnect, voice socket path |
| `status` | `active` / `completed`; drives the UI flow |
| `mode` | Echo of the requested mode (`text` / `voice`) |
| `position` | Role being interviewed for |
| `current_question` | The question to answer next |
| `created_at`, `updated_at` | Display / ordering |
| `questions_answered` | Progress: number of answered turns |
| `question_budget` | Progress: planned number of questions (the adaptive graph may finish earlier) |
| `campaign_id`, `application_id`, `schedule_id` | Campaign interviews only; identifiers the candidate already holds (campaign start URL, own schedule) |

Never included (at any depth): `retrieved_sources`, `document_id`, `chunk_id`,
`scope`, `retrieved_context`, `follow_up_questions`, `history` (answers +
analysis), `evaluation`, `overall_score`, `skills`, `rubric_snapshot`,
`extracted_evidence`, `agent_decision`, `identified_gaps`, `errors`,
`question_source`, `questions_asked`, `next_action`, `recommended_next_action`,
`current_competency`, `difficulty`, `interview_plan`, `organization_id`,
`candidate_id`, `objective_version`, `plan_version`.

## Internal view

Full persisted state (unchanged, backward compatible), including
`retrieved_sources` at top level and on each `history` entry.

## Endpoints

| Endpoint | Callers | Response |
| --- | --- | --- |
| `POST /api/v1/interview/start` | recruiter, candidate (self) | view by role |
| `POST /api/v1/interview/campaigns/{campaign_id}/applications/{application_id}/start` | candidate | view by role |
| `POST /api/v1/interview/{id}/answer` | candidate only (staff 403) | candidate view |
| `GET /api/v1/interview/{id}` | candidate, recruiter, platform_admin | view by role |
| `GET /api/v1/interviews` | candidate, recruiter, platform_admin | view by role, per item |
| `POST /api/v1/interview/{id}/report` | candidate, recruiter, platform_admin | report from `ReportService` (unchanged; see below) |
| `WS /ws/interview/{id}` | members | sends only `connected` + `status`, echoes client messages |
| `WS /ws/interview/{id}/voice` | members | `interview.question` / `tts.*` events carry only `current_question`, transcripts and audio |

Service-layer methods (`InterviewService.*`) still return the full internal state;
projection happens at the HTTP boundary so internal callers (voice session,
reports, worker) keep working.

## Open product decisions

- **Report export for candidates**: `POST /interview/{id}/report` is allowed for
  candidates (covered by existing tests) and returns scores, competency
  evidence, gaps, `rubric_snapshot`, `ai_recommendation`. It does not contain
  retrieval metadata. Whether candidates should see evaluations is a product
  decision; the interview views hide evaluations by default.
- Whether to expose a candidate-safe progress indicator other than
  `questions_answered` / `question_budget`.
