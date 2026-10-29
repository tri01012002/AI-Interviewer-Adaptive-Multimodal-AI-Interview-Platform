import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.database import (
    Base,
    InterviewAssessmentRecord,
    InterviewEvidenceRecord,
    InterviewQuestionRecord,
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
    rubric = {
        "competency": "distributed_systems",
        "weak": "Names a concept but gives no example.",
        "acceptable": "Explains a relevant action.",
        "strong": "Gives a specific action and measured result.",
        "exceptional": "Shows ownership and reproducible impact.",
        "requires_practical_evidence": True,
    }
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
                        "question_plan": {
                            "questions": [
                                {
                                    "proposal": {
                                        "question": "How did you handle recovery?",
                                        "competency": "distributed_systems",
                                    },
                                    "rubric": rubric,
                                }
                            ]
                        },
                        "follow_up_state": {"0": "completed"},
                        "history": [
                            {
                                "answer": "Synthetic answer quote.",
                                "question": "What measurable recovery result did you achieve?",
                                "is_follow_up": True,
                                "planned_question_index": 0,
                                "rubric": rubric,
                                "assessment": {
                                    "technical_correctness": 4,
                                    "relevance": 4,
                                    "depth": 3,
                                    "completeness": 4,
                                    "practical_experience": 4,
                                    "missing_evidence": ["Consistency trade-offs"],
                                },
                            }
                        ],
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
        question = InterviewQuestionRecord(
        interview_id=interview_id,
        sequence_number=1,
        competency="distributed_systems",
        difficulty="medium",
        question_text="What measurable recovery result did you achieve?",
        status="sent",
        generated_at=now,
        sent_at=now,
        )
        session.add(question)
        session.flush()
        turn = InterviewTurnRecord(
            interview_id=interview_id,
            turn_id="synthetic-turn",
            sequence_number=1,
            status="completed",
            candidate_answer="Synthetic answer quote.",
            question_id=question.id,
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
    assert report["missing_evidence"] == [
        "Failure recovery details",
        "Consistency trade-offs",
    ]
    assert report["ai_recommendation"]["evaluator"] == "llm"
    assert report["confidence"] == 0.82
    assessment = report["competencies"]["distributed_systems"]["question_assessments"][0]
    assert assessment["question"] == "What measurable recovery result did you achieve?"
    assert assessment["answer_quote"] == "Synthetic answer quote."
    assert assessment["rubric"] == rubric
    assert assessment["rubric_status"] == "available"
    assert assessment["depth"] == 3
    assert assessment["evidence_quotes"][0]["quote"] == "Synthetic answer quote."
    assert assessment["follow_up_status"] == "completed"
    assert report["follow_up_results"][0]["follow_up_answer"] == "Synthetic answer quote."
    assert "Consistency trade-offs" in report["missing_evidence"]
    assert "does not replace" in report["decision_disclaimer"]
    first_path = reports.export_report("synthetic-candidate", interview_id, {}, "json")
    retry_path = reports.export_report("synthetic-candidate", interview_id, {}, "json")
    assert first_path == retry_path
    assert len(list((tmp_path / "reports").glob("*.json"))) == 1
    text_path = reports.export_report("synthetic-candidate", interview_id, {}, "txt")
    assert text_path.endswith(".txt")
    text_report = reports.export_report("synthetic-candidate", interview_id, {}, "txt")
    assert text_report == text_path
    text_content = (tmp_path / "reports" / Path(text_path).name).read_text(encoding="utf-8")
    assert "Competency assessments:" in text_content
    assert "Synthetic answer quote." in text_content
    assert "Rubric:" in text_content
    assert "Follow-up results:" in text_content
    assert len(list((tmp_path / "reports").glob("*.txt"))) == 1
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
    assert report["competencies"] == {}
    assert report["follow_up_results"] == []
    assert report["supporting_evidence"] == []
    engine.dispose()


def test_report_rejects_missing_or_wrong_candidate_session(tmp_path):
    engine, factory, reports = _report_service(tmp_path)
    with factory.begin() as session:
        session.add(
            InterviewRecord(
                id="interview-private",
                candidate_id="actual-candidate",
                position="Backend Engineer",
                mode="text",
                current_question="Initial question",
                state_json=json.dumps({"status": "active"}),
            )
        )

    for candidate_id, interview_id in (
        ("other-candidate", "interview-private"),
        ("actual-candidate", "missing-interview"),
    ):
        with pytest.raises(LookupError, match="Persisted interview not found"):
            reports.build_report(candidate_id, interview_id)
    engine.dispose()


def test_report_does_not_label_unquoted_evaluator_text_as_answer_evidence(tmp_path):
    engine, factory, reports = _report_service(tmp_path)
    now = datetime.now(UTC)
    with factory.begin() as session:
        session.add(
            InterviewRecord(
                id="interview-unquoted-evidence",
                candidate_id="synthetic-candidate",
                position="Backend Engineer",
                mode="text",
                current_question="Initial question",
                state_json=json.dumps({"status": "active"}),
            )
        )
        turn = InterviewTurnRecord(
            interview_id="interview-unquoted-evidence",
            turn_id="synthetic-turn",
            sequence_number=1,
            status="completed",
            candidate_answer="I used Python in a service.",
            started_at=now,
            completed_at=now,
        )
        session.add(turn)
        session.flush()
        session.add(
            InterviewAssessmentRecord(
                interview_id="interview-unquoted-evidence",
                turn_id=turn.id,
                competency="python",
                score=2,
                status="insufficient_evidence",
                evidence_strength="moderate",
                confidence=0.6,
                strengths_json="[]",
                gaps_json="[]",
                rationale="Assessment rationale.",
            )
        )
        session.add(
            InterviewEvidenceRecord(
                interview_id="interview-unquoted-evidence",
                turn_id=turn.id,
                competency="python",
                evidence_text="Explicitly referenced python.",
                relevance=1.0,
            )
        )

    report = reports.build_report(
        "synthetic-candidate",
        "interview-unquoted-evidence",
    )
    detail = report["competencies"]["python"]["question_assessments"][0]
    assert detail["answer_quote"] == "I used Python in a service."
    assert detail["evidence_quotes"] == []
    assert report["supporting_evidence"] == []
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
