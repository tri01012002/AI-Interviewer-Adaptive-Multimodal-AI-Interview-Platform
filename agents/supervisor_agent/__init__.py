"""Top-level recruitment orchestrator for the Interview and Browser agents."""

from __future__ import annotations

from services.job_store import JobStore


class SupervisorAgent:
    """Coordinates workflow transitions between recruitment subagents."""

    agent_id = "supervisor-agent"

    def approve_job(self, job_id: str) -> dict | None:
        return JobStore.approve(job_id, self.agent_id)