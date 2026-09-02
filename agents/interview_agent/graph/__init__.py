"""Interview agent orchestration primitives."""

from __future__ import annotations

from typing import Any


class InterviewAgentCore:
    """Minimal MVP interview engine for adaptive candidate evaluation."""

    def __init__(self) -> None:
        self._intro_question = (
            "Please introduce yourself and summarize the projects or problems "
            "you have worked on that are most relevant to this role."
        )
        self._skill_keywords = {
            "python": ["python", "pandas", "numpy", "scikit", "fastapi", "flask"],
            "pytorch": ["pytorch", "torch", "cnn", "transformer", "training loop", "model training"],
            "computer_vision": [
                "computer vision",
                "cv",
                "yolo",
                "opencv",
                "object detection",
                "image classification",
                "segmentation",
                "vision",
            ],
        }

    def start_interview(self, candidate_id: str, position: str) -> dict[str, Any]:
        state: dict[str, Any] = {
            "candidate_id": candidate_id,
            "position": position,
            "current_question": self._intro_question,
            "questions_asked": [self._intro_question],
            "skills": {},
            "history": [],
            "status": "active",
            "overall_score": 0.0,
            "evaluation": {},
            "follow_up_questions": [],
            "mode": "text",
        }
        return state

    def handle_answer(self, state: dict[str, Any], answer: str) -> dict[str, Any]:
        normalized_answer = answer.lower()
        state.setdefault("skills", {})
        state.setdefault("history", [])

        for skill_name, keywords in self._skill_keywords.items():
            matched = [keyword for keyword in keywords if keyword in normalized_answer]
            if not matched:
                continue

            score = max(3, len(matched))
            current = state["skills"].get(skill_name, {"score": 0, "evidence": []})
            current["score"] = max(current.get("score", 0), score)
            current["evidence"] = list(dict.fromkeys(current.get("evidence", []) + matched))
            state["skills"][skill_name] = current

        state["history"].append({"answer": answer})

        if "production" in normalized_answer or "deploy" in normalized_answer or "deployed" in normalized_answer:
            next_question = (
                "Describe a model or system you deployed to production and the trade-offs "
                "you considered around reliability, monitoring, and scale."
            )
        elif "latency" in normalized_answer or "inference" in normalized_answer or "performance" in normalized_answer:
            next_question = (
                "How did you measure and optimize inference latency or throughput in a production ML pipeline?"
            )
        else:
            next_question = (
                "Tell me about a project where you deployed an ML model to production and "
                "optimized latency or throughput for end users."
            )

        state["current_question"] = next_question
        state["questions_asked"].append(next_question)
        return state
