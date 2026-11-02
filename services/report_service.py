"""Persisted interview outcome reports."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from config import settings
from services.database import (
    InterviewAssessmentRecord,
    InterviewEvidenceRecord,
    InterviewRecord,
    InterviewTurnRecord,
    SessionLocal,
)


class ReportService:
    """Build recruiter reports from durable interview and assessment records."""

    def __init__(self, output_dir: str | None = None, session_factory: Any | None = None) -> None:
        self.output_dir = Path(output_dir or settings.STORAGE_PATH)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._session_factory = session_factory or SessionLocal

    def build_report(
        self,
        candidate_id: str,
        interview_id: str,
        state: dict[str, Any] | None = None,
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        del state
        if not organization_id:
            raise LookupError("Persisted interview not found")
        with self._session_factory() as session:
            interview = session.get(InterviewRecord, interview_id)
            if (
                interview is None
                or interview.candidate_id != candidate_id
                or interview.organization_id != organization_id
            ):
                raise LookupError("Persisted interview not found")

            persisted_state = json.loads(interview.state_json)
            turns = list(
                session.execute(
                    select(InterviewTurnRecord)
                    .where(InterviewTurnRecord.interview_id == interview_id)
                    .order_by(InterviewTurnRecord.sequence_number)
                ).scalars()
            )
            assessments = list(
                session.execute(
                    select(InterviewAssessmentRecord)
                    .where(InterviewAssessmentRecord.interview_id == interview_id)
                    .order_by(InterviewAssessmentRecord.created_at)
                ).scalars()
            )
            evidence = list(
                session.execute(
                    select(InterviewEvidenceRecord)
                    .where(InterviewEvidenceRecord.interview_id == interview_id)
                    .order_by(InterviewEvidenceRecord.created_at)
                ).scalars()
            )

        competency_reports: dict[str, dict[str, Any]] = {}
        for assessment in assessments:
            competency = competency_reports.setdefault(
                assessment.competency,
                {
                    "scores": [],
                    "confidence_values": [],
                    "statuses": [],
                    "strengths": [],
                    "gaps": [],
                    "evidence": [],
                },
            )
            if assessment.status != "not_assessed":
                competency["scores"].append(assessment.score)
            competency["confidence_values"].append(assessment.confidence)
            competency["statuses"].append(assessment.status)
            competency["strengths"].extend(json.loads(assessment.strengths_json))
            competency["gaps"].extend(json.loads(assessment.gaps_json))

        for item in evidence:
            competency = competency_reports.setdefault(
                item.competency,
                {
                    "scores": [],
                    "confidence_values": [],
                    "statuses": [],
                    "strengths": [],
                    "gaps": [],
                    "evidence": [],
                },
            )
            competency["evidence"].append(
                {
                    "quote": item.evidence_text,
                    "type": item.evidence_type,
                    "strength": item.strength,
                    "specificity": item.specificity,
                    "ownership": item.ownership,
                    "relevance": item.relevance,
                    "evaluator": item.evaluator_type,
                }
            )

        scores = [score for item in competency_reports.values() for score in item["scores"]]
        confidences = [
            value
            for item in competency_reports.values()
            for value in item["confidence_values"]
        ]
        strengths = _unique(
            value for item in competency_reports.values() for value in item["strengths"]
        )
        gaps = _unique(value for item in competency_reports.values() for value in item["gaps"])
        latest_evaluation = persisted_state.get("evaluation", {})
        missing_evidence = _unique(latest_evaluation.get("missing_evidence", []))
        for values in competency_reports.values():
            values["score"] = (
                round(sum(values["scores"]) / len(values["scores"]), 2)
                if values["scores"]
                else None
            )
            values["confidence"] = (
                round(sum(values["confidence_values"]) / len(values["confidence_values"]), 3)
                if values["confidence_values"]
                else None
            )
            for field in ("scores", "confidence_values", "statuses"):
                values.pop(field)

        failed_turns = sum(turn.status == "failed_retryable" for turn in turns)
        pending_turns = sum(turn.status != "completed" for turn in turns)
        is_final = persisted_state.get("status") == "completed" and pending_turns == 0
        evaluation_status = (
            "unavailable"
            if failed_turns and not assessments
            else "partial"
            if pending_turns or len(assessments) < len(turns)
            else "complete"
            if assessments
            else "not_started"
        )
        summary = (
            f"{len(assessments)} persisted assessment(s) across "
            f"{len(competency_reports)} competency/competencies."
            if assessments
            else "No completed assessment is available."
        )
        assessment_payload = latest_evaluation.get("assessment", {})
        recommendation_text = latest_evaluation.get("feedback") or assessment_payload.get(
            "rationale"
        )
        recommendation = {
            "text": recommendation_text,
            "next_action": latest_evaluation.get("next_action")
            or assessment_payload.get("next_action"),
            "evaluator": latest_evaluation.get("evaluator_type"),
        }
        areas_for_assessment = _unique([*gaps, *missing_evidence])
        for name, values in competency_reports.items():
            if values["confidence"] is not None and values["confidence"] < 0.6:
                areas_for_assessment.append(
                    f"Gather additional evidence for {name}; assessment confidence is limited."
                )

        return {
            "candidate_id": interview.candidate_id,
            "interview_id": interview.id,
            "position": interview.position,
            "campaign": (
                None
                if interview.campaign_id is None
                else {
                    "campaign_id": interview.campaign_id,
                    "application_id": interview.application_id,
                    "schedule_id": interview.schedule_id,
                    "objective_version": interview.objective_version,
                    "plan_version": interview.plan_version,
                    "rubric_snapshot": json.loads(
                        interview.rubric_snapshot_json or "[]"
                    ),
                }
            ),
            "completion_status": persisted_state.get("status", "unknown"),
            "is_final": is_final,
            "evaluation_status": evaluation_status,
            "summary": summary,
            "overall_score": round(sum(scores) / len(scores) * 20.0, 2) if scores else None,
            "competencies": competency_reports,
            "supporting_evidence": [
                {
                    "competency": item.competency,
                    "quote": item.evidence_text,
                    "type": item.evidence_type,
                    "strength": item.strength,
                    "specificity": item.specificity,
                    "ownership": item.ownership,
                    "relevance": item.relevance,
                }
                for item in evidence
            ],
            "strengths": strengths,
            "gaps": gaps,
            "missing_evidence": missing_evidence,
            "confidence": round(sum(confidences) / len(confidences), 3)
            if confidences
            else None,
            "areas_requiring_further_assessment": _unique(areas_for_assessment),
            "ai_recommendation": recommendation,
            "failed_turn_count": failed_turns,
            "pending_turn_count": pending_turns,
            "decision_disclaimer": (
                "This AI assessment supports, but does not replace, the recruiter's "
                "independent hiring decision."
            ),
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def export_report(
        self,
        candidate_id: str,
        interview_id: str,
        state: dict[str, Any],
        organization_id: str,
        format: str = "json",
    ) -> str:
        if format not in {"json", "txt"}:
            raise ValueError("Report format must be 'json' or 'txt'")
        report = self.build_report(
            candidate_id,
            interview_id,
            state,
            organization_id=organization_id,
        )
        return self.write_report(report, format)

    def write_report(self, report: dict[str, Any], format: str = "json") -> str:
        if format not in {"json", "txt"}:
            raise ValueError("Report format must be 'json' or 'txt'")
        output_path = self.output_dir / f"report_{uuid4()}.{format}"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.output_dir,
                prefix=".report-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                content = (
                    json.dumps(report, indent=2)
                    if format == "json"
                    else _format_text_report(report)
                )
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, output_path)
        except OSError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        return str(output_path)


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


def _format_text_report(report: dict[str, Any]) -> str:
    lines = [
        f"Interview: {report['interview_id']}",
        f"Position: {report['position']}",
        f"Status: {report['completion_status']} ({report['evaluation_status']})",
        f"Overall score: {report['overall_score']}",
        f"Confidence: {report['confidence']}",
        "",
        "Summary:",
        report["summary"],
        "",
        "Strengths:",
        *[f"- {item}" for item in report["strengths"]],
        "",
        "Gaps:",
        *[f"- {item}" for item in report["gaps"]],
        "",
        "Areas requiring further assessment:",
        *[f"- {item}" for item in report["areas_requiring_further_assessment"]],
        "",
        "AI recommendation:",
        report["ai_recommendation"]["text"] or "No persisted AI recommendation is available.",
        "",
        report["decision_disclaimer"],
    ]
    return "\n".join(lines)
