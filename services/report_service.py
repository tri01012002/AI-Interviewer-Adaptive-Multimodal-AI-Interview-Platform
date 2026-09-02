"""Report generation and export for candidate interview outcomes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings


class ReportService:
    """Generate a simple JSON or text report from interview evaluation results."""

    def __init__(self, output_dir: str | None = None) -> None:
        self.output_dir = Path(output_dir or settings.STORAGE_PATH)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build_report(self, candidate_id: str, interview_id: str, state: dict[str, Any]) -> dict[str, Any]:
        evaluation = state.get("evaluation", {})
        overall_score = float(state.get("overall_score", evaluation.get("overall_score", 0.0)))
        skills = state.get("skills", {})

        report = {
            "candidate_id": candidate_id,
            "interview_id": interview_id,
            "position": state.get("position", ""),
            "overall_score": round(overall_score, 2),
            "skills": {name: {"score": values.get("score", 0), "evidence": values.get("evidence", [])} for name, values in skills.items()},
            "strengths": evaluation.get("strengths", []),
            "weaknesses": evaluation.get("weaknesses", []),
            "feedback": evaluation.get("feedback", ""),
            "confidence": evaluation.get("confidence", 0.0),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return report

    def export_report(self, candidate_id: str, interview_id: str, state: dict[str, Any], format: str = "json") -> str:
        report = self.build_report(candidate_id, interview_id, state)
        file_name = f"report_{candidate_id}_{interview_id}.{format}"
        output_path = self.output_dir / file_name
        if format == "json":
            output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        else:
            text = "\n".join(
                [
                    f"Candidate: {report['candidate_id']}",
                    f"Interview: {report['interview_id']}",
                    f"Position: {report['position']}",
                    f"Overall score: {report['overall_score']}",
                    f"Confidence: {report['confidence']}",
                    "\nStrengths:",
                    *[f"- {item}" for item in report["strengths"]],
                    "\nWeaknesses:",
                    *[f"- {item}" for item in report["weaknesses"]],
                    "\nFeedback:",
                    report["feedback"],
                ]
            )
            output_path.write_text(text, encoding="utf-8")
        return str(output_path)
