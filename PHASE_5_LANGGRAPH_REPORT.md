# Phase 5 LangGraph Report

**Date:** 2026-09-02  
**Status:** PARTIAL, locally verified

## 1. Inspected Before Changes

- SQLAlchemy models and Alembic head `7a1f_owner_and_turn_states`
- Repository/service transaction and authorization boundaries
- Turn uniqueness, lifecycle, stale recovery, and existing hardening tests
- Existing keyword-only `InterviewAgentCore`
- Python environment and declared LangGraph dependency

PostgreSQL/Docker is unavailable locally, so production database verification remains blocked.
LangGraph was installed successfully in the active Python 3.14 environment.

## 2. Architecture Before Changes

The service owned durable writes, but the agent was a procedural keyword matcher. It mutated a
state dictionary and selected fixed questions directly from substring checks. The service then
ran separate keyword RAG and evaluation shims before committing the snapshot.

## 3. LangGraph Architecture

The new `InterviewAgentCore` compiles a `StateGraph` with these nodes:

```text
START
  -> analyze_answer
  -> extract_evidence
  -> update_competencies
  -> identify_gaps
  -> decide_next_action
  -> [conditional route]
  -> generate_question
  -> validate_question
  -> END
```

The conditional route is selected from evidence strength, identified gaps, accumulated evidence,
and conversation history. It supports `DIG_DEEPER`, `INCREASE_DIFFICULTY`, `CHANGE_COMPETENCY`,
and `FINISH` decisions. `DECREASE_DIFFICULTY` is represented in the validated decision contract
but is not selected by the current local policy.

## 4. Graph State

`InterviewGraphState` contains only execution inputs and semantic outputs: interview/turn IDs,
latest answer, conversation reference, competency state, structured evidence, gaps, difficulty,
decision, generated question, and errors. The graph does not own database sessions or durable
business state.

## 5. Node Responsibilities

- `analyze_answer`: invokes the injected structured analyzer and validates its output.
- `extract_evidence`: exposes evidence gaps for later routing.
- `update_competencies`: updates the in-memory execution competency view.
- `identify_gaps`: supplies a conservative fallback gap when evidence is incomplete.
- `decide_next_action`: makes the conditional adaptive decision.
- `generate_question`: creates a validated proposal based on action, competency, and gaps.
- `validate_question`: validates the proposal and changes an exact repeated question.

## 6. Semantic Adapter and Confidence

The default adapter is deterministic and local because no provider integration was requested in
this phase. It produces structured evidence strength (`weak`, `moderate`, `strong`) and gaps; it
does not claim calibrated probability of candidate skill. The adapter is injectable, so a future
LLM provider can return the same Pydantic contract without moving persistence into graph nodes.

## 7. Persistence Boundary and Idempotency

`InterviewService` remains responsible for loading authorized durable state and committing it.
The graph receives a snapshot and returns semantic state. The service passes the durable `turn_id`
into graph execution and persists the result through repositories. Existing unique turn handling
and question sequence constraints remain in force.

Question generation is deterministic for a given decision. The service's existing duplicate-turn
replay prevents a completed retry from invoking the graph again, and question validation prevents
an exact repeated question in one graph execution history.

## 8. Failure and Retry Strategy

Invalid structured analyzer output and `TimeoutError` produce conservative fallback evidence and
an explicit graph error without corrupting graph state. The service's existing durable turn
processing/failure handling controls retryable database lifecycle transitions. There is no actual
external provider call, bounded provider retry loop, or checkpoint-backed worker in this phase.

## 9. Checkpointing Strategy

No LangGraph checkpointer was added. This is deliberate: PostgreSQL durable interview state and
turn idempotency remain authoritative. LangGraph in-memory execution is not presented as crash
recovery or exactly-once delivery.

## 10. Tests and Verification

```text
python -m pytest
25 passed, 52 warnings

Focused Phase 5 run:
18 passed, 54 warnings

python -m alembic downgrade -1
python -m alembic upgrade head
python -m alembic current
7a1f_owner_and_turn_states (head)
```

Behavior tests cover graph startup, evidence extraction, competency updates, gap routing,
deeper probes, difficulty increase, competency change, finish behavior, invalid structured output,
timeouts, duplicate execution, authorization, and recruiter mutation denial.

Diagnostics for touched graph/service/test files are clean. `git diff --check` passes.

## 11. Status and Limitations

| Capability | Status |
|---|---|
| Real compiled LangGraph | IMPLEMENTED |
| Conditional adaptive routing | IMPLEMENTED |
| Typed structured output validation | IMPLEMENTED |
| Deterministic local semantic adapter | MOCKED/local fallback |
| External LLM provider | NOT IMPLEMENTED |
| Bounded external LLM retry | NOT IMPLEMENTED |
| LangGraph checkpointing | NOT IMPLEMENTED |
| Durable graph state | NOT IMPLEMENTED; service/database remain authoritative |
| PostgreSQL verification | BLOCKED |
| RAG, voice, browser, Redis, Celery, Kubernetes | NOT IMPLEMENTED in this phase |