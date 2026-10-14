"""Supervisor control-plane operations for recruitment workflows."""

from __future__ import annotations

from agents.supervisor_agent import SupervisorAgent


class SupervisorService:
    """Application boundary for the top-level Supervisor Agent."""

    @staticmethod
    def approve_job(job_id: str) -> dict | None:
        return SupervisorAgent().approve_job(job_id)