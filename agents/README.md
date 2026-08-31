# Agents Module

Core LangGraph-based agents for AI Interviewer

## Directory Structure

```
agents/
├── interview_agent/          # Main orchestrator
│   ├── state.py             # InterviewState definition
│   ├── graph.py             # LangGraph workflow
│   ├── nodes/               # Individual workflow nodes
│   │   ├── planner.py
│   │   ├── question_generator.py
│   │   ├── answer_analyzer.py
│   │   └── evaluator.py
│   ├── tools/               # Tools available to agent
│   │   ├── rag_tool.py
│   │   ├── timer_tool.py
│   │   └── state_tool.py
│   └── prompts/             # Prompt templates
│       ├── system.txt
│       └── examples.txt
│
├── question_agent/          # Question generation
│   ├── state.py
│   ├── graph.py
│   ├── tools/
│   └── prompts/
│
├── evaluation_agent/        # Answer evaluation
│   ├── state.py
│   ├── graph.py
│   ├── scoring/
│   │   ├── rubric.py
│   │   ├── confidence.py
│   │   └── evidence.py
│   └── prompts/
│
├── browser_agent/           # Browser automation (future)
│   ├── state.py
│   ├── graph.py
│   ├── actions/
│   ├── validators/
│   └── prompts/
│
└── orchestrator.py          # Coordinate all agents
```

## Key Components

### 1. Interview Agent (Main)

Orchestrates the entire interview process:

```python
from agents.interview_agent import InterviewAgent

agent = InterviewAgent()
result = await agent.run(interview_state)
```

**Workflow:**
1. Load State
2. Analyze Candidate
3. Determine Next Objective
4. Retrieve Knowledge (RAG)
5. Generate Question
6. Validate Question
7. Ask Candidate
8. Receive Answer
9. Analyze Answer
10. Score Answer
11. Update State
12. Route (Continue or End?)

### 2. Question Agent

Specialized for generating adaptive questions:

```python
from agents.question_agent import QuestionAgent

qa = QuestionAgent()
question = await qa.generate(
    skills_gap=["RAG", "LLMOps"],
    difficulty="hard",
    context=conversation
)
```

### 3. Evaluation Agent

Scores and evaluates candidate answers:

```python
from agents.evaluation_agent import EvaluationAgent

evaluator = EvaluationAgent()
evaluation = await evaluator.evaluate(
    answer="...",
    rubric=rubric,
    context=conversation
)
```

### 4. Browser Agent (Future)

Automates recruiter portal workflows:

```python
from agents.browser_agent import BrowserAgent

browser_agent = BrowserAgent()
await browser_agent.upload_report(report, interview_id)
```

## Development

### Creating a New Agent

1. Define state:
```python
# agents/my_agent/state.py
from typing_extensions import TypedDict

class MyAgentState(TypedDict):
    input: str
    output: str
    context: dict
```

2. Implement nodes:
```python
# agents/my_agent/nodes/my_node.py
def my_node(state: MyAgentState) -> MyAgentState:
    # Process
    return state
```

3. Build graph:
```python
# agents/my_agent/graph.py
from langgraph.graph import StateGraph

def create_graph():
    graph = StateGraph(MyAgentState)
    graph.add_node("node_1", node_1)
    graph.add_edge("start", "node_1")
    return graph.compile()
```

## Testing

```bash
# Unit tests
pytest tests/unit/agents/ -v

# Integration tests
pytest tests/integration/ -v -k agent
```

## See Also

- [Architecture - Agent Design](../../docs/AGENT_DESIGN.md)
- [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)
- [Evaluation Framework](../evaluation/README.md)
