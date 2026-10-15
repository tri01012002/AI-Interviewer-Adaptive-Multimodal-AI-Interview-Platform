"""Browser Agent boundary for approved recruitment work."""

from __future__ import annotations

from agents.authorization import require_agent
from services.job_store import JobStore


class BrowserAgent:
	agent_id = "browser-agent"

	def get_approved_job(self, job_id: str) -> dict | None:
		require_agent(self.agent_id, "browser-agent")
		return JobStore.get_approved_for_browser(job_id, self.agent_id)
