# Project Context: AI Interview Platform → AI Recruitment Platform

## 1. Project Background

I am working with an existing repository:

`AI-Interviewer-Adaptive-Multimodal-AI-Interview-Platform`

The goal is to extend the existing AI interview platform into an AI recruitment platform with **exactly two independent agents**:

1. **Interview Agent**
2. **Browser Agent**

Before implementing anything, inspect the existing repository and understand its architecture. Do not assume that the existing agents already support the new recruitment workflow. In particular, the current Interview Agent may primarily support adaptive live interviews rather than job description generation and CV screening.

Preserve existing functionality and avoid unnecessary refactoring.

## 2. Target System Architecture

### Agent 1: Interview Agent

Responsibilities:
- Receive a hiring request from the client/HR.
- Generate a structured Job Description (JD) based on the client's requirements.
- Return the generated JD for review and approval before publication.
- Receive a batch of ranked candidates from the Browser Agent.
- Evaluate candidates against the approved JD and hiring requirements.
- Return a qualified shortlist with structured decisions and evidence-based reasons.

### Agent 2: Browser Agent

Responsibilities:
- Publish an approved JD to supported recruitment platforms, such as ITviec, TopCV, and a Facebook Page, subject to each platform's API, automation, and access restrictions.
- Collect CVs and normalize candidate information.
- Coordinate with the retrieval and reranking pipeline to identify and rank relevant candidates.
- Send a structured ranked candidate batch to the Interview Agent.
- Receive the qualified shortlist.
- Contact qualified candidates through supported communication channels.
- Track replies and coordinate interview scheduling.
- After a candidate confirms, create a calendar event and/or meeting link using supported Google Calendar and Zoom integrations.

Browser automation and external integrations must respect platform terms, authentication requirements, privacy obligations, and applicable access restrictions.

### Important Architectural Rule

There are exactly two agents. **RAG retrieval and reranking are supporting services/pipelines, not a third agent.**

The two agents must communicate through explicit structured data contracts and durable workflow state, not by sharing hidden reasoning or conversational context.

## 3. Agent Memory and Persistence

“Agent memory” primarily means persistent recruitment records and workflow state, not just LLM conversation history.

The system should be able to track:
- Job descriptions and approval/publication status.
- Candidate identity and contact information.
- Candidate CV references and retrieval metadata.
- Candidate rankings and screening results.
- Evidence and reasons supporting screening decisions.
- Outreach attempts and candidate replies.
- Interview scheduling and calendar/meeting references.
- Workflow status and timestamps.

Use a relational database for authoritative recruitment records and workflow state. Use a vector index and/or BM25 index for retrieval when appropriate. A vector database must not be the sole source of truth for candidate status or recruitment workflow history.

Reuse existing database entities when appropriate instead of duplicating them.

## 4. Existing Technology and Architecture Concepts

The repository already contains SQLAlchemy database models and database infrastructure, including concepts such as:
- `Base`, `engine`, `SessionLocal`
- `init_db()`
- `get_db_session()`
- `UserRecord`
- `CandidateRecord`
- `CandidateInvitationRecord`
- `InterviewRecord`
- `InterviewTurnRecord`
- `InterviewQuestionRecord`
- `InterviewEvidenceRecord`
- `InterviewCompetencyStateRecord`
- `InterviewAssessmentRecord`
- `RAGDocumentRecord`

The database URL resolver supports PostgreSQL and SQLite. The application uses Alembic migrations for schema changes; `Base.metadata.create_all()` is intended for controlled bootstrap/test scenarios rather than replacing the migration workflow.

`CandidateRecord` already stores basic candidate identity/contact data, including name, email, phone, resume URL, ownership/user references, and timestamps. Investigate its relationships and usage before changing it.

`RAGDocumentRecord` stores durable document/chunk information. Inspect the actual implementation before deciding whether it can support recruitment CV retrieval or whether additional metadata/models are needed.

The exact current implementation must be verified by reading the repository. Do not assume the list above is exhaustive or that all fields have the same definitions across versions.

## 5. Pydantic Schemas vs. SQLAlchemy Models vs. Services

Keep these responsibilities separate.

### Pydantic Schemas

Pydantic is a data validation/modeling library, not a FastAPI-only feature. FastAPI integrates it to validate request bodies and define response structures.

Schemas should define the data contracts used by API endpoints, services, and agent communication.

Potential recruitment schemas include:
- `CreateJobRequest`
- `JobDescriptionSchema`
- `CandidateProfileSchema`
- `RankedCandidateSchema`
- `RankedCandidateBatchSchema`
- `ScreeningResultSchema`
- `CandidateStatusSchema`

These names are suggestions, not instructions to create duplicates. Inspect existing schemas first.

Pydantic schemas validate and serialize data. They do not persist records in the database by themselves.

### SQLAlchemy Models

SQLAlchemy ORM models map Python classes to database tables and support reading/writing persistent records.

Existing models such as `CandidateRecord` should be reused where appropriate. New database entities may be needed for job descriptions, job applications, screening results, outreach, and scheduling.

Possible entities to investigate:
- Job records.
- Candidate-to-job application records.
- Screening results and supporting evidence.
- Outreach records.
- Scheduled interview/calendar records.

Do not create all of these automatically. First map the existing schema and determine the minimum additions required.

### Services

The service layer contains business logic and orchestrates operations such as:
- Generating a JD through an LLM.
- Saving and retrieving job records.
- Coordinating CV retrieval and reranking.
- Screening candidates.
- Updating application/workflow statuses.
- Sending outreach or scheduling interviews through external integrations.

Services may receive validated Pydantic objects, query/update the database through SQLAlchemy, call agents or LLMs, and invoke external APIs.

Services should not duplicate database models or unnecessarily embed business logic inside API route handlers.

### Typical Data Flow

`Client → FastAPI Route → Pydantic Request Schema → Service → Database / LLM / Retrieval Pipeline / External API`

For responses:

`Service Result → Pydantic Response Schema (when appropriate) → FastAPI Response`

Not every operation needs every component. For example, an operation that only processes in-memory data may not require a database query.

## 6. Initial Shared Data Contracts

The following are conceptual objects to evaluate against existing code:

- `JobDescription`: job ID, title, requirements, required skills, minimum experience, JD content, approval/publication status.
- `CandidateProfile`: candidate ID, name, skills, experience, CV reference.
- `RankedCandidateBatch`: job ID, ranked candidates, ranking scores, and relevant retrieval metadata.
- `ScreeningResult`: job ID, candidate ID, decision, evidence, and reasons.
- `CandidateStatus`: job ID, candidate ID, recruitment stage, and last-updated timestamp.

Candidate status is generally specific to a job application, so do not assume one global candidate status is sufficient for every job.

These are logical contracts, not necessarily database table definitions. Use separate Pydantic schemas and SQLAlchemy models where appropriate, and avoid duplicating the same class across agents.

## 7. Implementation Approach

Proceed incrementally and explain the relevant existing code before proposing changes.

Recommended order:

1. Inspect the repository structure, existing schemas, SQLAlchemy models, API routes, services, agent implementations, and Alembic migrations.
2. Map the existing components to the target architecture.
3. Identify what can be reused and what is missing.
4. Define or adapt shared Pydantic contracts for JD creation and agent communication.
5. Implement JD generation and its approval/persistence flow.
6. Implement or adapt CV ingestion, retrieval, and reranking using existing RAG components where suitable.
7. Implement candidate screening and structured shortlist generation.
8. Implement the Browser Agent's recruitment-platform integration workflow.
9. Add persistent outreach, reply tracking, and scheduling state.
10. Integrate calendar/meeting providers and add tests.

Do not implement the entire system in one large change. Start with the smallest useful vertical slice, then expand.

## 8. Coding Guidelines for the AI Assistant

- Inspect existing files before recommending or editing code.
- Use the repository's current conventions, configuration, dependency injection, and error-handling patterns.
- Search for existing schemas, services, models, and routes before creating new ones.
- Avoid duplicate classes, duplicate database tables, and unnecessary abstractions.
- Preserve the current adaptive interview functionality.
- Use shared structured contracts between agents.
- Keep Pydantic validation, service/business logic, SQLAlchemy persistence, and API routing as distinct responsibilities.
- Use Alembic migrations for database schema changes.
- Include appropriate error handling and tests.
- Do not claim that a feature or integration exists unless it is verified in the code.
- Do not overwrite files or make broad architectural changes without explaining the impact.
- When uncertain, ask a focused question or explain the options instead of inventing repository details.
- Prefer small, reviewable changes and explain why each change is needed.

## 10. Discovery Report (2026-10-14)

### Verified architecture

- Pydantic schemas are distributed across API routes, RAG indexing, evaluation, the Interview Agent graph, document indexing, and voice contracts. The main locations are `apps/api/v1/routes/*.py`, `rag/indexing/schemas/`, `evaluation/service.py`, `agents/interview_agent/graph/__init__.py`, `core/state_indexing.py`, and `voice/contracts.py`.
- SQLAlchemy models and database session management are centralized in `services/database.py`. It defines `Base`, `UserRecord`, `CandidateRecord`, interview records, `RAGDocumentRecord`, `engine`, `SessionLocal`, `init_db()`, and `get_db_session()`.
- `CandidateRecord` stores an independent candidate profile and ownership fields. It is not a candidate-to-job application record.
- `RAGDocumentRecord.job_id` is retrieval metadata only; it is not a persisted job or job-posting model.
- No verified database models or API routes exist for jobs/JDs, applications, screening results, outreach, or scheduling.
- `agents/interview_agent/graph/__init__.py` implements the adaptive live interview workflow. `agents/browser_agent/__init__.py`, `agents/question_agent/__init__.py`, and `agents/evaluation_agent/__init__.py` are placeholders. RAG retrieval/reranking is implemented as supporting code in `rag/service.py` and `agents/rag_agent/chunking_agent.py`.
- Existing reusable services include `services/candidate_store.py`, `services/interview_service.py`, `services/repositories.py`, `services/candidate_invitation_service.py`, `evaluation/service.py`, and `rag/service.py`.
- Alembic migrations currently cover the interview platform, RAG documents, ownership, and candidate invitations. No recruitment tables exist.

### Implementation status

- Discovery: completed.
- First vertical slice: implemented; behavior tests pending dependency setup.
- Immediate scope: persist recruiter-owned jobs with title, requirements/description, and lifecycle status; expose authenticated create/list/get operations; preserve existing interview flows.
- Deferred until this slice is verified: publication workflow, applications, screening, outreach, scheduling, and external platform integrations.

### Smallest safe next step

Add a `JobRecord` and its Alembic migration, then add dedicated Pydantic schemas, a recruiter-scoped service, API routes, and focused tests. The job ID will become the durable boundary that later application, screening, and RAG contracts can reference.

### First slice delivered

- Added `JobRecord` with recruiter ownership, description, requirements, lifecycle status, and timestamps.
- Added migration `c8d1_jobs` after `b6c9_candidate_invitations`.
- Added `CreateJobRequest` and `JobResponse` in `apps/api/v1/schemas/recruitment.py`.
- Added recruiter-only `POST /api/v1/jobs`, `GET /api/v1/jobs`, and `GET /api/v1/jobs/{job_id}`.
- Added recruiter-only `POST /api/v1/jobs/generate`, using the existing structured `LLMProvider` boundary and persisting generated output as a draft job.
- Added `SupervisorAgent` as the top-level orchestrator and `SupervisorService` as its application boundary. Recruiters request `POST /api/v1/jobs/{job_id}/approve`; the agent owns the approval transition and approved jobs are the only jobs exposed through the Browser Agent accessor.
- Added focused authorization and persistence tests in `tests/unit/test_jd.py`.
- `compileall` and `git diff --check` pass. Pytest/Alembic execution is currently blocked because the active `.venv` has no `pytest`, `fastapi`, `sqlalchemy`, or `alembic` packages installed.

## 9. Immediate Task

**Begin with repository discovery, not implementation.**

Inspect the existing project and report:

1. Where Pydantic schemas are currently defined.
2. Where SQLAlchemy models and database session management are defined.
3. Whether recruitment-specific job/JD, application, screening, outreach, and scheduling models already exist.
4. Which existing services and agents can be reused.
5. What the smallest safe next implementation step should be.

Provide exact file paths and explain the evidence from the code. Do not create or modify files until this initial architecture assessment is complete.