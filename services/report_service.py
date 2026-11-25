"""Persisted interview outcome reports.

Report exports are written to
``<STORAGE_PATH>/reports/<organization_id>/<interview_id>/report_<uuid>.<ext>``
so candidate erasure can find and delete them deterministically without a
database record. Exports written before this layout
(``<STORAGE_PATH>/report_<uuid>.<ext>``) are found by reading a size-bounded
header; see :func:`delete_candidate_report_exports` and
docs/CANDIDATE_DATA_DELETION.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select

from config import settings
from services.database import (
    InterviewAssessmentRecord,
    InterviewEvidenceRecord,
    InterviewRecord,
    InterviewTurnRecord,
    SessionLocal,
)

logger = logging.getLogger(__name__)

REPORT_EXPORT_SUBDIR = "reports"
_LEGACY_REPORT_NAME = re.compile(
    r"^(?:report_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.(?:json|txt)|\.report-[A-Za-z0-9_]{1,64}\.tmp)$"
)
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
# Legacy exports put the identifiers first; never read more than this.
_LEGACY_HEADER_BYTES = 64 * 1024
# ``json.dumps(report, indent=2)`` writes top-level keys with exactly two
# spaces of indentation and escapes quotes inside string values, so evidence
# text cannot forge these lines.
_JSON_INTERVIEW_ID = re.compile(r'^  "interview_id": "([^"\\]*)"', re.MULTILINE)
_JSON_CANDIDATE_ID = re.compile(r'^  "candidate_id": "([^"\\]*)"', re.MULTILINE)
_TEXT_INTERVIEW_ID = re.compile(r"\AInterview: (\S+)")


class ReportExportUnavailable(HTTPException):
    """The interview's candidate data was erased; no report may be exported."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_410_GONE,
            detail={
                "code": "candidate_personal_data_erased",
                "message": (
                    "The candidate's personal data was erased; "
                    "reports are no longer available."
                ),
            },
        )


def _refuse_erased(persisted_state: Any) -> None:
    if isinstance(persisted_state, dict) and persisted_state.get("personal_data_erased"):
        raise ReportExportUnavailable()


def report_export_root() -> Path:
    """Root directory that holds report exports (``settings.STORAGE_PATH``)."""
    return Path(settings.STORAGE_PATH)


def _path_segment(identifier: str) -> str:
    if _SAFE_PATH_SEGMENT.fullmatch(identifier):
        return identifier
    # Deterministic and filesystem-safe for identifiers with unusual characters.
    return "h-" + hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:40]


def report_export_directory(root: Path, organization_id: str, interview_id: str) -> Path:
    return (
        root
        / REPORT_EXPORT_SUBDIR
        / _path_segment(organization_id)
        / _path_segment(interview_id)
    )


class ReportService:
    """Build recruiter reports from durable interview and assessment records."""

    def __init__(self, output_dir: str | None = None, session_factory: Any | None = None) -> None:
        self._configured_output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._session_factory = session_factory or SessionLocal

    @property
    def output_dir(self) -> Path:
        # Resolved on use so the API's module-level instance follows the
        # configured STORAGE_PATH that candidate erasure also scans.
        if self._configured_output_dir:
            return Path(self._configured_output_dir)
        return report_export_root()

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
            _refuse_erased(persisted_state)
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
        organization_id = self._export_organization(report)
        output_dir = self.output_dir
        if organization_id is not None:
            output_dir = report_export_directory(
                output_dir, organization_id, str(report["interview_id"])
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"report_{uuid4()}.{format}"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_dir,
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
            if organization_id is not None:
                # Re-check just before publishing: an erasure that committed
                # while this export was being written must win.
                self._export_organization(report)
            os.replace(temporary_path, output_path)
        except (OSError, ReportExportUnavailable):
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        return str(output_path)

    def _export_organization(self, report: dict[str, Any]) -> str | None:
        """Owning organization of the persisted interview the report describes.

        Returns ``None`` for a report that is not tied to a persisted interview
        (it is then written to the flat legacy location). Raises
        :class:`ReportExportUnavailable` when the candidate's data was erased.
        """
        interview_id = report.get("interview_id")
        if not isinstance(interview_id, str) or not interview_id:
            return None
        with self._session_factory() as session:
            interview = session.get(InterviewRecord, interview_id)
            if interview is None:
                return None
            if report.get("candidate_id") not in (None, interview.candidate_id):
                raise LookupError("Persisted interview not found")
            try:
                persisted_state = json.loads(interview.state_json)
            except (TypeError, ValueError):
                persisted_state = {}
            _refuse_erased(persisted_state)
            return interview.organization_id


def delete_candidate_report_exports(
    organization_id: str,
    candidate_id: str,
    interview_ids: Iterable[str],
    root: Path | None = None,
) -> tuple[int, int]:
    """Delete every report export of the given interviews.

    ``interview_ids`` must already be scoped to ``organization_id`` and
    ``candidate_id`` by the caller (read from the database). Returns
    ``(exports still present, exports found before deletion)``. Missing files
    count as already deleted. Symbolic links are never followed: a link is
    removed itself, never its target. Logs carry counts and exception types only.
    """
    root = root if root is not None else report_export_root()
    wanted = {str(item) for item in interview_ids if item}
    if not wanted:
        return 0, 0
    pending = 0
    found = 0
    for interview_id in sorted(wanted):
        directory = report_export_directory(root, organization_id, interview_id)
        still, seen = _delete_scoped_exports(root, directory)
        pending += still
        found += seen
    still, seen = _delete_legacy_exports(root, wanted, candidate_id)
    pending += still
    found += seen
    if pending:
        logger.warning(
            "Report export deletion incomplete",
            extra={"pending_export_count": pending},
        )
    return pending, found


def _remove_export_file(path: Path) -> None:
    # ``os.unlink`` removes a symbolic link itself and never its target.
    os.unlink(path)


def _remove_and_verify(path: Path) -> bool:
    """Remove one export; return True when it is still present afterwards."""
    try:
        _remove_export_file(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(
            "Report export file deletion failed; erasure remains pending",
            extra={"failure_type": type(exc).__name__},
        )
    return os.path.lexists(path)


def _delete_scoped_exports(root: Path, directory: Path) -> tuple[int, int]:
    if not os.path.lexists(directory):
        return 0, 0
    chain = (root / REPORT_EXPORT_SUBDIR, directory.parent, directory)
    if any(path.is_symlink() for path in chain) or not directory.is_dir():
        # Never traverse a link out of export storage; report it as pending.
        logger.warning(
            "Report export directory is not a plain directory; erasure remains pending",
            extra={"failure_type": "unexpected_export_directory"},
        )
        return 1, 1
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        logger.warning(
            "Report export directory could not be listed; erasure remains pending",
            extra={"failure_type": type(exc).__name__},
        )
        return 1, 1
    pending = 0
    found = 0
    for entry in entries:
        found += 1
        if entry.is_dir(follow_symlinks=False):
            pending += 1
            continue
        if _remove_and_verify(Path(entry.path)):
            pending += 1
    if pending == 0:
        try:
            directory.rmdir()
        except OSError:
            pass
    return pending, found


def _delete_legacy_exports(
    root: Path, interview_ids: set[str], candidate_id: str
) -> tuple[int, int]:
    if root.is_symlink() or not root.is_dir():
        return 0, 0
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        logger.warning(
            "Report export storage could not be listed; erasure remains pending",
            extra={"failure_type": type(exc).__name__},
        )
        return 1, 1
    pending = 0
    found = 0
    for entry in entries:
        if not _LEGACY_REPORT_NAME.fullmatch(entry.name):
            continue
        # Only plain files directly inside STORAGE_PATH; links are never followed.
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            continue
        header = _read_header(Path(entry.path))
        if header is None:
            # Unreadable: ownership unknown, so erasure cannot claim completion.
            found += 1
            pending += 1
            continue
        if not _legacy_header_matches(header, interview_ids, candidate_id):
            continue
        found += 1
        if _remove_and_verify(Path(entry.path)):
            pending += 1
    return pending, found


def _read_header(path: Path) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.warning(
            "Legacy report export could not be inspected",
            extra={"failure_type": type(exc).__name__},
        )
        return None
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read(_LEGACY_HEADER_BYTES).decode("utf-8", errors="replace")


def _legacy_header_matches(
    header: str, interview_ids: set[str], candidate_id: str
) -> bool:
    interview = _JSON_INTERVIEW_ID.search(header)
    if interview is not None:
        candidate = _JSON_CANDIDATE_ID.search(header)
        if candidate is not None and candidate.group(1) != candidate_id:
            return False
        return interview.group(1) in interview_ids
    text_interview = _TEXT_INTERVIEW_ID.match(header)
    return text_interview is not None and text_interview.group(1) in interview_ids


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
