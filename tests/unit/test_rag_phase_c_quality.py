"""Phase C retrieval quality: normalization, job families, NULL-competency
semantics, evidence traceability, and the fixed probe set.

Lexical-only (no embedding provider); no live provider is called.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agents.interview_agent.graph import InterviewAgentCore
from apps.api.main import app
from rag.retrieval_probes import run_probes
from rag.service import (
    DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE,
    DocumentChunk,
    HybridRetriever,
    RAGQuestionService,
    RetrievalFilters,
    RetrievalResult,
    RetrievedChunk,
    RetrievedContext,
    SQLAlchemyVectorStore,
    _normalize_token,
    _tokens,
    campaign_scope_key,
    job_scope_ids,
    normalize_competency,
)
from services.database import InterviewRecord, SessionLocal
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    start_interview,
    tenant_headers,
)

GLOBAL = {"scope": "global", "scope_approval_reference": DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE}


# --- probe set -------------------------------------------------------------


def test_probe_set_hits_every_positive_without_isolation_violations():
    report = run_probes()

    positives = [case for case in report["cases"] if case["kind"] == "positive"]
    assert positives, "probe dataset must contain positive probes"
    missed = [case["id"] for case in positives if not case["hit"]]
    assert missed == []
    assert report["hit_rate_at_k"] == 1.0
    assert report["zero_result_rate"] == 0.0
    assert report["tenant_isolation_violations"] == 0
    assert report["negative_job_scope_violations"] == 0
    assert report["retrieval_mode"] == "lexical_only"


def test_probe_set_is_deterministic():
    first = run_probes()
    second = run_probes()
    assert first == second


# --- competency normalization ---------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Python experience", "python"),
        ("Python", "python"),
        ("python", "python"),
        ("Database design", "databases"),
        ("SQL", "databases"),
        ("databases", "databases"),
        ("Distributed systems", "distributed_systems"),
        ("Machine learning", "machine_learning"),
        ("ML", "machine_learning"),
        ("general", None),
        ("General", None),
        (None, None),
        ("   ", None),
        ("Stakeholder Communication", "stakeholder_communication"),
        ("Computer vision", "computer_vision"),
    ],
)
def test_normalize_competency_uses_explicit_table(label, expected):
    assert normalize_competency(label) == expected


# --- token normalization ---------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("transactions", "transaction"),
        ("queues", "queue"),
        ("retries", "retry"),
        ("queries", "query"),
        ("processes", "process"),
        ("indexes", "index"),
        ("databases", "database"),
        # Protected: technical terms and words ending in ss/us/is.
        ("postgresql", "postgresql"),
        ("postgres", "postgres"),
        ("kubernetes", "kubernetes"),
        ("redis", "redis"),
        ("langgraph", "langgraph"),
        ("aws", "aws"),
        ("ios", "ios"),
        ("process", "process"),
        ("class", "class"),
        ("status", "status"),
        ("analysis", "analysis"),
        ("k8s", "k8s"),
        ("s3", "s3"),
    ],
)
def test_token_normalization_is_conservative(token, expected):
    assert _normalize_token(token) == expected


def test_tokens_normalize_query_and_documents_identically():
    assert _tokens("Database Transactions, Queues") == ["database", "transaction", "queue"]
    assert _tokens("database transaction queue") == ["database", "transaction", "queue"]


# --- job scope -------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "family"),
    [
        ("Software Engineer", "software_engineer"),
        ("Senior Software Engineer", "software_engineer"),
        ("Staff Software Engineer", "software_engineer"),
        ("Backend Engineer", "software_engineer"),
        ("Software Developer", "software_engineer"),
        ("ML Engineer", "ai_engineer"),
        ("Machine Learning Engineer", "ai_engineer"),
        ("Senior AI Engineer", "ai_engineer"),
        ("Lead AI Engineer", "ai_engineer"),
    ],
)
def test_job_titles_resolve_to_controlled_family(title, family):
    ids = job_scope_ids(title)
    assert ids[0] == title.lower().replace(" ", "_")  # exact slug is kept first
    assert ids[-1] == family


@pytest.mark.parametrize(
    "title",
    ["Product Manager", "Senior Product Manager", "Data Analyst", "Engineering Manager", "Senior"],
)
def test_unrelated_roles_do_not_map_to_engineering_families(title):
    ids = job_scope_ids(title)
    assert len(ids) == 1
    assert not {"software_engineer", "ai_engineer"} & set(ids)


def test_campaign_scope_is_not_rewritten_or_granted_by_aliases():
    key = campaign_scope_key("abc")
    assert job_scope_ids(key) == (key,)
    filters = RetrievalFilters(job_id="senior_software_engineer", job_aliases=("software_engineer", key))
    assert filters.allowed_job_ids() == ("senior_software_engineer", "software_engineer")
    # Aliases never stand in for a job id on their own.
    assert RetrievalFilters(job_aliases=("software_engineer",)).allowed_job_ids() == ()


def test_retrieve_context_passes_normalized_competency_and_job_family():
    class Capturing:
        def retrieve(self, query, filters=None, top_k=5):
            self.filters = filters
            return RetrievalResult(query, [], 0, 0, False)

    retriever = Capturing()
    service = RAGQuestionService(retriever)
    service.retrieve_context("Senior Software Engineer", "Python experience", ["concurrency"], organization_id="org-a")
    assert retriever.filters.competency == "python"
    assert retriever.filters.job_id == "senior_software_engineer"
    assert retriever.filters.allowed_job_ids() == ("senior_software_engineer", "software_engineer")
    assert retriever.filters.organization_id == "org-a"

    service.retrieve_context("Software Engineer", "general", ["concurrency"])
    assert retriever.filters.competency is None


# --- NULL-competency semantics (in-memory path matches SQL path) -----------


def _org_chunk(document_id, organization_id, competency=None, text="transaction isolation retries"):
    return DocumentChunk(
        document_id,
        "c0",
        text,
        competency=competency,
        scope="organization",
        organization_id=organization_id,
        source="phase-c-test",
    )


def test_untagged_tenant_chunks_match_any_competency_in_memory():
    documents = [
        _org_chunk("untagged", "org-a"),
        _org_chunk("tagged-db", "org-a", competency="databases"),
        _org_chunk("tagged-python", "org-a", competency="python"),
        _org_chunk("other-tenant", "org-b"),
    ]
    result = HybridRetriever(documents).retrieve(
        "transactions", RetrievalFilters(competency="databases", organization_id="org-a"), top_k=10
    )
    assert {item.chunk.document_id for item in result.chunks} == {"untagged", "tagged-db"}


def test_untagged_tenant_chunks_match_any_competency_in_sql_store(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from services.database import RAGDocumentRecord

    engine = create_engine(f"sqlite:///{tmp_path / 'phase-c.db'}")
    RAGDocumentRecord.__table__.create(engine)
    store = SQLAlchemyVectorStore(sessionmaker(bind=engine, expire_on_commit=False))
    store.upsert(
        [
            _org_chunk("untagged", "org-a"),
            _org_chunk("label-tagged", "org-a", competency="Database design"),
            _org_chunk("tagged-python", "org-a", competency="python"),
            _org_chunk("general-tagged", "org-a", competency="general"),
            _org_chunk("other-tenant", "org-b"),
        ],
        None,
    )
    try:
        result = HybridRetriever(vector_store=store).retrieve(
            "transactions", RetrievalFilters(competency="SQL", organization_id="org-a"), top_k=10
        )
        assert {item.chunk.document_id for item in result.chunks} == {
            "untagged",
            "label-tagged",
            "general-tagged",
        }
    finally:
        engine.dispose()


# --- evidence traceability ------------------------------------------------


def _result(*chunks):
    return RetrievalResult(
        "q",
        [RetrievedChunk(chunk, 1.0) for chunk in chunks],
        0,
        len(chunks),
        True,
        fallback_reason="semantic_embeddings_unconfigured_lexical_only",
    )


def test_format_context_with_sources_keeps_attribution_and_only_ids():
    own = _org_chunk("own-doc", "org-a")
    foreign = _org_chunk("foreign-doc", "org-b")
    global_chunk = DocumentChunk("global-doc", "g0", "global transaction text", **GLOBAL)
    service = RAGQuestionService(retriever=object())

    context = service.format_context_with_sources(_result(own, global_chunk, foreign), "org-a")

    assert isinstance(context, RetrievedContext) and isinstance(context, str)
    assert "[UNTRUSTED_CONTEXT source=own-doc chunk=c0]" in context
    assert "[LEXICAL_ONLY]" in context
    assert list(context.sources) == [
        {"document_id": "own-doc", "chunk_id": "c0", "scope": "organization"},
        {"document_id": "global-doc", "chunk_id": "g0", "scope": "global"},
    ]
    assert all(set(source) == {"document_id", "chunk_id", "scope"} for source in context.sources)
    assert service.format_context(_result(own)) == str(service.format_context_with_sources(_result(own)))


def test_graph_persists_retrieved_sources_on_the_turn():
    sources = [{"document_id": "doc-1", "chunk_id": "c0", "scope": "organization"}]

    def retriever(_state):
        return RetrievedContext("[UNTRUSTED_CONTEXT source=doc-1 chunk=c0] guidance", sources)

    agent = InterviewAgentCore(context_retriever=retriever)
    state = agent.start_interview("candidate", "Software Engineer")
    updated = agent.handle_answer(state, "I used Redis to cache API responses.", turn_id="t-1")

    entry = updated["history"][-1]
    assert entry["turn_id"] == "t-1"
    assert entry["retrieved_sources"] == sources
    assert entry["generated_question"] == updated["current_question"]
    assert updated["retrieved_sources"] == sources
    json.dumps(updated["history"])  # persisted through state_json

    plain = InterviewAgentCore(context_retriever=lambda _state: "plain text context")
    updated_plain = plain.handle_answer(plain.start_interview("c", "Software Engineer"), "I used Redis.")
    assert updated_plain["history"][-1]["retrieved_sources"] == []


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_interview_turn_persists_only_own_tenant_and_global_source_ids(client: TestClient):
    tenant_a = create_tenant(client, "Tenant A")
    tenant_b = create_tenant(client, "Tenant B")
    suffix = uuid4().hex[:8]
    token = f"quorumtrace{suffix}"
    own, other = f"a-trace-{suffix}", f"b-trace-{suffix}"
    SQLAlchemyVectorStore().upsert(
        [
            _org_chunk(own, tenant_a.organization_id, text=f"{token} rollout latency guidance"),
            _org_chunk(other, tenant_b.organization_id, text=f"{token} rollout latency guidance"),
        ],
        None,
    )
    _candidate, candidate_headers, profile, _ = create_candidate_through_invitation(client, tenant_a)
    interview = start_interview(client, tenant_a, profile["candidate_id"])
    headers = tenant_headers(candidate_headers, tenant_a.organization_id)

    response = client.post(
        f"/api/v1/interview/{interview['interview_id']}/answer",
        json={"turn_id": "trace-turn", "answer": f"I shipped the {token} rollout and measured latency."},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        state = json.loads(session.get(InterviewRecord, interview["interview_id"]).state_json)
    entry = state["history"][-1]
    assert entry["turn_id"] == "trace-turn"
    source_ids = {source["document_id"] for source in entry["retrieved_sources"]}
    assert own in source_ids
    assert other not in source_ids
    assert all(source["scope"] in {"organization", "global"} for source in entry["retrieved_sources"])
    assert all(set(source) == {"document_id", "chunk_id", "scope"} for source in entry["retrieved_sources"])
    assert token not in json.dumps(entry["retrieved_sources"])
