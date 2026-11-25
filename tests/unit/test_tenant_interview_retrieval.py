"""Tenant and campaign isolation for interview-time retrieval.

Every assertion checks the document IDs the real retriever returned from the
database, not just the filter arguments. Retrieval is lexical-only
(EMBEDDING_PROVIDER=none); no live provider is called.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import app
from apps.api.v1.routes.interview import service as interview_service
from rag.service import (
    DocumentChunk,
    HybridRetriever,
    RAGQuestionService,
    RetrievalFilters,
    SQLAlchemyVectorStore,
    _is_verified_global,
    campaign_scope_key,
)
from services.database import (
    InterviewRecord,
    RecruitmentCampaignExecutionRecord,
    SessionLocal,
)
from services.interview_service import InterviewContextUnavailableError
from services.recruitment_campaign_orchestrator import RecruitmentCampaignOrchestrator
from services.tenant_authorization import TenantContext
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    start_interview,
    tenant_headers,
)

# A token shared by every fixture document so lexical scoring cannot be what
# separates tenants: only the data-layer filter can.
SHARED_TOKEN = "zephyrquorum"
# Fixtures carry one chunk per competency the interview queries (untagged, the
# generic first turn and the default campaign rubric competency) so isolation is
# exercised across every competency path, not only the untagged one.
FIXTURE_COMPETENCIES = (None, "general", "Python experience")


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _chunk(
    document_id: str, organization_id: str, *, job_id: str | None = None
) -> list[DocumentChunk]:
    return [
        DocumentChunk(
            document_id=document_id,
            chunk_id=f"c{index}",
            text=f"{SHARED_TOKEN} guidance probe deployment evidence {document_id}",
            competency=competency,
            scope="organization",
            organization_id=organization_id,
            job_id=job_id,
            source=f"tenant-isolation-{document_id}",
        )
        for index, competency in enumerate(FIXTURE_COMPETENCIES)
    ]


def _ingest(*chunk_groups: list[DocumentChunk]) -> None:
    SQLAlchemyVectorStore().upsert(
        [chunk for group in chunk_groups for chunk in group], None
    )


def _service() -> RAGQuestionService:
    service = RAGQuestionService(
        HybridRetriever(vector_store=SQLAlchemyVectorStore())
    )
    service.max_chunks = 50
    return service


def _ids(result) -> set[str]:
    return {item.chunk.document_id for item in result.chunks}


def _private_ids(result) -> set[str]:
    return {
        item.chunk.document_id
        for item in result.chunks
        if item.chunk.scope != "global"
    }


def _retrieve(service, organization_id, campaign_id=None):
    return service.retrieve_context(
        "Synthetic Engineer",
        None,
        [SHARED_TOKEN],
        organization_id=organization_id,
        campaign_id=campaign_id,
    )


@pytest.fixture
def two_tenants(client: TestClient):
    tenant_a = create_tenant(client, "Tenant A")
    tenant_b = create_tenant(client, "Tenant B")
    suffix = uuid4().hex[:8]
    docs = {
        "a": f"a-general-{suffix}",
        "b": f"b-general-{suffix}",
    }
    _ingest(
        _chunk(docs["a"], tenant_a.organization_id),
        _chunk(docs["b"], tenant_b.organization_id),
    )
    return tenant_a, tenant_b, docs


def test_each_organization_retrieves_only_its_own_documents(two_tenants):
    tenant_a, tenant_b, docs = two_tenants
    service = _service()

    result_a = _retrieve(service, tenant_a.organization_id)
    result_b = _retrieve(service, tenant_b.organization_id)

    assert docs["a"] in _ids(result_a)
    assert docs["b"] not in _ids(result_a)
    assert docs["b"] in _ids(result_b)
    assert docs["a"] not in _ids(result_b)
    assert all(
        item.chunk.organization_id == tenant_a.organization_id
        for item in result_a.chunks
        if item.chunk.scope != "global"
    )


def test_missing_organization_returns_no_private_documents(two_tenants):
    _tenant_a, _tenant_b, docs = two_tenants
    service = _service()

    result = _retrieve(service, None)
    relevant = service.retrieve_relevant_questions(
        "Synthetic Engineer", {SHARED_TOKEN: {"gaps": [SHARED_TOKEN]}}
    )

    assert _private_ids(result) == set()
    assert all(_is_verified_global(item.chunk) for item in result.chunks)
    assert not any(docs["a"] in text or docs["b"] in text for text in relevant)


def test_campaign_documents_are_restricted_to_the_authorized_campaign(client: TestClient):
    tenant_a = create_tenant(client, "Tenant A")
    tenant_b = create_tenant(client, "Tenant B")
    campaign_x, campaign_y = str(uuid4()), str(uuid4())
    suffix = uuid4().hex[:8]
    general = f"a-general-{suffix}"
    doc_x = f"a-campaign-x-{suffix}"
    doc_y = f"a-campaign-y-{suffix}"
    _ingest(
        _chunk(general, tenant_a.organization_id),
        _chunk(doc_x, tenant_a.organization_id, job_id=campaign_scope_key(campaign_x)),
        _chunk(doc_y, tenant_a.organization_id, job_id=campaign_scope_key(campaign_y)),
    )
    service = _service()

    in_x = _ids(_retrieve(service, tenant_a.organization_id, campaign_x))
    no_campaign = _ids(_retrieve(service, tenant_a.organization_id))
    other_tenant_same_campaign = _ids(_retrieve(service, tenant_b.organization_id, campaign_x))

    assert {general, doc_x} <= in_x
    assert doc_y not in in_x
    assert general in no_campaign
    assert doc_x not in no_campaign and doc_y not in no_campaign
    assert not {general, doc_x, doc_y} & other_tenant_same_campaign


def test_campaign_scope_key_rejects_blank_campaign():
    with pytest.raises(ValueError):
        campaign_scope_key("  ")
    assert RetrievalFilters(job_id="role").allowed_job_ids() == ("role",)


class _RetrievalSpy:
    """Wraps the real retriever and records filters plus the returned IDs."""

    def __init__(self, retriever) -> None:
        self._retriever = retriever
        self.calls: list[tuple[RetrievalFilters, set[str]]] = []

    def retrieve(self, query, filters=None, top_k=5):
        result = self._retriever.retrieve(query, filters, top_k=50)
        self.calls.append((filters, _ids(result)))
        return result


@pytest.fixture
def retrieval_spy(monkeypatch: pytest.MonkeyPatch):
    spy = _RetrievalSpy(interview_service.rag.retriever)
    monkeypatch.setattr(interview_service.rag, "retriever", spy)
    return spy


def _answer(client, interview_id, headers, turn_id):
    return client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={
            "turn_id": turn_id,
            "answer": f"I shipped the {SHARED_TOKEN} rollout and measured the latency result.",
        },
        headers=headers,
    )


def _interview_for(client, tenant):
    _candidate, candidate_headers, profile, _ = create_candidate_through_invitation(
        client, tenant
    )
    interview = start_interview(client, tenant, profile["candidate_id"])
    return interview["interview_id"], tenant_headers(
        candidate_headers, tenant.organization_id
    ), candidate_headers


def test_interview_turn_retrieves_only_its_tenant_documents(
    client: TestClient, two_tenants, retrieval_spy: _RetrievalSpy
):
    tenant_a, _tenant_b, docs = two_tenants
    interview_id, headers, _ = _interview_for(client, tenant_a)

    response = _answer(client, interview_id, headers, "tenant-turn-1")

    assert response.status_code == 200, response.text
    assert retrieval_spy.calls, "interview turn should run tenant retrieval"
    returned = set().union(*(ids for _filters, ids in retrieval_spy.calls))
    assert docs["a"] in returned
    assert docs["b"] not in returned
    assert all(
        filters.organization_id == tenant_a.organization_id
        for filters, _ids_ in retrieval_spy.calls
    )


def test_forged_organization_header_cannot_reach_another_tenant(
    client: TestClient, two_tenants, retrieval_spy: _RetrievalSpy
):
    tenant_a, tenant_b, _docs = two_tenants
    interview_id, _headers, candidate_headers = _interview_for(client, tenant_a)

    forged = tenant_headers(candidate_headers, tenant_b.organization_id)
    response = _answer(client, interview_id, forged, "forged-header-turn")

    assert response.status_code in {403, 404}, response.text
    assert retrieval_spy.calls == []


def test_forged_state_organization_is_ignored_in_favor_of_persisted_tenant(
    client: TestClient, two_tenants, retrieval_spy: _RetrievalSpy
):
    tenant_a, tenant_b, docs = two_tenants
    interview_id, headers, _ = _interview_for(client, tenant_a)
    with SessionLocal.begin() as session:
        record = session.get(InterviewRecord, interview_id)
        state = json.loads(record.state_json)
        state["organization_id"] = tenant_b.organization_id
        state["campaign_id"] = str(uuid4())
        record.state_json = json.dumps(state)

    response = _answer(client, interview_id, headers, "forged-state-turn")

    assert response.status_code == 200, response.text
    returned = set().union(*(ids for _filters, ids in retrieval_spy.calls))
    assert docs["a"] in returned
    assert docs["b"] not in returned
    assert all(
        filters.organization_id == tenant_a.organization_id
        and filters.campaign_id is None
        for filters, _ids_ in retrieval_spy.calls
    )
    # Candidate responses omit organization_id (Phase E allowlist); verify the
    # persisted state was re-bound to the row's tenant instead.
    assert "organization_id" not in response.json()
    assert "campaign_id" not in response.json()
    with SessionLocal() as session:
        persisted = json.loads(session.get(InterviewRecord, interview_id).state_json)
    assert persisted["organization_id"] == tenant_a.organization_id


def test_replayed_and_retried_turns_keep_tenant_filtering(
    client: TestClient,
    two_tenants,
    retrieval_spy: _RetrievalSpy,
    monkeypatch: pytest.MonkeyPatch,
):
    tenant_a, _tenant_b, docs = two_tenants
    interview_id, headers, _ = _interview_for(client, tenant_a)

    from integrations.llm_providers.provider import LLMTimeoutError

    original = interview_service.evaluator.evaluate_result
    monkeypatch.setattr(
        interview_service.evaluator,
        "evaluate_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(LLMTimeoutError("synthetic")),
    )
    failed = _answer(client, interview_id, headers, "retry-turn")
    assert failed.status_code == 503, failed.text
    monkeypatch.setattr(interview_service.evaluator, "evaluate_result", original)

    retried = _answer(client, interview_id, headers, "retry-turn")
    assert retried.status_code == 200, retried.text
    calls_after_retry = len(retrieval_spy.calls)
    replayed = _answer(client, interview_id, headers, "retry-turn")

    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["current_question"] == retried.json()["current_question"]
    # A completed-turn replay serves persisted state and runs no new retrieval.
    assert len(retrieval_spy.calls) == calls_after_retry
    returned = set().union(*(ids for _filters, ids in retrieval_spy.calls))
    assert docs["a"] in returned and docs["b"] not in returned
    assert all(
        filters.organization_id == tenant_a.organization_id
        for filters, _ids_ in retrieval_spy.calls
    )


def test_graph_context_retrieval_fails_closed_without_tenant(retrieval_spy: _RetrievalSpy):
    with pytest.raises(InterviewContextUnavailableError):
        interview_service._retrieve_graph_context(
            {"position": "Synthetic Engineer", "organization_id": None}
        )
    with pytest.raises(InterviewContextUnavailableError):
        interview_service._retrieve_graph_context(
            {"position": "Synthetic Engineer", "organization_id": "  "}
        )
    assert retrieval_spy.calls == []


def test_worker_context_is_bound_to_the_execution_tenant(client: TestClient):
    from tests.unit.test_recruitment_candidate_intake import create_approved_campaign

    tenant_a = create_tenant(client, "Tenant A")
    tenant_b = create_tenant(client, "Tenant B")
    executions = {}
    for tenant in (tenant_a, tenant_b):
        campaign_id = create_approved_campaign(client, tenant)
        execution = RecruitmentCampaignOrchestrator.start(
            campaign_id,
            TenantContext(
                actor_user_id=tenant.recruiter["id"],
                organization_id=tenant.organization_id,
                role="recruiter",
            ),
        )
        executions[tenant.organization_id] = execution.id

    for organization_id, execution_id in executions.items():
        context = RecruitmentCampaignOrchestrator._worker_context(execution_id)
        assert context is not None
        assert context.organization_id == organization_id
        assert context.is_automated is True

    # An execution whose tenant no longer matches its campaign gets no context.
    execution_a = executions[tenant_a.organization_id]
    with SessionLocal.begin() as session:
        session.execute(
            update(RecruitmentCampaignExecutionRecord)
            .where(RecruitmentCampaignExecutionRecord.id == execution_a)
            .values(organization_id=tenant_b.organization_id)
        )
    assert RecruitmentCampaignOrchestrator._worker_context(execution_a) is None


def test_worker_prepared_campaign_interview_uses_its_campaign_documents(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retrieval_spy: _RetrievalSpy,
):
    from tests.unit.test_campaign_lifecycle import (
        _prepare_candidate_with_worker,
        _setup_offline_env,
    )
    from tests.unit.test_interview_scheduling import _future_local_slots
    from tests.unit.test_recruitment_candidate_intake import create_approved_campaign

    _setup_offline_env(monkeypatch, tmp_path)
    tenant_a = create_tenant(client, "Tenant A")
    tenant_b = create_tenant(client, "Tenant B")
    _, candidate_headers, _, _ = create_candidate_through_invitation(client, tenant_a)
    windows = _future_local_slots(count=1)
    campaign_id = create_approved_campaign(
        client,
        tenant_a,
        campaign_automation={
            "response_deadline": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "interview_duration_minutes": 30,
            "timezone_name": "Asia/Ho_Chi_Minh",
            "proposed_windows": [
                {
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=30)).isoformat(),
                }
                for start in windows
            ],
            "interviewer_user_id": tenant_a.recruiter["id"],
            "exclusive_resources": [],
        },
    )
    RecruitmentCampaignOrchestrator.start(
        campaign_id,
        TenantContext(
            actor_user_id=tenant_a.recruiter["id"],
            organization_id=tenant_a.organization_id,
            role="recruiter",
        ),
    )
    application_id, _communication_id, schedule = _prepare_candidate_with_worker(
        client, tenant_a, campaign_id, candidate_headers, slot_index=0
    )

    suffix = uuid4().hex[:8]
    own_campaign = f"a-this-campaign-{suffix}"
    other_campaign = f"a-other-campaign-{suffix}"
    other_tenant = f"b-same-campaign-id-{suffix}"
    _ingest(
        _chunk(own_campaign, tenant_a.organization_id, job_id=campaign_scope_key(campaign_id)),
        _chunk(
            other_campaign,
            tenant_a.organization_id,
            job_id=campaign_scope_key(str(uuid4())),
        ),
        _chunk(other_tenant, tenant_b.organization_id, job_id=campaign_scope_key(campaign_id)),
    )

    headers = tenant_headers(candidate_headers, tenant_a.organization_id)
    started = client.post(
        f"/api/v1/interview/campaigns/{campaign_id}/applications/{application_id}/start",
        json={"schedule_id": schedule["id"], "mode": "text"},
        headers=headers,
    )
    assert started.status_code == 200, started.text
    interview_id = started.json()["interview_id"]

    response = _answer(client, interview_id, headers, "campaign-tenant-turn")

    assert response.status_code == 200, response.text
    assert retrieval_spy.calls
    returned = set().union(*(ids for _filters, ids in retrieval_spy.calls))
    assert own_campaign in returned
    assert other_campaign not in returned
    assert other_tenant not in returned
    assert all(
        filters.organization_id == tenant_a.organization_id
        and filters.campaign_id == campaign_id
        for filters, _ids_ in retrieval_spy.calls
    )
    with SessionLocal() as session:
        stored = session.execute(
            select(InterviewRecord.organization_id, InterviewRecord.campaign_id).where(
                InterviewRecord.id == interview_id
            )
        ).one()
    assert tuple(stored) == (tenant_a.organization_id, campaign_id)
