# PHASE 1 IMPLEMENTATION PLAN

## Production Core Foundation: Durable State + Real LangGraph + Turn Idempotency + RBAC

**Date:** 2026-09-02  
**Phase:** 1 of 10  
**Status:** PLANNING  
**Target Completion:** TBD

---

## PART 1: RE-AUDIT FINDINGS

### Current State Summary

The repository contains a **HYBRID database architecture** that creates confusion and technical debt:

#### Database Layer 1: SQLAlchemy (services/database.py)
- **Tables**: users, candidates, interviews
- **Status**: Defined but PARTIALLY used
- **Issue**: InterviewRecord table defined but NOT used by interview API

#### Database Layer 2: Raw SQLite3 (services/interview_store.py)
- **Tables**: interviews (single table)
- **Status**: ACTIVELY used by interview API
- **Issue**: Stores entire state as JSON blob in TEXT column
- **Conflict**: Bypasses SQLAlchemy, not PostgreSQL-ready

#### Current Interview Flow
```
FastAPI Route (interview.py)
    ↓
InterviewStore (raw sqlite3)  ← NOT using SQLAlchemy
    ↓
InterviewAgentCore (keyword matching)
    ↓
Raw JSON state blob
```

#### Authorization Gap
**CRITICAL SECURITY ISSUE:**
- Interview routes have **NO authorization checks**
- Any user with valid JWT can access any interview
- Candidate routes DO have `Depends(security)` but interview routes do NOT
- No tenant/organization isolation

#### Current Test Coverage
- Only 4 unit tests
- No idempotency tests
- No crash recovery tests
- No authorization tests
- No transaction tests

---

## PART 2: PROBLEMS PHASE 1 SOLVES

### P0 Issues (Blocking Production)

| Problem | Current State | Phase 1 Fix |
|---------|---------------|-----------|
| **Dual Database Layers** | SQLAlchemy + raw sqlite3 | Unify to single SQLAlchemy layer |
| **No Turn IDs** | State blob only | Add interview_turns table with UNIQUE(interview_id, turn_id) |
| **No Idempotency** | Retry = duplicate processing | Add idempotency_key + database constraints |
| **No Authorization** | Anyone can read any interview | Implement RBAC + resource ownership checks |
| **Keyword Matching** | Hardcoded if-else orchestration | Replace with LangGraph state machine |
| **Crash Recovery** | No way to resume partial state | Implement turn-level persistence + recovery |
| **No Migrations** | Schema via metadata.create_all() | Add Alembic for safe schema evolution |
| **No Tenant Isolation** | Single shared database | Add tenant_id/org_id to schema |

---

## PART 3: TARGET ARCHITECTURE

### Post-Phase 1 Flow

```
┌─────────────────────────────────────────┐
│         FastAPI Route Layer             │
│  (HTTP validation + auth + response)    │
└──────────────────┬──────────────────────┘
                   ↓
┌─────────────────────────────────────────┐
│   Authorization Middleware              │
│  (RBAC + resource ownership checks)     │
└──────────────────┬──────────────────────┘
                   ↓
┌─────────────────────────────────────────┐
│  Interview Application Service          │
│  (orchestration + business logic)       │
└──────────────────┬──────────────────────┘
                   ↓
┌─────────────────────────────────────────┐
│    LangGraph Runtime                    │
│  (agent graph execution + state mgmt)   │
└──────────────────┬──────────────────────┘
                   ↓
┌─────────────────────────────────────────┐
│  Repository/Data Access Layer           │
│  (SQLAlchemy + transactions)            │
└──────────────────┬──────────────────────┘
                   ↓
┌─────────────────────────────────────────┐
│   PostgreSQL / SQLite (durable)         │
│  (normalized relational schema)         │
└─────────────────────────────────────────┘
```

### Key Architectural Principles

1. **Single Source of Truth**: Database (not in-memory or JSON blob)
2. **Normalized Schema**: Tables for turns, questions, evidence (not JSON)
3. **LangGraph Orchestration**: Real state machine (not keyword matching)
4. **Transaction Safety**: All state changes within transactions
5. **Authorization at Service Layer**: Not just route-level
6. **Idempotent Processing**: Safe retries via database constraints
7. **Crash Recovery**: Persistent turn state allows resume

---

## PART 4: TARGET DATABASE SCHEMA

### Existing Tables (To Keep)

```sql
-- users (unchanged)
users
  id: UUID PK
  email: VARCHAR UNIQUE NOT NULL
  password_hash: VARCHAR NOT NULL
  role: VARCHAR NOT NULL
  created_at: TIMESTAMP
  updated_at: TIMESTAMP

-- candidates (unchanged, but add tenant_id)
candidates
  id: UUID PK
  tenant_id: UUID NOT NULL  -- NEW
  name: VARCHAR NOT NULL
  email: VARCHAR UNIQUE NOT NULL
  phone: VARCHAR
  resume_url: VARCHAR
  created_at: TIMESTAMP
  updated_at: TIMESTAMP
  FK: tenant_id → organizations.id
```

### New/Modified Tables

```sql
-- interviews (replace JSON blob with normalized state)
interviews
  id: UUID PK
  tenant_id: UUID NOT NULL
  candidate_id: UUID NOT NULL
  position: VARCHAR NOT NULL
  mode: VARCHAR NOT NULL
  status: VARCHAR NOT NULL  -- active, completed, cancelled
  current_turn_id: UUID
  difficulty: VARCHAR DEFAULT 'medium'
  started_at: TIMESTAMP
  completed_at: TIMESTAMP
  created_at: TIMESTAMP
  updated_at: TIMESTAMP
  version: INT DEFAULT 1  -- For optimistic locking
  FK: tenant_id → organizations.id
  FK: candidate_id → candidates.id
  FK: current_turn_id → interview_turns.id
  INDEX: (tenant_id, candidate_id, status)
  INDEX: (tenant_id, created_at DESC)

-- interview_turns (NEW - durable turn state)
interview_turns
  id: UUID PK
  interview_id: UUID NOT NULL
  turn_id: UUID NOT NULL  -- Client-stable ID
  sequence_number: INT NOT NULL
  status: VARCHAR NOT NULL  -- received, processing, completed, error
  question_id: UUID
  candidate_answer: TEXT
  started_at: TIMESTAMP
  completed_at: TIMESTAMP
  created_at: TIMESTAMP
  updated_at: TIMESTAMP
  version: INT DEFAULT 1
  FK: interview_id → interviews.id
  FK: question_id → interview_questions.id
  UNIQUE: (interview_id, turn_id)  -- CRITICAL for idempotency
  UNIQUE: (interview_id, sequence_number)
  INDEX: (interview_id, created_at)
  INDEX: (interview_id, status)

-- interview_questions (NEW - question history + state)
interview_questions
  id: UUID PK
  interview_id: UUID NOT NULL
  turn_id: UUID
  sequence_number: INT NOT NULL
  competency: VARCHAR
  difficulty: VARCHAR
  question_text: TEXT NOT NULL
  question_type: VARCHAR  -- free_form, multiple_choice, etc.
  status: VARCHAR NOT NULL  -- pending, sent, answered, cancelled
  generated_at: TIMESTAMP
  sent_at: TIMESTAMP
  created_at: TIMESTAMP
  updated_at: TIMESTAMP
  FK: interview_id → interviews.id
  FK: turn_id → interview_turns.id
  INDEX: (interview_id, created_at)
  INDEX: (interview_id, status)

-- interview_evidence (NEW - evidence extracted from answers)
interview_evidence
  id: UUID PK
  interview_id: UUID NOT NULL
  turn_id: UUID NOT NULL
  competency: VARCHAR NOT NULL
  evidence_text: TEXT NOT NULL
  strength: VARCHAR  -- strong, medium, weak
  specificity: VARCHAR  -- high, medium, low
  ownership: VARCHAR  -- explicit, implicit, none
  created_at: TIMESTAMP
  FK: interview_id → interviews.id
  FK: turn_id → interview_turns.id
  INDEX: (interview_id, competency)

-- interview_competency_state (NEW - interview-level competency tracking)
interview_competency_state
  id: UUID PK
  interview_id: UUID NOT NULL
  competency: VARCHAR NOT NULL
  evidence_count: INT DEFAULT 0
  confidence: FLOAT DEFAULT 0.0  -- 0.0 to 1.0
  strength: VARCHAR  -- strong, medium, weak, unknown
  last_updated_at: TIMESTAMP
  created_at: TIMESTAMP
  updated_at: TIMESTAMP
  FK: interview_id → interviews.id
  UNIQUE: (interview_id, competency)
  INDEX: (interview_id, confidence DESC)

-- organizations (NEW - tenant isolation)
organizations
  id: UUID PK
  name: VARCHAR NOT NULL
  created_at: TIMESTAMP
  updated_at: TIMESTAMP

-- (Optional) user_organization_mapping (NEW)
user_organization_mapping
  user_id: UUID FK → users.id
  organization_id: UUID FK → organizations.id
  role: VARCHAR  -- admin, interviewer, recruiter
  created_at: TIMESTAMP
  PRIMARY KEY: (user_id, organization_id)
```

### Migration Strategy

**Phase 1** will use Alembic to:

1. Create new tables (interview_turns, interview_questions, etc.)
2. Backfill interview data from old schema if applicable
3. Mark old interview_store.py schema as deprecated
4. Support gradual transition

**Transition Period:**
- New code uses normalized schema
- Old InterviewStore can coexist during tests
- Eventually remove raw SQLite3 access

---

## PART 5: TURN ID + IDEMPOTENCY DESIGN

### Turn Lifecycle

```
Client Request
    │
    ├─→ Contains: interview_id, turn_id (stable client ID)
    │
    ↓
API Layer
    ├─→ Validate interview exists
    ├─→ Check authorization (owner can submit turn)
    │
    ↓
Database Lookup
    ├─→ Query: SELECT * FROM interview_turns 
    │           WHERE interview_id = ? AND turn_id = ?
    ├─→ If EXISTS:
    │   └─→ Return existing turn state (IDEMPOTENT)
    ├─→ If NOT EXISTS:
    │   └─→ Proceed to processing
    │
    ↓
Transaction Begin
    ├─→ CREATE interview_turn (status = 'processing')
    ├─→ LangGraph execution
    ├─→ PERSIST evidence, questions
    ├─→ UPDATE interview_turn (status = 'completed')
    ├─→ UPDATE interview state
    │
    ↓
Transaction Commit
    ├─→ All-or-nothing
    ├─→ Unique constraint ensures no duplicate turns
    │
    ↓
Response
    └─→ Return turn + generated question
```

### Idempotency Guarantees

**Database-Level Guarantees:**
```sql
UNIQUE(interview_id, turn_id)
→ Prevents duplicate turn creation

UNIQUE(interview_id, sequence_number)
→ Prevents sequence conflicts
```

**Application-Level Guarantees:**
1. Before processing, check if turn exists
2. If exists, return cached result
3. If not exists, process within transaction
4. Unique constraint acts as circuit breaker

### Retry Semantics

| Scenario | Behavior |
|----------|----------|
| Same turn submitted twice | Returns first result (database constraint) |
| Concurrent same turn | Database serialization handles |
| Turn after API crash | Check turn status, resume if incomplete |
| Old stale turn | Reject if interview already advanced past it |

---

## PART 6: CRASH RECOVERY DESIGN

### Recovery Pattern: At-Least-Once + Idempotent Effects

**Example Sequence:**

```
Turn received
    ↓
BEGIN TRANSACTION
    ├─→ CREATE interview_turn (status='processing', sequence=5)
    └─→ COMMIT ✓
    
    ↓
LangGraph execution
    ├─→ Analyze answer
    ├─→ Extract evidence
    ├─→ [CRASH HERE] ← API dies
    
    ↓
On Restart (replay same turn)
    ├─→ SELECT * FROM interview_turns 
    │           WHERE interview_id = X AND turn_id = Y
    │
    ├─→ Found: sequence=5, status='processing'
    │
    ├─→ Resume: Check what was partially done
    │   ├─→ Evidence table: partially written
    │   └─→ Question table: not created yet
    │
    ├─→ Safe Actions:
    │   ├─→ Re-extract evidence (idempotent - use UPSERT)
    │   ├─→ Generate question (idempotent - check existing)
    │   └─→ Update turn status='completed'
```

### Recovery Implementation

**Phase 1 will implement:**

1. **Turn Status Field** in interview_turns table
   - Allows tracking: received → processing → completed

2. **Checkpoint Mechanism**
   - Record progress at each LangGraph node
   - On restart, skip completed nodes

3. **Idempotent Upserts**
   - Evidence: INSERT OR UPDATE
   - Questions: Check existing before creating

4. **Tests** for crash scenarios
   - Simulate crash at each node
   - Verify recovery produces same result

---

## PART 7: REAL LANGGRAPH AGENT DESIGN

### Current (Keyword Matching)
```python
def handle_answer(state, answer):
    if "production" in answer:
        next_question = "Tell me about..."
    elif "latency" in answer:
        next_question = "How did you measure..."
    else:
        next_question = "Tell me about a project..."
```

### Phase 1 Target (LangGraph State Machine)

```python
from langgraph.graph import StateGraph, START, END
from typing import TypedDict, Literal

class InterviewState(TypedDict):
    interview_id: str
    candidate_id: str
    position: str
    current_turn_id: str
    candidate_answer: str
    competencies: dict  # competency → strength
    evidence: list      # extracted evidence
    questions_asked: list
    question_pending: dict  # current question
    difficulty: str
    next_action: Literal["continue", "switch", "finish"]
    metadata: dict

graph = StateGraph(InterviewState)

# Nodes
def load_state(state: InterviewState) -> InterviewState:
    """Load interview and turn from database"""
    return state

def validate_turn(state: InterviewState) -> InterviewState:
    """Verify turn is valid and not duplicate"""
    return state

def analyze_answer(state: InterviewState) -> InterviewState:
    """Analyze candidate answer for quality/relevance"""
    return state

def extract_evidence(state: InterviewState) -> InterviewState:
    """Extract evidence from answer (Phase 1: deterministic; Phase 2: LLM)"""
    return state

def update_competencies(state: InterviewState) -> InterviewState:
    """Update competency scores based on evidence"""
    return state

def determine_strategy(state: InterviewState) -> InterviewState:
    """Decide next action: continue, switch, finish"""
    # Deterministic for Phase 1
    if state['evidence'] and state['competencies']['current'] > 0.8:
        state['next_action'] = 'switch'
    elif len(state['questions_asked']) > 8:
        state['next_action'] = 'finish'
    else:
        state['next_action'] = 'continue'
    return state

def generate_question(state: InterviewState) -> InterviewState:
    """Generate or select next question"""
    # Phase 1: deterministic; Phase 3: RAG-based
    return state

def persist_state(state: InterviewState) -> InterviewState:
    """Persist turn, evidence, question to database"""
    # Critical: All-or-nothing transaction
    return state

# Graph structure
graph.add_node("load_state", load_state)
graph.add_node("validate_turn", validate_turn)
graph.add_node("analyze_answer", analyze_answer)
graph.add_node("extract_evidence", extract_evidence)
graph.add_node("update_competencies", update_competencies)
graph.add_node("determine_strategy", determine_strategy)
graph.add_node("generate_question", generate_question)
graph.add_node("persist_state", persist_state)

# Edges
graph.add_edge(START, "load_state")
graph.add_edge("load_state", "validate_turn")
graph.add_edge("validate_turn", "analyze_answer")
graph.add_edge("analyze_answer", "extract_evidence")
graph.add_edge("extract_evidence", "update_competencies")
graph.add_edge("update_competencies", "determine_strategy")
graph.add_edge("determine_strategy", "generate_question")
graph.add_edge("generate_question", "persist_state")
graph.add_edge("persist_state", END)

interview_graph = graph.compile()
```

### Key Design Principles

1. **Typed State**: Use TypedDict, not arbitrary dicts
2. **Checkpointing**: Use LangGraph's built-in checkpointing
3. **Database State ≠ Graph State**:
   - Graph state: execution context
   - Database state: durable business state
4. **Deterministic for Phase 1**: No LLM reasoning in graph yet
5. **Testable**: Each node is independently testable
6. **Extensible**: Phase 2/3 can add LLM nodes without restructuring

---

## PART 8: AUTHORIZATION & RBAC DESIGN

### Current Gap

**Interview routes have NO authorization:**
```python
@router.post("/interview/start")
async def start_interview(payload: InterviewStartRequest):
    # ❌ Missing: Are you authorized to create interview for this candidate?
    state = agent.start_interview(...)
    return state
```

### Phase 1 RBAC Model

#### Roles

```
admin
  → manage users
  → manage all interviews
  → view all candidates
  
interviewer
  → create interviews for assigned candidates
  → view interviews they created
  → cannot view other interviewers' interviews
  
candidate
  → view own interviews
  → submit answers
  → view own report
  
recruiter (optional)
  → view all candidates in org
  → view all completed interviews
  → cannot start new interviews
```

#### Authorization Checks

**Interview Creation:**
```python
@router.post("/interview/start")
async def start_interview(payload, credentials=Depends(security)):
    user = get_current_user(credentials)
    
    # ✓ NEW: Check authorization
    if user['role'] not in ['admin', 'interviewer']:
        raise PermissionDenied()
    
    candidate = get_candidate(payload.candidate_id)
    if candidate.tenant_id != user.tenant_id:
        raise PermissionDenied()  # Cross-tenant access blocked
    
    # ... proceed
```

**Interview Access:**
```python
@router.get("/interview/{interview_id}")
async def get_interview(interview_id, credentials=Depends(security)):
    user = get_current_user(credentials)
    interview = get_interview(interview_id)
    
    # ✓ NEW: Resource ownership check
    if interview.tenant_id != user.tenant_id:
        raise PermissionDenied()
    
    if user['role'] == 'interviewer':
        # Can only see interviews they created
        if interview.created_by != user.id:
            raise PermissionDenied()
    
    return interview
```

### Implementation

**Phase 1 will:**

1. Add `tenant_id` and `created_by` to interview schema
2. Add authorization middleware/decorators
3. Implement resource ownership checks
4. Add RBAC tests
5. Document authorization matrix

---

## PART 9: APPLICATION SERVICE LAYER

### Current Problem

**Business logic is in routes:**
```python
# ❌ Route contains orchestration
@router.post("/interview/{interview_id}/answer")
async def submit_answer(interview_id: str, payload: InterviewAnswerRequest):
    current_state = store.get(interview_id)
    updated_state = agent.handle_answer(current_state, payload.answer)
    evaluation = evaluator.evaluate_answer(...)
    updated_state["evaluation"] = evaluation
    store.save(interview_id, updated_state)
    return updated_state
```

### Phase 1 Solution

**Extract application service:**
```python
# services/interview_application_service.py
class InterviewApplicationService:
    def __init__(
        self,
        interview_repository,
        interview_agent,
        evaluator,
        rag_service,
        report_service
    ):
        self.interview_repo = interview_repository
        self.agent = interview_agent
        self.evaluator = evaluator
        self.rag = rag_service
        self.report = report_service
    
    def start_interview(
        self,
        tenant_id: str,
        candidate_id: str,
        position: str,
        mode: str
    ) -> dict:
        """Orchestrate interview start"""
        # Validation, authorization, business logic here
        # Call repositories/agent
        # Return result
        pass
    
    def submit_answer(
        self,
        tenant_id: str,
        interview_id: str,
        turn_id: str,
        answer: str
    ) -> dict:
        """Orchestrate answer submission"""
        # Idempotency check
        # Call LangGraph
        # Persist turn + evidence + question
        # Return turn state
        pass
```

**Route becomes thin:**
```python
@router.post("/interview/{interview_id}/answer")
async def submit_answer(
    interview_id: str,
    payload: InterviewAnswerRequest,
    credentials=Depends(security)
):
    user = get_current_user(credentials)
    
    # ✓ Delegate to service
    result = interview_service.submit_answer(
        tenant_id=user['tenant_id'],
        interview_id=interview_id,
        turn_id=payload.turn_id,
        answer=payload.answer
    )
    
    return result
```

### Dependency Injection

```python
# apps/api/main.py
from services.interview_application_service import InterviewApplicationService

interview_service = InterviewApplicationService(
    interview_repository=InterviewRepository(),
    interview_agent=InterviewAgentRuntime(),
    evaluator=EvaluationService(),
    rag_service=RAGQuestionService(),
    report_service=ReportService()
)

# Inject into routes or make available globally
```

---

## PART 10: TESTING STRATEGY

### Phase 1 Test Coverage

| Category | Tests | Purpose |
|----------|-------|---------|
| **Database** | 8-10 | Schema, migrations, constraints |
| **Idempotency** | 6-8 | Duplicate turns, retries |
| **Authorization** | 8-10 | RBAC, tenant isolation |
| **LangGraph** | 6-8 | Node execution, routing |
| **Crash Recovery** | 6-8 | Partial state resume |
| **API Integration** | 8-10 | Full flow E2E |
| **Transaction Safety** | 4-6 | Concurrent access |
| **Total** | 46-60 | (vs current 4) |

### Test Examples

#### Test: Idempotent Duplicate Turn
```python
def test_submit_same_turn_twice():
    """Verify duplicate turn returns same result"""
    # Setup
    interview_id = create_interview(...)
    turn_id = uuid4()
    answer = "I built a production ML system"
    
    # First submission
    result1 = interview_service.submit_answer(
        interview_id=interview_id,
        turn_id=turn_id,
        answer=answer
    )
    
    # Second submission (identical)
    result2 = interview_service.submit_answer(
        interview_id=interview_id,
        turn_id=turn_id,
        answer=answer
    )
    
    # Assert: same result
    assert result1['turn_id'] == result2['turn_id']
    assert result1['question'] == result2['question']
    assert result1['sequence_number'] == result2['sequence_number']
```

#### Test: Crash Recovery
```python
def test_crash_recovery_mid_turn():
    """Verify system recovers after crash during processing"""
    # Setup
    interview_id = create_interview(...)
    turn_id = uuid4()
    
    # Simulate partial execution: create turn, crash before question generation
    db.create_turn(interview_id, turn_id, status='processing')
    db.create_evidence(interview_id, turn_id, ...)
    # CRASH: process dies here
    
    # On restart, retry same turn
    result = interview_service.submit_answer(
        interview_id=interview_id,
        turn_id=turn_id,
        answer="..."
    )
    
    # Assert: turn completed successfully
    assert result['status'] == 'completed'
    assert result['turn_id'] == turn_id
    # Evidence was not duplicated
    assert len(db.get_evidence(interview_id, turn_id)) == 1
```

#### Test: Authorization Enforcement
```python
def test_candidate_cannot_access_other_candidate_interview():
    """Verify cross-candidate interview access is blocked"""
    user_a = create_user("a@example.com")
    user_b = create_user("b@example.com")
    
    interview = create_interview(candidate_a)
    
    # User B tries to access interview of Candidate A
    with pytest.raises(PermissionDenied):
        interview_service.get_interview(
            tenant_id=user_b['tenant_id'],
            interview_id=interview['id']
        )
```

---

## PART 11: MIGRATION STRATEGY

### Alembic Setup

**Phase 1 will:**

1. Initialize Alembic (`alembic init`)
2. Create initial migration file
3. Support both SQLite (dev) and PostgreSQL (prod)
4. Document migration commands

**Commands:**
```bash
# Create new migration
alembic revision --autogenerate -m "Add interview_turns table"

# Apply migrations
alembic upgrade head

# Rollback
alembic downgrade -1

# Check current version
alembic current
```

### Data Migration

**If Old Data Exists:**

```python
def migration_create_turns_from_json_state():
    """Migrate old JSON state blob to normalized tables"""
    
    old_interviews = db.query(InterviewRecord).all()
    
    for old_interview in old_interviews:
        state = json.loads(old_interview.state_json)
        
        # Create interview_turns from history
        for i, answer_entry in enumerate(state['history']):
            turn = InterviewTurn(
                interview_id=old_interview.id,
                turn_id=uuid4(),  # Generate stable ID
                sequence_number=i+1,
                status='completed',
                candidate_answer=answer_entry['answer'],
                created_at=old_interview.created_at
            )
            db.add(turn)
        
        db.commit()
```

### Backward Compatibility

- Old code can use InterviewRecord until migration completes
- New code uses normalized schema
- Gradual transition prevents disruption

---

## PART 12: BACKWARD COMPATIBILITY & ROLLBACK

### Strategy

1. **Coexistence Period**:
   - New schema and old InterviewStore can run simultaneously
   - Routes can be gradually migrated

2. **Dual Write** (if needed):
   - Write to both SQLAlchemy and SQLite during transition
   - Verify both remain consistent

3. **Rollback Plan**:
   - Keep old InterviewStore code intact
   - If new schema fails, switch back to routes using old store
   - Database rollback via `alembic downgrade`

4. **Data Safety**:
   - Never destructively migrate without backup
   - Test migrations on copy of production data first

---

## PART 13: FILES TO CREATE / MODIFY

### New Files to Create

```
alembic/
  ├── versions/
  │   └── 001_initial_schema.py
  ├── env.py
  ├── script.py.mako
  └── alembic.ini

services/
  ├── repositories/
  │   ├── __init__.py
  │   ├── interview_repository.py
  │   ├── turn_repository.py
  │   └── evidence_repository.py
  ├── interview_application_service.py
  └── interview_agent_runtime.py

apps/api/middleware/
  ├── __init__.py
  └── authorization.py

tests/unit/
  ├── test_database_schema.py
  ├── test_idempotency.py
  ├── test_authorization.py
  ├── test_langgraph_agent.py
  ├── test_crash_recovery.py
  └── test_api_integration.py

docs/
  ├── PHASE_1_ARCHITECTURE.md
  ├── PHASE_1_SCHEMA.md
  ├── PHASE_1_TURN_LIFECYCLE.md
  └── PHASE_1_AUTHORIZATION.md
```

### Files to Modify

```
services/database.py
  - Add new tables (interview_turns, evidence, competency_state, organization)
  - Add foreign keys
  - Add indices

services/interview_store.py
  - Mark as deprecated
  - Keep for backward compatibility during transition

agents/interview_agent/graph/__init__.py
  - Replace InterviewAgentCore with LangGraph version
  - Keep old version for reference

apps/api/v1/routes/interview.py
  - Add authorization checks
  - Delegate to interview_application_service
  - Remove direct orchestration logic

apps/api/main.py
  - Instantiate interview_application_service
  - Add authorization middleware

config/settings.py
  - Add ALEMBIC_* configuration
  - Add authorization constants (roles, etc.)

requirements.txt
  - Add: alembic
  - Update: langgraph (ensure real graph support)

tests/unit/test_auth_candidate_db.py
  - Add authorization tests
  - Add turn idempotency tests

.github/workflows/test.yml
  - Remove || true masks (enforce quality gates)
  - Add migration testing

docker-compose.yml
  - Add alembic migration step in startup
  - Ensure PostgreSQL for production-like testing
```

### Files to Remove/Deprecate

```
services/interview_store.py
  - Mark as DEPRECATED
  - Recommend using repositories instead
  - Remove after Phase 1 + verification period

agents/interview_agent/graph/__init__.py (old version)
  - Replace entirely with LangGraph version
  - Keep in git history for reference
```

---

## PART 14: INCREMENTAL IMPLEMENTATION STEPS

### Step 1: Database Schema & Migrations (Days 1-2)

**Tasks:**
1. Add Alembic to project
2. Create `alembic/versions/001_initial_schema.py`
3. Define new tables (interview_turns, evidence, competency_state, organization)
4. Test migration creates schema
5. Test migration works with PostgreSQL + SQLite

**Output:**
- Alembic working
- New schema defined
- Migration tested

### Step 2: Repositories & Data Access (Days 3-4)

**Tasks:**
1. Create `services/repositories/interview_repository.py`
2. Implement CRUD for interviews with authorization
3. Create turn repository
4. Create evidence repository
5. Add transactional methods

**Output:**
- Data access layer isolated
- Repository tests passing

### Step 3: Turn IDs & Idempotency (Days 4-5)

**Tasks:**
1. Update InterviewTurn schema
2. Add database constraints (UNIQUE turns)
3. Implement idempotency checks in repository
4. Add idempotent upsert for evidence
5. Write idempotency tests

**Output:**
- Duplicate turns rejected by database
- Idempotency tests passing

### Step 4: Authorization & RBAC (Days 5-6)

**Tasks:**
1. Add tenant_id/org_id to schema
2. Implement RBAC decorators
3. Add authorization middleware
4. Update routes with auth checks
5. Write authorization tests

**Output:**
- Routes protected
- Resource ownership enforced
- RBAC tests passing

### Step 5: LangGraph Agent (Days 6-7)

**Tasks:**
1. Design InterviewState TypedDict
2. Implement LangGraph nodes
3. Connect graph
4. Add checkpointing
5. Write agent tests

**Output:**
- LangGraph working
- Agent tests passing
- Testable node structure

### Step 6: Application Service (Days 7-8)

**Tasks:**
1. Create `services/interview_application_service.py`
2. Implement business orchestration
3. Add idempotency logic
4. Add crash recovery patterns
5. Integration tests

**Output:**
- Service layer handling orchestration
- Routes simplified
- Integration tests passing

### Step 7: Update Routes & Integration (Days 8-9)

**Tasks:**
1. Refactor interview routes to use service
2. Add authorization to all routes
3. Update request/response schemas
4. Add stable IDs to API contracts
5. API integration tests

**Output:**
- Routes simplified
- Authorization working
- API contracts stable

### Step 8: Comprehensive Testing (Days 9-10)

**Tasks:**
1. Write database tests
2. Write crash recovery tests
3. Write authorization tests
4. Write E2E interview flow
5. Document test coverage

**Output:**
- 50+ tests passing
- High coverage
- Production-ready reliability

---

## PART 15: DEFINITION OF DONE

### Phase 1 Complete When ALL True:

#### Architecture ✓
- [ ] LangGraph graph replaces keyword-matching
- [ ] API routes delegate to application service
- [ ] Application service orchestrates agent
- [ ] Clear repository/data-access boundaries
- [ ] No circular dependencies

#### Database ✓
- [ ] Alembic migrations working
- [ ] Normalized schema (turns, questions, evidence)
- [ ] Foreign keys and constraints enforced
- [ ] Indices on high-query columns
- [ ] PostgreSQL-compatible schema

#### Reliability ✓
- [ ] Turn IDs implemented
- [ ] Idempotent retries working
- [ ] Duplicate turns rejected
- [ ] Database constraints verified
- [ ] UNIQUE(interview_id, turn_id) enforced

#### Authorization ✓
- [ ] RBAC implemented
- [ ] Resource ownership checks
- [ ] Tenant isolation
- [ ] All protected endpoints verified
- [ ] Authorization tests passing

#### Crash Recovery ✓
- [ ] Turn status tracking
- [ ] Checkpoint mechanism
- [ ] Partial state resume
- [ ] Idempotent re-execution
- [ ] Recovery tests passing

#### Testing ✓
- [ ] 50+ tests (vs current 4)
- [ ] Database schema tests
- [ ] Idempotency tests
- [ ] Authorization tests
- [ ] Crash recovery tests
- [ ] LangGraph tests
- [ ] API integration tests
- [ ] All tests passing

#### Code Quality ✓
- [ ] Type hints on all new code
- [ ] No circular dependencies
- [ ] Meaningful error messages
- [ ] Clear domain boundaries
- [ ] No hardcoded credentials
- [ ] Linting/formatting passing
- [ ] No `|| true` in CI

---

## PART 16: KNOWN CONSTRAINTS & ASSUMPTIONS

### Constraints

1. **LangGraph Learning Curve**: Requires understanding LangGraph patterns
2. **Database Migration Complexity**: Need careful planning to avoid data loss
3. **Authorization Retrofit**: Existing interviews may have unclear ownership
4. **Testing Infrastructure**: Need to set up comprehensive test suite
5. **Backward Compatibility**: Old code may need to run alongside new

### Assumptions

1. PostgreSQL will be used for production (SQLite for dev)
2. Tenants/organizations will be implemented
3. LangGraph checkpointing is acceptable for state management
4. At-least-once processing semantics are acceptable
5. Interview-level idempotency is sufficient (no sub-turn idempotency)

---

## PART 17: RISKS & MITIGATION

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Data loss during migration | High | Backup before migration, test on copy first |
| LangGraph overhead | Medium | Profile before optimization |
| Authorization gaps | High | Comprehensive test coverage required |
| Schema evolution | Medium | Alembic provides safe migrations |
| Concurrent turns | Medium | Database locks + tests verify safety |
| Backward compatibility | Medium | Keep old code, gradual transition |

---

## PART 18: SUCCESS CRITERIA

### Quantitative

- ✅ All tests passing (50+ tests)
- ✅ Zero authorization bypass vulnerabilities
- ✅ Idempotent retries work 100% of time
- ✅ Crash recovery works for all node boundaries
- ✅ Schema migrations work on clean database
- ✅ No data loss in old→new migration

### Qualitative

- ✅ Architecture is clearly structured
- ✅ Routes are thin (20 lines max)
- ✅ Business logic is in services
- ✅ Data access is isolated in repositories
- ✅ LangGraph makes decision flow explicit
- ✅ Authorization is enforced consistently
- ✅ Code is maintainable for future developers

---

## PART 19: DO NOT IMPLEMENT THESE YET

**Explicitly out of scope for Phase 1:**

- ❌ Real RAG retrieval
- ❌ Embeddings
- ❌ BM25 / hybrid search
- ❌ Reranking
- ❌ Real STT/TTS
- ❌ VAD
- ❌ Browser automation
- ❌ Kubernetes
- ❌ Distributed WebSocket
- ❌ Celery queues
- ❌ LLM-based evaluation
- ❌ LLM-based question generation
- ❌ Prometheus/observability
- ❌ Fairness evaluation
- ❌ A/B testing
- ❌ Load testing

**However**, Phase 1 interfaces must support these additions without restructuring.

---

## PART 20: DELIVERABLES

### At End of Phase 1, Deliver:

1. **Code**
   - All new files, modified files
   - All tests passing
   - Clean git history

2. **Documentation**
   - PHASE_1_IMPLEMENTATION_REPORT.md
   - Architecture decision record
   - Database schema diagram
   - LangGraph graph diagram
   - Authorization matrix
   - API contract documentation
   - Migration guide

3. **Tests**
   - 50+ unit/integration tests
   - Test results report
   - Coverage report

4. **Verification**
   - Manual testing checklist (passed)
   - Database constraint verification
   - Authorization verification
   - Crash recovery verification
   - Idempotency verification

---

## PHASE 1 SUMMARY

| Dimension | Value |
|-----------|-------|
| **Duration** | ~10 days |
| **Effort** | ~80-100 hours |
| **Complexity** | High |
| **Risk** | Medium (mitigated by tests) |
| **Production Ready** | ✓ Foundation + RBAC |
| **Tests Added** | ~50 |
| **Database Tables Added** | 5-6 |
| **Files Created** | 20+ |
| **Files Modified** | 10+ |
| **Breaking Changes** | Minimal (backward compatible) |

---

## Next Steps

1. **Review this plan** with team
2. **Adjust scope** if necessary
3. **Approve** implementation approach
4. **Begin STEP 1** (Database + Alembic)
5. **Execute incrementally** following steps 1-8
6. **Track progress** against this plan
7. **Document findings** in PHASE_1_IMPLEMENTATION_REPORT.md

**Plan Status:** ✅ READY FOR IMPLEMENTATION

---

**Document Version:** 1.0  
**Last Updated:** 2026-09-02  
**Status:** APPROVED FOR PHASE 1 START
