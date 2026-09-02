"""Lightweight RAG question retrieval layer for adaptive interviewing."""

from __future__ import annotations

from typing import Any


class RAGQuestionService:
    """Minimal retrieval-and-generation shim for question selection."""

    def __init__(self) -> None:
        self._kb = {
            "ai engineer": [
                "Tell me about a model you deployed to production and how you evaluated it.",
                "How did you optimize model inference latency for real users?",
                "Describe a trade-off you made between accuracy and performance.",
            ],
            "data scientist": [
                "What experiment design did you use for model validation and monitoring?",
                "How did you communicate model quality to stakeholders?",
            ],
            "software engineer": [
                "Walk me through a production incident you debugged end-to-end.",
                "How did you design a system for reliability and scale?",
            ],
        }

    def retrieve_relevant_questions(self, position: str, skills: dict[str, Any]) -> list[str]:
        normalized = (position or "").lower()
        base = self._kb.get(normalized, self._kb["ai engineer"])

        if not skills:
            return base[:2]

        if "computer_vision" in skills:
            base.insert(0, "What computer vision model trade-offs did you make for deployment constraints?")
        if "pytorch" in skills:
            base.insert(0, "How did you structure your PyTorch training and evaluation pipeline?")
        return base[:4]

    def generate_follow_up_question(self, position: str, answer: str, skills: dict[str, Any]) -> str:
        questions = self.retrieve_relevant_questions(position, skills)
        if not answer:
            return questions[0]
        lowered = answer.lower()
        if "production" in lowered or "deploy" in lowered:
            return "What metrics did you use to validate the success of the production deployment?"
        if "latency" in lowered or "performance" in lowered:
            return "Which bottleneck did you optimize first, and how did you measure the result?"
        return questions[0]
