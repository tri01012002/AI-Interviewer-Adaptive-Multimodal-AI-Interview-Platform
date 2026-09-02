# Phase 6 Evidence-Based Evaluation Report

**Date:** 2026-09-02  
**Status:** PARTIAL, locally verified

## Existing Implementation Before Phase 6

Evaluation was a keyword-count function returning a single dictionary. Evidence, score,
confidence, and weaknesses were derived together, and normalized evidence and competency tables
were not written by the interview service.

## New Evaluation Architecture

The evaluation domain now separates:

- `EvidenceItem`: explicit candidate-grounded evidence with competency, type, strength,
  specificity, and relevance.
- `CompetencyAssessment`: score, assessment status, evidence strength, evidence-sufficiency
  confidence, strengths, gaps, rationale, and evaluator metadata.
- `EvaluationResult`: evidence plus one competency assessment.
- `Rubric`: competency-specific criteria passed into the evaluator interface.
- `EvaluationService.aggregate`: interview-level aggregation that excludes `NOT_ASSESSED`
  competencies from the average and reports evidence coverage separately.

The evaluator interface is provider-neutral and accepts `question`, `answer`, `competency`, and
`rubric`. The current implementation is `DeterministicEvaluator` only.

## Score Semantics

The domain uses a bounded 0-5 scale:

- 0: no demonstrated evidence / not assessed
- 1: weak
- 2: insufficient or developing evidence
- 3: competent
- 4: strong evidence
- 5: exceptional

The deterministic policy currently produces 0, 2, or 4 based on explicit evidence. It does not
claim that a score is objective ground truth.

## Confidence Semantics

Confidence means confidence that the available evidence is sufficient and reliable for the
assessment. It is not the probability that the candidate possesses a skill and is not calibrated.
Empty or unrelated answers remain `NOT_ASSESSED`; absence of evidence is not treated as proof of
poor ability.

## Durable Persistence

Migration `8b2f_evidence_assessments` adds evidence metadata and the `interview_assessments`
table. Assessment identity is unique by `(interview_id, turn_id, competency)`. The repository
persists evidence linked to interview, database turn, and answered question, then persists the
assessment and updates competency state within the existing answer transaction. A duplicate
completed turn replays without re-running evaluation effects.

## LangGraph Integration

The Phase 5 graph produces structured answer evidence and gaps for adaptive routing. The service
then invokes the reusable evaluator for each detected competency and persists its result. Graph
execution remains in-memory semantic state; the service and database remain authoritative for
business state and transaction control.

## Telemetry and Security

Evaluation telemetry records competency, evaluator type/version, duration, and success without
logging the candidate answer. Evaluation runs only after the existing authorized interview lookup;
cross-user interview access remains denied by repository filtering, and recruiter answer mutation
remains forbidden.

## Tests and Verification

```text
python -m pytest
37 passed, 83 warnings

Focused Phase 6:
python -m pytest tests/unit/test_evaluation_domain.py
6 passed, 2 warnings

python -m alembic current
8b2f_evidence_assessments (head)

python -m alembic downgrade -1
python -m alembic upgrade head
8b2f_evidence_assessments (head)

git diff --check
passed
```

Coverage includes evidence persistence and links, strong/weak/missing evidence, non-invention,
score/confidence bounds, aggregation, evaluator version persistence, retry idempotency, ownership,
and recruiter mutation denial. Touched files report no editor diagnostics.

## Status and Limitations

| Capability | Status |
|---|---|
| Evidence domain model | IMPLEMENTED |
| Competency assessment model | IMPLEMENTED |
| Rubric abstraction | IMPLEMENTED |
| Deterministic evaluator | IMPLEMENTED |
| Interview-level aggregation | IMPLEMENTED |
| Atomic evidence/assessment/turn persistence | IMPLEMENTED locally |
| External LLM evaluator | NOT IMPLEMENTED |
| LLM prompt/model versioning | NOT IMPLEMENTED |
| Statistical score calibration | NOT IMPLEMENTED |
| RAG context/reranking | NOT IMPLEMENTED |
| PostgreSQL migration/concurrency verification | BLOCKED |
| Load testing | NOT IMPLEMENTED |

## Future LLM Evaluator Plan

A future `LLMEvaluator` may implement the existing `Evaluator` protocol and return the same
Pydantic contracts. It must use bounded provider calls, validate output, version its model/prompt,
and preserve the distinction between evidence, score, and evidence-sufficiency confidence. No
fake LLM call or external provider integration was added in Phase 6.

## PostgreSQL Status

**POSTGRESQL VERIFICATION: BLOCKED.** Docker/PostgreSQL is unavailable in this environment.
The migration and full suite were verified against the local SQLite database only.

## Change Boundary

RAG, reranking, voice, browser automation, Redis, Celery, Kubernetes, and external LLM integration
were intentionally not started.
