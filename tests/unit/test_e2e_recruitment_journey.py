"""Deterministic end-to-end acceptance test of the core recruitment journey.

One synthetic organization goes from tenant setup to hiring decision and
candidate erasure through the real API and business services. Only
deterministic providers are used (LLM_PROVIDER=none, lexical-only retrieval,
offline email simulation); network, SMTP and live models are never reached.
Negative tests at the end cover tenant isolation, approval gating, hiring
authorization, scheduling, answer idempotency, worker crash recovery, Redis
outage and partial file deletion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.main import app
from apps.api.v1.routes.interview import service as interview_service
from config import settings
from infrastructure.redis_store import RedisClient, RedisStore
from rag.service import campaign_scope_key
from services import report_service as report_module
from services.candidate_application_service import CandidateApplicationService
from services.candidate_cv_storage import CandidateCVStorage
from services.candidate_screening import CandidateScreeningResult
from services.database import (
    AccessAuditRecord,
    CandidateAvailabilityRequestRecord,
    CandidateCommunicationRecord,
    CandidateCriterionAssessmentRecord,
    CandidateCVDocumentRecord,
    CandidateRecord,
    CandidateScreeningRecord,
    InterviewAssessmentRecord,
    InterviewEvidenceRecord,
    InterviewRecord,
    InterviewScheduleRecord,
    InterviewTurnRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignExecutionRecord,
    RecruitmentCampaignHiringOutcomeRecord,
    RecruitmentCampaignRecord,
    SessionLocal,
)
from services.recruitment_campaign_orchestrator import RecruitmentCampaignOrchestrator
from services.recruitment_campaign_worker import RecruitmentCampaignWorker
from services.report_service import report_export_directory
from services.tenant_authorization import TenantContext
from tests.unit.tenant_test_utils import (
    active_membership,
    create_candidate_through_invitation,
    create_tenant,
    start_interview,
    tenant_headers,
)
from tests.unit.test_campaign_lifecycle import (
    _prepare_candidate_with_worker,
    _record_hire,
    _setup_offline_env,
    _wake_execution,
)
from tests.unit.test_interview_scheduling import _confirm, _future_local_slots
from tests.unit.test_recruitment_candidate_intake import (
    create_approved_campaign,
    make_docx,
    submit_cv,
)
from tests.unit.test_tenant_interview_retrieval import (
    _chunk,
    _ingest,
    _RetrievalSpy,
)

# Candidate-facing interview responses are an allowlist (docs/INTERVIEW_API_CONTRACT.md).
CANDIDATE_VIEW_KEYS = {
    "interview_id",
    "status",
    "mode",
    "position",
    "current_question",
    "created_at",
    "updated_at",
    "questions_answered",
    "question_budget",
    "campaign_id",
    "application_id",
    "schedule_id",
}
INTERNAL_KEYS = {
    "retrieved_sources",
    "document_id",
    "chunk_id",
    "scope",
    "retrieved_context",
    "history",
    "evaluation",
    "rubric_snapshot",
    "organization_id",
    "candidate_id",
    "interview_plan",
}
RUBRIC = [
    {
        "criterion_id": "python",
        "competency": "Python experience",
        "weight": 1.0,
        "evidence_terms": ["Python"],
        "score_scale": {"minimum": 0, "maximum": 5, "score_when_evidenced": 4},
    },
    {
        "criterion_id": "kubernetes",
        "competency": "Kubernetes experience",
        "weight": 1.0,
        "evidence_terms": ["Kubernetes"],
        "score_scale": {"minimum": 0, "maximum": 5, "score_when_evidenced": 3},
    },
]
ANSWER = (
    "I built and deployed Python and Kubernetes services, "
    "then improved production latency by 30 percent."
)


def _run_worker_until_idle(max_rounds: int = 40) -> None:
    """Run real worker rounds until no work is left (bounded).

    A shared test database may contain eligible rows from other tests; a single
    ``dispatch_once`` handles only one item per workflow, so drain instead.
    """
    for _ in range(max_rounds):
        if not RecruitmentCampaignWorker().dispatch_once():
            return


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    """CV storage and report exports in private temp dirs; no network or SMTP."""
    _setup_offline_env(monkeypatch, tmp_path)
    report_root = tmp_path_factory.mktemp("journey-reports")
    monkeypatch.setattr(settings, "STORAGE_PATH", str(report_root))
    return tmp_path, report_root


@pytest.fixture
def retrieval_spy(monkeypatch: pytest.MonkeyPatch) -> _RetrievalSpy:
    spy = _RetrievalSpy(interview_service.rag.retriever)
    monkeypatch.setattr(interview_service.rag, "retriever", spy)
    return spy


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value))
    return set()


def _assert_candidate_view(payload: dict) -> None:
    assert set(payload) <= CANDIDATE_VIEW_KEYS, set(payload) - CANDIDATE_VIEW_KEYS
    assert not _keys(payload) & INTERNAL_KEYS


def _document_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = {value["document_id"]} if isinstance(value.get("document_id"), str) else set()
        return found.union(*(_document_ids(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_document_ids(item) for item in value))
    return set()


def _automation_policy(tenant, count: int = 1) -> dict:
    return {
        "response_deadline": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        "interview_duration_minutes": 30,
        "timezone_name": "Asia/Ho_Chi_Minh",
        "proposed_windows": [
            {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat()}
            for start in _future_local_slots(count=count)
        ],
        "interviewer_user_id": tenant.recruiter["id"],
        "exclusive_resources": [],
    }


def _recruiter_context(tenant) -> TenantContext:
    return TenantContext(
        actor_user_id=tenant.recruiter["id"],
        organization_id=tenant.organization_id,
        role="recruiter",
    )


def _audits(**filters) -> list[AccessAuditRecord]:
    with SessionLocal() as session:
        query = select(AccessAuditRecord)
        for column, value in filters.items():
            query = query.where(getattr(AccessAuditRecord, column) == value)
        return list(session.execute(query).scalars())


def test_core_recruitment_journey_end_to_end(
    client: TestClient,
    offline: tuple[Path, Path],
    retrieval_spy: _RetrievalSpy,
):
    cv_root, report_root = offline
    marker = f"JOURNEYPII{uuid4().hex}"
    candidate_name = f"Synthetic Journey {marker[-8:]}"

    # 1. Org admin + tenant setup and campaign creation.
    tenant = create_tenant(client, "Journey Org A")
    other_tenant = create_tenant(client, "Journey Org B")
    org_id = tenant.organization_id
    recruiter = tenant_headers(tenant.recruiter_headers, org_id)
    admin = tenant_headers(tenant.org_admin_headers, org_id)
    admin_membership = active_membership(tenant.org_admin["id"], org_id)
    recruiter_membership = active_membership(tenant.recruiter["id"], org_id)
    assert admin_membership is not None and admin_membership.role == "org_admin"
    assert recruiter_membership is not None and recruiter_membership.role == "recruiter"
    created = client.post(
        "/api/v1/recruitment-campaigns",
        json={"director_hiring_brief": "Hire one platform engineer for the core team."},
        headers=recruiter,
    )
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]
    assert created.json()["approval_state"] == "draft"

    # 2. Recruiter requirements (with the explicit scheduling policy) and objective.
    response = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/requirements",
        json={
            "role": "software engineer",
            "skills": ["Python", "Kubernetes"],
            "campaign_automation": _automation_policy(tenant),
        },
        headers=recruiter,
    )
    assert response.status_code == 200, response.text
    objective = client.put(
        f"/api/v1/recruitment-campaigns/{campaign_id}/objective",
        json={
            "expected_version": 1,
            "idempotency_key": f"objective-{campaign_id}",
            "objective": {
                "requested_role": "Software Engineer",
                "seniority": "Senior",
                "target_headcount": 1,
                "deadline": (datetime.now(UTC).date() + timedelta(days=30)).isoformat(),
                "timezone_name": "UTC",
                "required_qualifications": [],
                "success_criteria": [
                    "Organization administrator confirms the approved hiring criteria."
                ],
                "clarifications": [],
            },
        },
        headers=recruiter,
    )
    assert objective.status_code == 200, objective.text

    # 3. JD, rubric and application form prepared.
    for step, payload in (
        ("job-description", {"job_description": "Build reliable Python services on Kubernetes."}),
        ("rubric", {"competency_rubric": RUBRIC}),
        (
            "application-form",
            {
                "application_form_schema": {
                    "type": "object",
                    "properties": {"portfolio": {"type": "string"}},
                }
            },
        ),
    ):
        response = client.post(
            f"/api/v1/recruitment-campaigns/{campaign_id}/{step}",
            json=payload,
            headers=recruiter,
        )
        assert response.status_code == 200, response.text
    prepared = client.get(f"/api/v1/recruitment-campaigns/{campaign_id}", headers=recruiter)
    assert prepared.status_code == 200
    assert prepared.json()["job_description"].startswith("Build reliable")
    assert [item["criterion_id"] for item in prepared.json()["competency_rubric"]] == [
        "python",
        "kubernetes",
    ]

    # 4. Human approval: the recruiter cannot approve; the org admin does.
    submitted = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/submit-for-approval",
        headers=recruiter,
    )
    assert submitted.status_code == 200, submitted.text
    self_approval = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/approve",
        json={"approval_reason": "Recruiter self-approval."},
        headers=recruiter,
    )
    assert self_approval.status_code == 403
    approved = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/approve",
        json={"approval_reason": "Reviewed criteria for candidate intake."},
        headers=admin,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval_state"] == "approved"
    execution = RecruitmentCampaignOrchestrator.start(campaign_id, _recruiter_context(tenant))
    assert execution.status == "awaiting_progress"

    # 5. Candidate submits the application with a DOCX CV (idempotent replay).
    candidate_user, candidate_auth, profile, _ = create_candidate_through_invitation(
        client, tenant, name=candidate_name
    )
    candidate = tenant_headers(candidate_auth, org_id)
    candidate_id = profile["candidate_id"]
    cv = make_docx(f"{marker} senior engineer built Python and Kubernetes services.")
    uploaded = submit_cv(client, tenant, candidate_auth, campaign_id, cv)
    replayed = submit_cv(client, tenant, candidate_auth, campaign_id, cv)
    assert uploaded.status_code == replayed.status_code == 202, uploaded.text
    application_id = uploaded.json()["id"]
    assert replayed.json()["id"] == application_id
    assert uploaded.json()["document"].get("storage_key") is None

    # 6. The worker's CV step parses this application (targeted, so rows left by
    # other tests in a shared database cannot take its turn).
    assert CandidateApplicationService.process_dispatched(application_id) is not None
    with SessionLocal() as session:
        application = session.get(RecruitmentApplicationRecord, application_id)
        document = session.execute(
            select(CandidateCVDocumentRecord).where(
                CandidateCVDocumentRecord.application_id == application_id
            )
        ).scalar_one()
        storage_key = document.storage_key
        assert application.status == "needs_review"
        assert marker in (document.extracted_text or "")
    assert (cv_root / storage_key).is_file()

    # 7. Screening is evidence-based and deterministic.
    screened = client.get(f"/api/v1/recruitment-applications/{application_id}", headers=recruiter)
    assert screened.status_code == 200, screened.text
    screening = screened.json()["screening"]
    assert screening["result_source"] == "deterministic"
    scores = {item["criterion_id"]: item["score"] for item in screening["assessments"]}
    assert scores == {"python": 4, "kubernetes": 3}

    # 8. Human review of every criterion.
    reviewed = client.post(
        f"/api/v1/recruitment-applications/{application_id}/review",
        json={
            "criteria": [
                {"criterion_id": item["criterion_id"], "status": "confirmed"}
                for item in screening["assessments"]
            ]
        },
        headers=admin,
    )
    assert reviewed.status_code == 200, reviewed.text
    with SessionLocal() as session:
        assert session.get(RecruitmentApplicationRecord, application_id).review_status == "reviewed"

    # 9. Availability: the worker drafts the invitation, humans approve it, the
    # worker creates the request, the candidate responds, the recruiter reviews.
    _run_worker_until_idle()
    with SessionLocal() as session:
        communication_id = session.execute(
            select(CandidateCommunicationRecord.id).where(
                CandidateCommunicationRecord.application_id == application_id,
                CandidateCommunicationRecord.template_type == "interview_invitation",
            )
        ).scalar_one()
    assert client.post(
        f"/api/v1/candidate-communications/{communication_id}/submit", headers=recruiter
    ).status_code == 200
    assert client.post(
        f"/api/v1/candidate-communications/{communication_id}/approve", headers=admin
    ).status_code == 200
    reconciled = RecruitmentCampaignOrchestrator.run_dispatched(_wake_execution(campaign_id))
    assert reconciled is not None and reconciled.status == "awaiting_progress"
    _run_worker_until_idle()
    with SessionLocal() as session:
        communication = session.get(CandidateCommunicationRecord, communication_id)
        assert communication.delivery_status == "simulated"
        request = session.execute(
            select(CandidateAvailabilityRequestRecord).where(
                CandidateAvailabilityRequestRecord.application_id == application_id
            )
        ).scalar_one()
        request_id, zone_name = request.id, request.timezone_name
        window = json.loads(request.proposed_windows_json)[0]
    zone = ZoneInfo(zone_name)
    responded = client.post(
        f"/api/v1/candidate-availability-requests/{request_id}/responses",
        json={
            "idempotency_key": f"journey-availability-{application_id[:16]}",
            "timezone_name": zone_name,
            "windows": [
                {
                    "start": datetime.fromisoformat(window["start"]).astimezone(zone).isoformat(),
                    "end": datetime.fromisoformat(window["end"]).astimezone(zone).isoformat(),
                }
            ],
        },
        headers=candidate,
    )
    assert responded.status_code == 200, responded.text
    availability = client.post(
        f"/api/v1/candidate-availability-requests/{request_id}/review",
        json={"preferred_window_indices": [0]},
        headers=recruiter,
    )
    assert availability.status_code == 200, availability.text

    # 10. The worker proposes the schedule; the interviewer confirms it.
    _run_worker_until_idle()
    with SessionLocal() as session:
        schedule = session.execute(
            select(InterviewScheduleRecord).where(
                InterviewScheduleRecord.application_id == application_id
            )
        ).scalar_one()
        assert schedule.status == "pending_confirmation"
        assert schedule.interviewer_user_id == tenant.recruiter["id"]
        schedule_payload = {"id": schedule.id, "version": schedule.version}
    start_endpoint = (
        f"/api/v1/interview/campaigns/{campaign_id}/applications/{application_id}/start"
    )
    early = client.post(
        start_endpoint, json={"schedule_id": schedule_payload["id"], "mode": "text"}, headers=candidate
    )
    assert early.status_code == 409, early.text
    confirmed = _confirm(client, tenant, schedule_payload, headers=recruiter)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed_internal"

    # 14 (fixtures). Retrieval corpus: this campaign, another org-A campaign, org B.
    suffix = uuid4().hex[:8]
    own_doc = f"journey-own-campaign-{suffix}"
    other_campaign_doc = f"journey-other-campaign-{suffix}"
    other_org_doc = f"journey-other-org-{suffix}"
    _ingest(
        _chunk(own_doc, org_id, job_id=campaign_scope_key(campaign_id)),
        _chunk(other_campaign_doc, org_id, job_id=campaign_scope_key(str(uuid4()))),
        _chunk(other_org_doc, other_tenant.organization_id, job_id=campaign_scope_key(campaign_id)),
    )

    # 11. The candidate starts the campaign interview (allowlisted view).
    started = client.post(
        start_endpoint, json={"schedule_id": schedule_payload["id"], "mode": "text"}, headers=candidate
    )
    assert started.status_code == 200, started.text
    state = started.json()
    _assert_candidate_view(state)
    interview_id = state["interview_id"]
    assert state["status"] == "active" and state["current_question"]

    # 12. The adaptive interview runs to completion.
    answered = 0
    for index in range(12):
        if state["status"] == "completed":
            break
        response = client.post(
            f"/api/v1/interview/{interview_id}/answer",
            json={"turn_id": f"journey-turn-{index}", "answer": f"{ANSWER} {marker}"},
            headers=candidate,
        )
        assert response.status_code == 200, response.text
        state = response.json()
        _assert_candidate_view(state)
        answered += 1
    assert state["status"] == "completed"
    assert answered >= 2

    # 13. Answers are evaluated and assessments persisted.
    with SessionLocal() as session:
        turns = session.execute(
            select(InterviewTurnRecord).where(InterviewTurnRecord.interview_id == interview_id)
        ).scalars().all()
        assessments = session.execute(
            select(InterviewAssessmentRecord).where(
                InterviewAssessmentRecord.interview_id == interview_id
            )
        ).scalars().all()
        evidence_count = session.scalar(
            select(func.count(InterviewEvidenceRecord.id)).where(
                InterviewEvidenceRecord.interview_id == interview_id
            )
        )
    assert len(turns) == answered
    assert all(turn.status == "completed" for turn in turns)
    assert assessments and {item.turn_id for item in assessments} <= {t.id for t in turns}
    assert all(item.evaluator_type == "deterministic" for item in assessments)

    # 14. Retrieval returned only this org's documents for this campaign.
    assert retrieval_spy.calls
    returned = set().union(*(ids for _filters, ids in retrieval_spy.calls))
    assert own_doc in returned
    assert other_campaign_doc not in returned and other_org_doc not in returned
    assert all(
        filters.organization_id == org_id and filters.campaign_id == campaign_id
        for filters, _ids in retrieval_spy.calls
    )

    # 15. Evidence persisted with campaign provenance; staff see internal sources.
    assert evidence_count > 0
    with SessionLocal() as session:
        record = session.get(InterviewRecord, interview_id)
        assert (record.organization_id, record.campaign_id, record.application_id) == (
            org_id,
            campaign_id,
            application_id,
        )
        assert record.schedule_id == schedule_payload["id"]
        assert json.loads(record.rubric_snapshot_json) == RUBRIC
    internal = client.get(f"/api/v1/interview/{interview_id}", headers=recruiter)
    assert internal.status_code == 200
    sources = _document_ids(internal.json())
    assert own_doc in sources
    assert not {other_campaign_doc, other_org_doc} & sources
    candidate_read = client.get(f"/api/v1/interview/{interview_id}", headers=candidate)
    assert candidate_read.status_code == 200
    _assert_candidate_view(candidate_read.json())

    # 16. The interview is complete and the report is final; export it.
    assert internal.json()["status"] == "completed"
    exported = client.post(f"/api/v1/interview/{interview_id}/report?format=json", headers=recruiter)
    assert exported.status_code == 200, exported.text
    report = exported.json()["report"]
    assert report["is_final"] is True
    assert report["campaign"]["campaign_id"] == campaign_id
    export_dir = report_export_directory(report_root, org_id, interview_id)
    assert (export_dir / exported.json()["report_file"]).is_file()

    # 17. Hiring decision: the recruiter is refused, the org admin hires.
    refused = client.post(
        f"/api/v1/recruitment-campaigns/{campaign_id}/applications/{application_id}/outcome",
        json={
            "outcome": "hired",
            "criteria_satisfied": True,
            "idempotency_key": f"recruiter-hire-{application_id[:16]}",
            "reason": "Recruiter attempted to confirm the hire.",
        },
        headers=recruiter,
    )
    assert refused.status_code == 403
    hire = _record_hire(client, tenant, campaign_id, application_id)
    assert hire["outcome"] == "hired" and hire["criteria_satisfied"] is True

    # 18. The worker reconciles campaign progress: target reached.
    completed = RecruitmentCampaignOrchestrator.run_dispatched(_wake_execution(campaign_id))
    assert completed is not None and completed.status == "completed"
    progress = client.get(f"/api/v1/recruitment-campaigns/{campaign_id}/progress", headers=recruiter)
    assert progress.status_code == 200
    assert progress.json()["target_headcount"] == 1
    assert progress.json()["qualifying_hires"] == 1
    assert progress.json()["remaining_hires"] == 0
    assert progress.json()["objective_status"] == "target_achieved"

    # 19. The org admin erases the candidate's personal data. The confirmed slot
    # is still in the future (the interview was taken early), but its interview
    # completed and a hire is recorded, so erasure must not be blocked by it.
    with SessionLocal() as session:
        slot = session.get(InterviewScheduleRecord, schedule_payload["id"])
        slot_end = slot.end_at if slot.end_at.tzinfo else slot.end_at.replace(tzinfo=UTC)
    assert slot_end > datetime.now(UTC)
    erased = client.delete(f"/api/v1/candidates/{candidate_id}/personal-data", headers=admin)
    assert erased.status_code == 200, erased.text
    assert erased.json()["status"] == "completed"
    assert erased.json()["pending_file_count"] == 0
    assert not (cv_root / storage_key).exists()
    assert not export_dir.exists()
    assert client.post(
        f"/api/v1/interview/{interview_id}/report?format=json", headers=recruiter
    ).status_code == 410
    with SessionLocal() as session:
        stored = session.get(CandidateRecord, candidate_id)
        assert stored.user_id is None and candidate_name not in stored.name
        outcome = session.execute(
            select(RecruitmentCampaignHiringOutcomeRecord).where(
                RecruitmentCampaignHiringOutcomeRecord.application_id == application_id
            )
        ).scalar_one()
        assert outcome.outcome == "hired"

    # 20. Audit evidence remains, with identifiers only.
    approval_audits = _audits(resource_id=campaign_id, action="campaign_approved")
    hire_audits = _audits(resource_id=application_id, action="campaign_candidate_hired")
    erasure_audits = [
        row
        for row in _audits(resource_id=candidate_id, resource_type="candidate")
        if row.action.startswith("candidate_personal_data_erasure_")
    ]
    assert [row.actor_user_id for row in approval_audits] == [tenant.org_admin["id"]]
    assert [row.actor_user_id for row in hire_audits] == [tenant.org_admin["id"]]
    assert sorted(row.outcome for row in erasure_audits) == ["completed", "pending"]
    pii = (marker, candidate_name, candidate_user["email"])
    for row in _audits(organization_id=org_id):
        rendered = " ".join(
            str(value)
            for value in (
                row.action,
                row.resource_type,
                row.resource_id,
                row.outcome,
                row.support_reason,
                row.request_id,
            )
        )
        assert not any(needle in rendered for needle in pii), row.action


# --------------------------------------------------------------------------
# Negative paths
# --------------------------------------------------------------------------


@dataclass
class ScheduledCandidate:
    tenant: Any
    campaign_id: str
    application_id: str
    schedule: dict
    candidate_id: str
    candidate_headers: dict[str, str]

    @property
    def start_endpoint(self) -> str:
        return (
            f"/api/v1/interview/campaigns/{self.campaign_id}/applications/"
            f"{self.application_id}/start"
        )


def _scheduled_candidate(client: TestClient) -> ScheduledCandidate:
    """An approved campaign with one reviewed candidate holding a confirmed schedule."""
    tenant = create_tenant(client, "Journey Negative")
    _, candidate_auth, profile, _ = create_candidate_through_invitation(client, tenant)
    campaign_id = create_approved_campaign(
        client, tenant, campaign_automation=_automation_policy(tenant)
    )
    RecruitmentCampaignOrchestrator.start(campaign_id, _recruiter_context(tenant))
    application_id, _communication_id, schedule = _prepare_candidate_with_worker(
        client, tenant, campaign_id, candidate_auth, slot_index=0
    )
    return ScheduledCandidate(
        tenant=tenant,
        campaign_id=campaign_id,
        application_id=application_id,
        schedule=schedule,
        candidate_id=profile["candidate_id"],
        candidate_headers=tenant_headers(candidate_auth, tenant.organization_id),
    )


def _start(client: TestClient, data: ScheduledCandidate, schedule_id: str | None = None):
    return client.post(
        data.start_endpoint,
        json={"schedule_id": schedule_id or data.schedule["id"], "mode": "text"},
        headers=data.candidate_headers,
    )


def _answer(client: TestClient, interview_id: str, headers, turn_id: str, answer: str = ANSWER):
    return client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={"turn_id": turn_id, "answer": answer},
        headers=headers,
    )


def _turn_count(interview_id: str) -> int:
    with SessionLocal() as session:
        return session.scalar(
            select(func.count(InterviewTurnRecord.id)).where(
                InterviewTurnRecord.interview_id == interview_id
            )
        )


def test_cross_tenant_staff_get_not_found_for_campaign_application_interview_and_erasure(
    client: TestClient, offline: tuple[Path, Path]
):
    data = _scheduled_candidate(client)
    started = _start(client, data)
    assert started.status_code == 200, started.text
    interview_id = started.json()["interview_id"]
    other = create_tenant(client, "Journey Intruder")
    org_b = other.organization_id

    for headers in (
        tenant_headers(other.recruiter_headers, org_b),
        tenant_headers(other.org_admin_headers, org_b),
    ):
        for path in (
            f"/api/v1/recruitment-campaigns/{data.campaign_id}",
            f"/api/v1/recruitment-campaigns/{data.campaign_id}/progress",
            f"/api/v1/recruitment-applications/{data.application_id}",
            f"/api/v1/interview/{interview_id}",
        ):
            assert client.get(path, headers=headers).status_code == 404, path
        hire = client.post(
            f"/api/v1/recruitment-campaigns/{data.campaign_id}/applications/"
            f"{data.application_id}/outcome",
            json={
                "outcome": "rejected",
                "criteria_satisfied": False,
                "idempotency_key": f"intruder-{uuid4().hex[:16]}",
                "reason": "Cross-tenant attempt.",
            },
            headers=headers,
        )
        assert hire.status_code == 404
    erase = client.delete(
        f"/api/v1/candidates/{data.candidate_id}/personal-data",
        headers=tenant_headers(other.org_admin_headers, org_b),
    )
    assert erase.status_code == 404
    # Naming org A in the header does not help a non-member.
    forged = client.get(
        f"/api/v1/recruitment-campaigns/{data.campaign_id}",
        headers=tenant_headers(other.recruiter_headers, data.tenant.organization_id),
    )
    assert forged.status_code in {403, 404}
    with SessionLocal() as session:
        assert session.get(CandidateRecord, data.candidate_id).user_id is not None
        assert session.scalar(
            select(func.count(RecruitmentCampaignHiringOutcomeRecord.id)).where(
                RecruitmentCampaignHiringOutcomeRecord.application_id == data.application_id
            )
        ) == 0


def test_unapproved_campaign_blocks_cv_application_and_interview_start(
    client: TestClient, offline: tuple[Path, Path]
):
    tenant = create_tenant(client, "Journey Unapproved")
    _, candidate_auth, _, _ = create_candidate_through_invitation(client, tenant)
    draft = client.post(
        "/api/v1/recruitment-campaigns",
        json={"director_hiring_brief": "Draft campaign that is never approved."},
        headers=tenant_headers(tenant.recruiter_headers, tenant.organization_id),
    )
    assert draft.status_code == 201
    blocked = submit_cv(client, tenant, candidate_auth, draft.json()["id"])
    assert blocked.status_code == 409, blocked.text
    with SessionLocal() as session:
        assert session.get(RecruitmentCampaignRecord, draft.json()["id"]).approval_state == "draft"
        assert session.scalar(
            select(func.count(RecruitmentApplicationRecord.id)).where(
                RecruitmentApplicationRecord.campaign_id == draft.json()["id"]
            )
        ) == 0

    # Approval revoked after scheduling (objective changed): no new CVs, no start.
    data = _scheduled_candidate(client)
    recruiter = tenant_headers(data.tenant.recruiter_headers, data.tenant.organization_id)
    current = client.get(f"/api/v1/recruitment-campaigns/{data.campaign_id}", headers=recruiter)
    revised = client.put(
        f"/api/v1/recruitment-campaigns/{data.campaign_id}/objective",
        json={
            "expected_version": current.json()["objective_version"],
            "idempotency_key": f"revise-{data.campaign_id}",
            "objective": {
                "requested_role": "Staff Engineer",
                "seniority": "Staff",
                "target_headcount": 1,
                "deadline": (datetime.now(UTC).date() + timedelta(days=20)).isoformat(),
                "timezone_name": "UTC",
                "required_qualifications": [],
                "success_criteria": ["Re-approved criteria."],
                "clarifications": [],
            },
        },
        headers=recruiter,
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["approval_state"] == "draft"
    start = _start(client, data)
    assert start.status_code == 409, start.text
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count(InterviewRecord.id)).where(
                InterviewRecord.application_id == data.application_id
            )
        ) == 0
    _, late_auth, _, _ = create_candidate_through_invitation(client, data.tenant)
    late = submit_cv(client, data.tenant, late_auth, data.campaign_id)
    assert late.status_code == 409, late.text


def test_hiring_decision_requires_org_admin(client: TestClient, offline: tuple[Path, Path]):
    data = _scheduled_candidate(client)
    org_id = data.tenant.organization_id
    endpoint = (
        f"/api/v1/recruitment-campaigns/{data.campaign_id}/applications/"
        f"{data.application_id}/outcome"
    )
    for headers in (tenant_headers(data.tenant.recruiter_headers, org_id), data.candidate_headers):
        response = client.post(
            endpoint,
            json={
                "outcome": "hired",
                "criteria_satisfied": True,
                "idempotency_key": f"unauthorized-{uuid4().hex[:16]}",
                "reason": "Unauthorized hire attempt.",
            },
            headers=headers,
        )
        assert response.status_code == 403, response.text
    candidate_reject = client.post(
        endpoint,
        json={
            "outcome": "rejected",
            "criteria_satisfied": False,
            "idempotency_key": f"candidate-reject-{uuid4().hex[:12]}",
            "reason": "Candidate attempted to record an outcome.",
        },
        headers=data.candidate_headers,
    )
    assert candidate_reject.status_code == 403
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count(RecruitmentCampaignHiringOutcomeRecord.id)).where(
                RecruitmentCampaignHiringOutcomeRecord.application_id == data.application_id
            )
        ) == 0
    assert _audits(resource_id=data.application_id, action="campaign_candidate_hired") == []


def test_interview_start_requires_the_confirmed_schedule(
    client: TestClient, offline: tuple[Path, Path]
):
    data = _scheduled_candidate(client)
    assert _start(client, data, schedule_id=str(uuid4())).status_code == 409
    wrong_application = client.post(
        f"/api/v1/interview/campaigns/{data.campaign_id}/applications/{uuid4()}/start",
        json={"schedule_id": data.schedule["id"], "mode": "text"},
        headers=data.candidate_headers,
    )
    assert wrong_application.status_code == 404
    cancelled = client.post(
        f"/api/v1/interview-schedules/{data.schedule['id']}/cancel",
        json={"expected_version": data.schedule["version"], "reason": "Interviewer unavailable"},
        headers=tenant_headers(data.tenant.recruiter_headers, data.tenant.organization_id),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert _start(client, data).status_code == 409
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count(InterviewRecord.id)).where(
                InterviewRecord.application_id == data.application_id
            )
        ) == 0


def test_duplicate_answer_replays_and_conflicting_answer_is_rejected(
    client: TestClient, offline: tuple[Path, Path]
):
    data = _scheduled_candidate(client)
    started = _start(client, data)
    assert started.status_code == 200
    interview_id = started.json()["interview_id"]

    first = _answer(client, interview_id, data.candidate_headers, "dup-turn-1")
    assert first.status_code == 200, first.text
    assert _turn_count(interview_id) == 1
    replay = _answer(client, interview_id, data.candidate_headers, "dup-turn-1")
    assert replay.status_code == 200
    assert replay.json()["current_question"] == first.json()["current_question"]
    assert replay.json()["questions_answered"] == first.json()["questions_answered"]
    assert _turn_count(interview_id) == 1
    conflict = _answer(
        client, interview_id, data.candidate_headers, "dup-turn-1", "A different answer."
    )
    assert conflict.status_code == 409, conflict.text
    assert _turn_count(interview_id) == 1


def test_worker_crash_recovers_expired_leases_and_fences_stale_commits(
    client: TestClient, offline: tuple[Path, Path]
):
    tenant = create_tenant(client, "Journey Crash")
    _, candidate_auth, _, _ = create_candidate_through_invitation(client, tenant)
    campaign_id = create_approved_campaign(client, tenant)
    execution = RecruitmentCampaignOrchestrator.start(campaign_id, _recruiter_context(tenant))
    assert execution.status == "awaiting_progress"
    uploaded = submit_cv(
        client, tenant, candidate_auth, campaign_id, make_docx("Python engineer.")
    )
    assert uploaded.status_code == 202
    application_id = uploaded.json()["id"]

    # Simulate workers that crashed mid-lease on the CV and the execution.
    expired = datetime.now(UTC) - timedelta(seconds=5)
    with SessionLocal.begin() as session:
        application = session.get(RecruitmentApplicationRecord, application_id)
        application.status = "running"
        application.attempt_count = 1
        application.lease_token = "crashed-cv-worker"
        application.lease_expires_at = expired
        record = session.get(RecruitmentCampaignExecutionRecord, execution.id)
        record.status = "running"
        record.lease_token = "crashed-campaign-worker"
        record.lease_expires_at = expired
        record.available_at = expired

    assert application_id in CandidateApplicationService.discover_eligible(50)
    RecruitmentCampaignWorker().dispatch_once()
    with SessionLocal() as session:
        application = session.get(RecruitmentApplicationRecord, application_id)
        assert application.status == "needs_review"
        assert application.lease_token != "crashed-cv-worker"
        assert application.attempt_count == 2
    recovered = RecruitmentCampaignOrchestrator.run_dispatched(execution.id)
    assert recovered is not None and recovered.status == "awaiting_progress"
    with SessionLocal() as session:
        assert session.get(RecruitmentCampaignExecutionRecord, execution.id).lease_token != (
            "crashed-campaign-worker"
        )

    # The crashed worker waking up cannot commit with its stale lease.
    stale = CandidateApplicationService._persist_screening(
        application_id,
        tenant.organization_id,
        "crashed-cv-worker",
        "stale text",
        {"parsed": True},
        CandidateScreeningResult(rubric_version="stale", rubric_snapshot=[], assessments=[]),
    )
    assert stale is None
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count(CandidateScreeningRecord.id)).where(
                CandidateScreeningRecord.application_id == application_id
            )
        ) == 1
        screening_id = session.execute(
            select(CandidateScreeningRecord.id).where(
                CandidateScreeningRecord.application_id == application_id
            )
        ).scalar_one()
        assert session.scalar(
            select(func.count(CandidateCriterionAssessmentRecord.id)).where(
                CandidateCriterionAssessmentRecord.screening_id == screening_id
            )
        ) == 2


def test_redis_outage_fails_interview_answers_closed(
    client: TestClient, offline: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
):
    data = _scheduled_candidate(client)
    started = _start(client, data)
    assert started.status_code == 200
    interview_id = started.json()["interview_id"]

    with monkeypatch.context() as patch:
        unreachable = RedisClient("redis://127.0.0.1:1/0", namespace="interview")
        patch.setattr(interview_service, "redis_client", unreachable)
        patch.setattr(
            interview_service, "redis_store", RedisStore(unreachable, namespace="interview")
        )
        outage = _answer(client, interview_id, data.candidate_headers, "redis-turn-1")
    assert outage.status_code == 503, outage.text
    assert _turn_count(interview_id) == 0

    recovered = _answer(client, interview_id, data.candidate_headers, "redis-turn-1")
    assert recovered.status_code == 200, recovered.text
    assert _turn_count(interview_id) == 1


def test_partial_file_deletion_is_pending_then_retry_completes(
    client: TestClient, offline: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
):
    cv_root, report_root = offline
    tenant = create_tenant(client, "Journey Erasure")
    _, candidate_auth, profile, _ = create_candidate_through_invitation(client, tenant)
    org_id = tenant.organization_id
    campaign_id = create_approved_campaign(client, tenant)
    uploaded = submit_cv(client, tenant, candidate_auth, campaign_id)
    assert uploaded.status_code == 202
    application_id = uploaded.json()["id"]
    assert CandidateApplicationService.process_dispatched(application_id) is not None
    interview_id = start_interview(client, tenant, profile["candidate_id"])["interview_id"]
    assert _answer(
        client, interview_id, tenant_headers(candidate_auth, org_id), "erase-turn-1"
    ).status_code == 200
    exported = client.post(
        f"/api/v1/interview/{interview_id}/report?format=json",
        headers=tenant_headers(tenant.recruiter_headers, org_id),
    )
    assert exported.status_code == 200, exported.text
    export_dir = report_export_directory(report_root, org_id, interview_id)
    with SessionLocal() as session:
        storage_key = session.execute(
            select(CandidateCVDocumentRecord.storage_key).where(
                CandidateCVDocumentRecord.application_id == application_id
            )
        ).scalar_one()
    endpoint = f"/api/v1/candidates/{profile['candidate_id']}/personal-data"
    admin = tenant_headers(tenant.org_admin_headers, org_id)

    def failing(*_args, **_kwargs):
        raise OSError("synthetic storage outage")

    with monkeypatch.context() as patch:
        patch.setattr(CandidateCVStorage, "delete", staticmethod(failing))
        patch.setattr(report_module, "_remove_export_file", failing)
        partial = client.delete(endpoint, headers=admin)
    assert partial.status_code == 202, partial.text
    assert partial.json()["status"] == "deletion_pending"
    assert partial.json()["retryable"] is True
    assert partial.json()["pending_file_count"] == 2
    assert (cv_root / storage_key).is_file()
    assert any(export_dir.iterdir())

    retried = client.delete(endpoint, headers=admin)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "completed"
    assert retried.json()["pending_file_count"] == 0
    assert not (cv_root / storage_key).exists()
    assert not export_dir.exists()
    outcomes = sorted(
        row.outcome
        for row in _audits(resource_id=profile["candidate_id"], resource_type="candidate")
        if row.action.startswith("candidate_personal_data_erasure_")
    )
    assert outcomes.count("partial") == 1 and outcomes.count("completed") == 1
