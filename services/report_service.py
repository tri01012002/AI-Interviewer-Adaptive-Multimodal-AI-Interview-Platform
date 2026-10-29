"""Persisted interview outcome reports."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from config import settings
from services.database import (
    InterviewAssessmentRecord,
    InterviewEvidenceRecord,
    InterviewQuestionRecord,
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
            questions = list(
                session.execute(
                    select(InterviewQuestionRecord)
                    .where(InterviewQuestionRecord.interview_id == interview_id)
                    .order_by(InterviewQuestionRecord.sequence_number)
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

        turn_by_id = {turn.id: turn for turn in turns}
        question_by_id = {question.id: question for question in questions}
        history = persisted_state.get("history") or []
        history_by_sequence = {
            index + 1: item
            for index, item in enumerate(history)
            if isinstance(item, dict)
        }
        evidence_by_turn: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            turn = turn_by_id.get(item.turn_id)
            answer = turn.candidate_answer if turn is not None else None
            if not answer or not _contains_quote(answer, item.evidence_text):
                continue
            evidence_by_turn.setdefault(item.turn_id, []).append(
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

        competency_reports: dict[str, dict[str, Any]] = {}
        for assessment in assessments:
            turn = turn_by_id.get(assessment.turn_id)
            turn_history = history_by_sequence.get(turn.sequence_number, {}) if turn else {}
            question = question_by_id.get(turn.question_id) if turn and turn.question_id else None
            rubric = turn_history.get("rubric")
            if rubric is None:
                rubric = next(
                    (
                        item.get("rubric")
                        for item in (persisted_state.get("question_plan") or {}).get("questions", [])
                        if item.get("proposal", {}).get("question")
                        == (turn_history.get("question") or (question.question_text if question else None))
                    ),
                    None,
                )
            recorded_assessment = turn_history.get("assessment") or {}
            follow_up_index = turn_history.get("planned_question_index")
            follow_up_status = (
                persisted_state.get("follow_up_state", {}).get(str(follow_up_index))
                if follow_up_index is not None
                else None
            )
            competency = competency_reports.setdefault(
                assessment.competency,
                {
                    "scores": [],
                    "confidence_values": [],
                    "statuses": [],
                    "strengths": [],
                    "gaps": [],
                    "evidence": [],
                    "question_assessments": [],
                },
            )
            if assessment.status != "not_assessed":
                competency["scores"].append(assessment.score)
            competency["confidence_values"].append(assessment.confidence)
            competency["statuses"].append(assessment.status)
            competency["strengths"].extend(json.loads(assessment.strengths_json))
            competency["gaps"].extend(json.loads(assessment.gaps_json))
            competency["evidence"].extend(evidence_by_turn.get(assessment.turn_id, []))
            competency["question_assessments"].append(
                {
                    "turn_sequence": turn.sequence_number if turn else None,
                    "question": turn_history.get("question")
                    or (question.question_text if question else None),
                    "answer_quote": turn.candidate_answer if turn else None,
                    "is_follow_up": turn_history.get("is_follow_up"),
                    "rubric": rubric,
                    "rubric_status": "available" if rubric else "unavailable",
                    "score": assessment.score,
                    "score_scale": "0-5; based on the available evidence and rubric",
                    "assessment_status": assessment.status,
                    "confidence": assessment.confidence,
                    "technical_correctness": recorded_assessment.get("technical_correctness"),
                    "relevance": recorded_assessment.get("relevance"),
                    "depth": recorded_assessment.get("depth"),
                    "completeness": recorded_assessment.get("completeness"),
                    "practical_experience": recorded_assessment.get("practical_experience"),
                    "strengths": json.loads(assessment.strengths_json),
                    "gaps": json.loads(assessment.gaps_json),
                    "missing_evidence": recorded_assessment.get("missing_evidence", []),
                    "rationale": assessment.rationale,
                    "evidence_quotes": evidence_by_turn.get(assessment.turn_id, []),
                    "follow_up_status": follow_up_status or "not_recorded",
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
        latest_evaluation = persisted_state.get("evaluation") or {}
        missing_evidence_values = list(latest_evaluation.get("missing_evidence") or [])
        missing_evidence_values.extend(
            missing
            for item in history
            if isinstance(item, dict)
            for missing in (item.get("assessment") or {}).get("missing_evidence", [])
        )
        missing_evidence = _unique(missing_evidence_values)
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
                {"competency": competency, **item}
                for competency, values in competency_reports.items()
                for item in values["evidence"]
            ],
            "follow_up_results": _follow_up_results(persisted_state),
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
    return list(
        dict.fromkeys(
            str(value)
            for value in (values or [])
            if isinstance(value, str) and value
        )
    )


def _contains_quote(answer: str, quote: str) -> bool:
    def normalize(value: str) -> str:
        return " ".join(value.split()).casefold()

    return bool(quote.strip()) and normalize(quote) in normalize(answer)


def _follow_up_results(state: dict[str, Any]) -> list[dict[str, Any]]:
    plan = (state.get("question_plan") or {}).get("questions", [])
    statuses = state.get("follow_up_state") or {}
    history = [item for item in (state.get("history") or []) if isinstance(item, dict)]
    results = []
    for index, planned in enumerate(plan):
        follow_up = next(
            (
                item
                for item in history
                if item.get("is_follow_up")
                and item.get("planned_question_index") == index
            ),
            None,
        )
        results.append(
            {
                "planned_question_index": index,
                "question": planned.get("proposal", {}).get("question"),
                "status": statuses.get(str(index), "not_recorded"),
                "follow_up_question": (
                    follow_up.get("question") if follow_up is not None else None
                ),
                "follow_up_answer": (
                    follow_up.get("answer") if follow_up is not None else None
                ),
            }
        )
    return results


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
        "Competency assessments:",
    ]
    for competency, details in report["competencies"].items():
        lines.extend(
            [
                f"- {competency}: score {details['score']} / 5; confidence {details['confidence']}",
            ]
        )
        for item in details["question_assessments"]:
            lines.extend(
                [
                    f"  Question: {item['question'] or 'Unavailable'}",
                    f"  Score/status: {item['score']} / 5 ({item['assessment_status']})",
                    f"  Candidate answer: {item['answer_quote'] or 'Unavailable'}",
                    f"  Rubric: {json.dumps(item['rubric'], ensure_ascii=False) if item['rubric'] else 'Unavailable'}",
                    f"  Follow-up: {item['follow_up_status']}",
                ]
            )
            lines.extend(
                f"  Evidence quote: {evidence['quote']}"
                for evidence in item["evidence_quotes"]
            )
            lines.extend(
                f"  Missing evidence: {missing}"
                for missing in item["missing_evidence"]
            )
    lines.extend(
        [
            "",
            "Follow-up results:",
            *[
                f"- {item['question']}: {item['status']}"
                + (
                    f"; follow-up: {item['follow_up_question']}"
                    if item["follow_up_question"]
                    else ""
                )
                for item in report["follow_up_results"]
            ],
            "",
            "AI recommendation:",
            report["ai_recommendation"]["text"] or "No persisted AI recommendation is available.",
            "",
            report["decision_disclaimer"],
        ]
    )
    return "\n".join(lines)
