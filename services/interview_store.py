"""Compatibility facade for legacy callers of interview persistence."""

from __future__ import annotations

import json
from typing import Any

from services.interview_service import InterviewService


class InterviewStore:
    """Delegate legacy method names to the application service."""

    def __init__(self, service: InterviewService | None = None) -> None:
        self.service = service or InterviewService()

    def create(self, candidate_id: str, position: str, mode: str, state: dict[str, Any]) -> str:
        return self.service.start_interview(candidate_id, position, mode)["interview_id"]

    def get(self, interview_id: str) -> dict[str, Any] | None:
        return self.service.get_interview(interview_id)

    def save(self, interview_id: str, state: dict[str, Any]) -> dict[str, Any]:
        current = self.service.get_interview(interview_id)
        if current is None:
            raise KeyError(f"Interview not found: {interview_id}")
        return self.service.submit_answer(
            interview_id,
            state.get("history", [{}])[-1].get("answer", ""),
            f"legacy-{state.get('updated_at', '')}",
        )

    def delete(self, interview_id: str) -> None:
        raise NotImplementedError("Interview deletion is not part of the application service yet")

    def list(self) -> list[dict[str, Any]]:
        return self.service.list_interviews()
