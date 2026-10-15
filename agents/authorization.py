"""Authorization contracts for internal agent-to-agent operations."""

from __future__ import annotations

from typing import Literal

AgentId = Literal["supervisor-agent", "interview-agent", "browser-agent"]


class AgentAuthorizationError(PermissionError):
    """Raised when an internal operation is called by the wrong agent."""


def require_agent(agent_id: str, expected: AgentId) -> None:
    if agent_id != expected:
        raise AgentAuthorizationError(
            f"Agent '{agent_id}' is not authorized for '{expected}' operations"
        )