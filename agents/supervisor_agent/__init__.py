"""LangGraph-based top-level orchestrator for recruitment agents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

try:
    from langgraph.graph import END, START, StateGraph
except ImportError:  # pragma: no cover - compatibility with older LangGraph releases
    from langgraph.graph import END, StateGraph
    try:
        from langgraph.constants import START
    except ImportError:  # pragma: no cover - fallback for older runtime variants
        START = "__start__"

from agents.authorization import require_agent
from services.job_store import JobStore


class SupervisorState(TypedDict, total=False):
    task_type: str
    payload: dict[str, Any]
    target_agent: str
    next_node: str
    result: Any
    error: str


class SupervisorAgent:
    """Routes workflow tasks through explicit LangGraph nodes and branches."""

    agent_id = "supervisor-agent"

    def __init__(
        self,
        agent_handlers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
    ) -> None:
        self._agent_handlers = dict(agent_handlers or {})
        self.graph = self._build_graph()

    def route_task(self, task_type: str, payload: dict[str, Any]) -> Any:
        require_agent(self.agent_id, "supervisor-agent")
        state = self.graph.invoke({"task_type": task_type, "payload": payload})
        if state.get("error"):
            raise ValueError(state["error"])
        return state.get("result")

    def approve_job(self, job_id: str) -> dict | None:
        return self.route_task("approve_job", {"job_id": job_id})

    def _build_graph(self):
        workflow = StateGraph(SupervisorState)
        workflow.add_node("route_task", self._route_task)
        workflow.add_node("approve_job", self._approve_job_task)
        workflow.add_node("browser_agent", self._browser_agent_task)
        workflow.add_node("interview_agent", self._interview_agent_task)
        workflow.add_node("reject_task", self._reject_task)
        workflow.add_edge(START, "route_task")
        workflow.add_conditional_edges(
            "route_task",
            lambda state: state["next_node"],
            {
                "approve_job": "approve_job",
                "browser_agent": "browser_agent",
                "interview_agent": "interview_agent",
                "reject_task": "reject_task",
            },
        )
        workflow.add_edge("approve_job", END)
        workflow.add_edge("browser_agent", END)
        workflow.add_edge("interview_agent", END)
        workflow.add_edge("reject_task", END)
        return workflow.compile()

    @staticmethod
    def _route_task(state: SupervisorState) -> dict[str, str]:
        target_by_task = {
            "approve_job": "approve_job",
            "publish_job": "browser_agent",
            "screen_candidates": "interview_agent",
        }
        task_type = state.get("task_type", "")
        next_node = target_by_task.get(task_type, "reject_task")
        return {"next_node": next_node, "target_agent": next_node}

    def _approve_job_task(self, state: SupervisorState) -> dict[str, Any]:
        payload = state["payload"]
        return {"result": JobStore.approve(payload["job_id"], self.agent_id)}

    def _browser_agent_task(self, state: SupervisorState) -> dict[str, Any]:
        handler = self._agent_handlers.get("browser-agent")
        if handler is None:
            return {"error": "Browser Agent handler is not configured"}
        return {"result": handler(state["payload"])}

    def _interview_agent_task(self, state: SupervisorState) -> dict[str, Any]:
        handler = self._agent_handlers.get("interview-agent")
        if handler is None:
            return {"error": "Interview Agent handler is not configured"}
        return {"result": handler(state["payload"])}

    @staticmethod
    def _reject_task(state: SupervisorState) -> dict[str, str]:
        return {"error": f"Unsupported supervisor task '{state.get('task_type', '')}'"}