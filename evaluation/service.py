"""Lightweight answer evaluation logic."""

from __future__ import annotations

from typing import Any


class EvaluationService:
    """Computes skill-level and overall interview scores from a candidate answer."""

    def evaluate_answer(self, answer: str, skills: dict[str, Any], position: str) -> dict[str, Any]:
        text = answer.lower()
        evidence: list[str] = []

        if "python" in text:
            evidence.append("Python experience")
        if "pytorch" in text:
            evidence.append("PyTorch experience")
        if "computer vision" in text or "yolo" in text or "opencv" in text:
            evidence.append("Computer vision experience")
        if "production" in text or "deploy" in text:
            evidence.append("Production deployment experience")
        if "latency" in text or "inference" in text or "performance" in text:
            evidence.append("Optimization and performance awareness")

        total_score = 0.0
        skill_scores: dict[str, float] = {}
        for skill_name, skill_state in skills.items():
            score = float(skill_state.get("score", 0))
            skill_scores[skill_name] = round(min(score, 5.0), 2)
            total_score += score

        if skill_scores:
            overall_score = round((total_score / max(len(skill_scores), 1)) * 20.0, 2)
        else:
            overall_score = round(max(0.0, len(evidence) * 12.0), 2)

        return {
            "position": position,
            "skills_detected": skill_scores,
            "overall_score": overall_score,
            "strengths": evidence[:3] or ["Candidate provided substantive project context."],
            "weaknesses": ["Continue probing for measurable impact and trade-offs."] if not evidence else [],
            "confidence": min(0.95, max(0.5, 0.5 + (len(evidence) * 0.08))),
            "evidence": evidence,
            "feedback": (
                "Strong signal across the core technical stack; continue with deployment, scale, and trade-off questions."
                if evidence
                else "Candidate answer is brief; ask for concrete examples, metrics, and project ownership."
            ),
        }
