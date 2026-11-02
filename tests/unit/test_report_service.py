import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.database import (
    Base,
    InterviewAssessmentRecord,
    InterviewEvidenceRecord,
    InterviewRecord,
    InterviewTurnRecord,
)
from services.report_service import ReportService


def _report_service(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'report-test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory, ReportService(str(tmp_path / "reports"), factory)


def test_report_uses_persisted_assessments_and_evidence(tmp_path):
    engine, factory, reports = _report_service(tmp_path)
    now = datetime.now(UTC)
    interview_id = "interview-report"
    with factory.begin() as session:
        session.add(
            InterviewRecord(
                id=interview_id,
                candidate_id="synthetic-candidate",
                organization_id="synthetic-organization",
                position="Backend Engineer",
                mode="text",
                current_question="Follow-up question",
                state_json=json.dumps(
                    {
                        "status": "completed",
                        "evaluation": {
                            "evaluator_type": "llm",
                            "next_action": "FINISH",
                            "feedback": "Persisted recommendation based on this answer.",
                            "missing_evidence": ["Failure recovery details"],
                        },
                    }
                ),
            )
        )
        turn = InterviewTurnRecord(
            interview_id=interview_id,
            turn_id="synthetic-turn",
            sequence_number=1,
            status="completed",
            candidate_answer="Synthetic answer quote.",
            started_at=now,
            completed_at=now,
        )
        session.add(turn)
        session.flush()
        session.add(
            InterviewAssessmentRecord(
                interview_id=interview_id,
                turn_id=turn.id,
                competency="distributed_systems",
                score=4,
                status="demonstrated",
                evidence_strength="strong",
                confidence=0.82,
                strengths_json='["Explained recovery"]',
                gaps_json='["Discuss consistency trade-offs"]',
                rationale="Persisted assessment rationale.",
                evaluator_type="llm",
                evaluator_version="test-v1",
            )
        )
        session.add(
            InterviewEvidenceRecord(
                interview_id=interview_id,
                turn_id=turn.id,
                competency="distributed_systems",
                evidence_text="Synthetic answer quote.",
                evidence_type="project_experience",
                strength="strong",
                specificity="high",
                ownership="explicit",
                relevance=0.9,
                evaluator_type="llm",
                evaluator_version="test-v1",
            )
        )

    report = reports.build_report(
        "synthetic-candidate",
        interview_id,
        {"overall_score": 0, "skills": {"invented": {"score": 5}}},
        organization_id="synthetic-organization",
    )

    assert report["is_final"] is True
    assert report["evaluation_status"] == "complete"
    assert report["overall_score"] == 80
    assert report["competencies"]["distributed_systems"]["score"] == 4
    assert report["supporting_evidence"][0]["quote"] == "Synthetic answer quote."
    assert report["strengths"] == ["Explained recovery"]
    assert report["gaps"] == ["Discuss consistency trade-offs"]
    assert report["missing_evidence"] == ["Failure recovery details"]
    assert report["ai_recommendation"]["evaluator"] == "llm"
    assert report["confidence"] == 0.82
    assert "does not replace" in report["decision_disclaimer"]
    engine.dispose()


def test_report_marks_failed_or_incomplete_interview_without_fake_evaluation(tmp_path):
    engine, factory, reports = _report_service(tmp_path)
    with factory.begin() as session:
        session.add(
            InterviewRecord(
                id="interview-partial",
                candidate_id="synthetic-candidate",
                organization_id="synthetic-organization",
                position="Backend Engineer",
                mode="text",
                current_question="Initial question",
                state_json=json.dumps(
                    {"status": "active", "evaluation": {}}
                ),
            )
        )
        session.add(
            InterviewTurnRecord(
                interview_id="interview-partial",
                turn_id="failed-turn",
                sequence_number=1,
                status="failed_retryable",
                candidate_answer="Synthetic answer.",
            )
        )

    report = reports.build_report(
        "synthetic-candidate",
        "interview-partial",
        organization_id="synthetic-organization",
    )

    assert report["is_final"] is False
    assert report["completion_status"] == "active"
    assert report["evaluation_status"] == "unavailable"
    assert report["overall_score"] is None
    assert report["failed_turn_count"] == 1
    assert report["ai_recommendation"]["text"] is None
    engine.dispose()


def test_report_write_failure_does_not_leave_partial_export(tmp_path, monkeypatch):
    engine, _factory, reports = _report_service(tmp_path)

    def fail_replace(*_args, **_kwargs):
        raise OSError("synthetic atomic promotion failure")

    monkeypatch.setattr("services.report_service.os.replace", fail_replace)
    with pytest.raises(OSError, match="synthetic atomic promotion failure"):
        reports.write_report({"synthetic": "report"})

    assert list(reports.output_dir.iterdir()) == []
    engine.dispose()
