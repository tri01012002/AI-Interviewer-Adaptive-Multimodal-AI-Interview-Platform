from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from config import settings
from rag.corpus_loader import load_demo_corpus
from rag.service import (
    DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE,
    DenseRetriever,
    DocumentChunk,
    EmbeddingProviderError,
    HybridRetriever,
    OpenAIEmbeddingProvider,
    RAGQuestionService,
    RetrievalFilters,
    RetrievalResult,
    SQLAlchemyVectorStore,
)
from services.database import RAGDocumentRecord

GLOBAL_APPROVAL = DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE


class FakeSemanticEmbedding:
    model = "fake-semantic"
    version = "fake-semantic-v1"
    dimension = 3

    def embed(self, text: str) -> list[float]:
        lowered = text.lower()
        if any(token in lowered for token in ("async", "external calls", "backpressure")):
            return [1.0, 0.0, 0.0]
        if any(token in lowered for token in ("cache", "stale", "invalidation")):
            return [0.0, 0.0, 1.0]
        return [0.0, 1.0, 0.0]


def _vector_store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rag-test.db'}")
    RAGDocumentRecord.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory, SQLAlchemyVectorStore(factory)


def _insert_global_fixture(factory, document, approval_reference=GLOBAL_APPROVAL):
    with factory.begin() as session:
        session.add(
            RAGDocumentRecord(
                document_id=document.document_id,
                chunk_id=document.chunk_id,
                text=document.text,
                document_type=document.source_type,
                competency_id=document.competency,
                job_id=document.job_id,
                organization_id=None,
                scope="global",
                scope_approval_reference=approval_reference,
                difficulty=document.difficulty,
                source=document.source,
                version=document.version,
                active=document.active,
                language=document.language,
                tags_json="[]",
            )
        )


def test_sqlite_database_url_is_respected(monkeypatch, tmp_path):
    from services.database import _resolve_database_url

    database_url = f"sqlite:///{tmp_path / 'configured.db'}"
    monkeypatch.setattr(settings, "DATABASE_URL", database_url)

    assert _resolve_database_url() == database_url


def test_openai_embedding_provider_uses_configured_model_dimension_and_validates(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "openai")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "test-embedding-model")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 3)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    calls = []

    class FakeClient:
        class embeddings:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                return SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, 0.0, 0.0])])

    provider = OpenAIEmbeddingProvider(FakeClient())

    assert provider.embed("a semantic query") == [1.0, 0.0, 0.0]
    assert provider.model == "test-embedding-model"
    assert provider.dimension == 3
    assert calls == [{
        "model": "test-embedding-model",
        "input": "a semantic query",
        "dimensions": 3,
    }]


def test_embedding_provider_rejects_invalid_dimensions_and_provider_failures(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "openai")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "test-embedding-model")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 3)

    class InvalidClient:
        class embeddings:
            @staticmethod
            def create(**kwargs):
                assert kwargs["input"]
                return SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, 0.0])])

    with pytest.raises(EmbeddingProviderError, match="dimension mismatch"):
        OpenAIEmbeddingProvider(InvalidClient()).embed("query")

    class BrokenClient:
        class embeddings:
            @staticmethod
            def create(**kwargs):
                assert kwargs["input"]
                raise TimeoutError

    with pytest.raises(EmbeddingProviderError, match="TimeoutError"):
        OpenAIEmbeddingProvider(BrokenClient()).embed("query")


def test_provider_configuration_is_explicit_without_credentials(monkeypatch):
    from rag.service import configured_embedding_provider

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    with pytest.raises(EmbeddingProviderError, match="requires OPENAI_API_KEY"):
        configured_embedding_provider()

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "none")
    assert configured_embedding_provider() is None

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "unknown-provider")
    with pytest.raises(EmbeddingProviderError, match="unsupported embedding provider"):
        configured_embedding_provider()


def test_durable_ingestion_reindex_and_dense_semantic_ranking(tmp_path):
    engine, factory, store = _vector_store(tmp_path)
    provider = FakeSemanticEmbedding()
    documents = [
        DocumentChunk(
            "python-async-knowledge",
            "chunk-1",
            "Async I/O uses an event loop and backpressure.",
            competency="python",
            source_type="technical_knowledge",
            job_id="ai_engineer",
            source="test-corpus",
            organization_id="org-a",
            scope="organization",
        ),
        DocumentChunk(
            "database-transaction-knowledge",
            "chunk-1",
            "Database transactions use locks to coordinate writes.",
            competency="databases",
            source_type="technical_knowledge",
            job_id="software_engineer",
            source="test-corpus",
            organization_id="org-a",
            scope="organization",
        ),
    ]

    assert store.upsert(
        documents,
        provider,
        replace_source=True,
    ) == 2
    assert store.upsert(
        documents,
        provider,
        replace_source=True,
    ) == 2

    with factory() as session:
        assert session.query(func.count(RAGDocumentRecord.id)).scalar() == 2

    reopened_store = SQLAlchemyVectorStore(factory)
    results = reopened_store.retrieve(
        "Keep a service available while many external calls are pending",
        RetrievalFilters(
            competency="python",
            job_id="ai_engineer",
            organization_id="org-a",
        ),
        2,
        provider,
    )
    assert results[0].chunk.document_id == "python-async-knowledge"
    assert results[0].dense_score == 1.0
    assert reopened_store.documents(
        RetrievalFilters(
            competency="python",
            job_id="software_engineer",
            organization_id="org-a",
        )
    ) == []
    engine.dispose()


def test_missing_or_stale_semantic_vectors_use_labeled_lexical_results(tmp_path):
    engine, _, store = _vector_store(tmp_path)
    documents = [
        DocumentChunk(
            "redis-cache",
            "chunk",
            "Redis cache invalidation prevents stale values.",
            competency="distributed_systems",
            job_id="software_engineer",
            source_type="technical_knowledge",
            organization_id="org-a",
            scope="organization",
        )
    ]
    store.upsert(documents, None)
    retriever = HybridRetriever(
        dense=DenseRetriever(FakeSemanticEmbedding()),
        vector_store=store,
    )
    missing = retriever.retrieve(
        "Redis cache invalidation",
        RetrievalFilters(
            competency="distributed_systems",
            job_id="software_engineer",
            organization_id="org-a",
        ),
    )
    assert missing.chunks
    assert missing.dense_count == 0
    assert missing.fallback_reason == "no_matching_semantic_vectors"

    store.upsert(
        documents,
        FakeSemanticEmbedding(),
    )

    class NewEmbeddingVersion(FakeSemanticEmbedding):
        version = "fake-semantic-v2"

    stale = HybridRetriever(
        dense=DenseRetriever(NewEmbeddingVersion()),
        vector_store=store,
    ).retrieve(
        "Redis cache invalidation",
        RetrievalFilters(
            competency="distributed_systems",
            job_id="software_engineer",
            organization_id="org-a",
        ),
    )
    assert stale.chunks
    assert stale.dense_count == 0
    assert stale.fallback_reason == "no_matching_semantic_vectors"
    engine.dispose()


def test_metadata_filters_scope_job_competency_difficulty_and_active(tmp_path):
    engine, factory, store = _vector_store(tmp_path)
    docs = [
        DocumentChunk(
            "target",
            "chunk",
            "Transaction locking, isolation, and retries.",
            competency="databases",
            job_id="software_engineer",
            difficulty="hard",
            source_type="evaluation_rubric",
            source="test-corpus",
            organization_id="org-a",
            scope="organization",
        ),
        DocumentChunk(
            "other-job",
            "chunk",
            "Transaction locking and retries.",
            competency="databases",
            job_id="ai_engineer",
            difficulty="hard",
            source_type="evaluation_rubric",
            source="test-corpus",
            organization_id="org-a",
            scope="organization",
        ),
        DocumentChunk(
            "other-organization",
            "chunk",
            "Transaction locking, isolation, and retries.",
            competency="databases",
            job_id="software_engineer",
            difficulty="hard",
            source_type="evaluation_rubric",
            source="test-corpus",
            organization_id="org-b",
            scope="organization",
        ),
        DocumentChunk(
            "inactive",
            "chunk",
            "Transaction isolation.",
            competency="databases",
            job_id="software_engineer",
            difficulty="hard",
            source_type="evaluation_rubric",
            source="test-corpus",
            scope="global",
            scope_approval_reference=GLOBAL_APPROVAL,
            active=False,
        ),
        DocumentChunk(
            "other-competency",
            "chunk",
            "Cache invalidation and consistency.",
            competency="distributed_systems",
            job_id="software_engineer",
            difficulty="hard",
            source_type="evaluation_rubric",
            source="test-corpus",
        ),
        DocumentChunk(
            "global",
            "chunk",
            "Database transactions and retries.",
            competency="databases",
            source_type="evaluation_rubric",
            source="test-corpus",
            scope="global",
            scope_approval_reference=GLOBAL_APPROVAL,
        ),
    ]
    store.upsert(
        [document for document in docs if document.scope != "global"],
        None,
    )
    for global_document in (document for document in docs if document.scope == "global"):
        _insert_global_fixture(factory, global_document)
    filtered = store.documents(
        RetrievalFilters(
            competency="databases",
            job_id="software_engineer",
            difficulty="hard",
            source_type="evaluation_rubric",
            include_global=True,
            organization_id="org-a",
        )
    )
    assert {document.document_id for document in filtered} == {"target", "global"}
    org_b = store.documents(
        RetrievalFilters(
            competency="databases",
            job_id="software_engineer",
            include_global=True,
            organization_id="org-b",
        )
    )
    assert {document.document_id for document in org_b} == {"other-organization", "global"}
    unscoped = store.documents(RetrievalFilters(include_global=True))
    assert [document.document_id for document in unscoped] == ["global"]
    engine.dispose()


def test_tenant_retrieval_excludes_other_tenants_and_unclassified_legacy_rows(
    tmp_path,
):
    engine, factory, store = _vector_store(tmp_path)
    tenant_a = DocumentChunk(
        "shared-document",
        "shared-chunk",
        "Organization A private corpus text.",
        source="shared-source",
        organization_id="org-a",
        scope="organization",
    )
    tenant_b = DocumentChunk(
        "shared-document",
        "shared-chunk",
        "Organization B private corpus text.",
        source="shared-source",
        organization_id="org-b",
        scope="organization",
    )
    unknown = DocumentChunk(
        "legacy-document",
        "legacy-chunk",
        "Unclassified legacy corpus text.",
        source="legacy-source",
    )
    legacy_with_org_hint = DocumentChunk(
        "legacy-org-hint",
        "legacy-chunk",
        "Unclassified record with a stale organization hint.",
        source="legacy-source",
        organization_id="org-a",
    )
    global_document = DocumentChunk(
        "shared-document",
        "shared-chunk",
        "Approved global corpus text.",
        source="shared-source",
        scope="global",
        scope_approval_reference=GLOBAL_APPROVAL,
    )
    store.upsert([tenant_a, tenant_b, unknown], None)
    _insert_global_fixture(factory, global_document)
    with factory.begin() as session:
        session.add(
            RAGDocumentRecord(
                document_id=legacy_with_org_hint.document_id,
                chunk_id=legacy_with_org_hint.chunk_id,
                text=legacy_with_org_hint.text,
                document_type=legacy_with_org_hint.source_type,
                source=legacy_with_org_hint.source,
                version=legacy_with_org_hint.version,
                organization_id=legacy_with_org_hint.organization_id,
                scope="unclassified",
            )
        )

    tenant_a_results = store.documents(
        RetrievalFilters(organization_id="org-a", include_global=True)
    )
    assert {
        (document.organization_id, document.text) for document in tenant_a_results
    } == {
        ("org-a", "Organization A private corpus text."),
        (None, "Approved global corpus text."),
    }
    tenant_b_results = store.documents(
        RetrievalFilters(organization_id="org-b", include_global=True)
    )
    assert {
        (document.organization_id, document.text) for document in tenant_b_results
    } == {
        ("org-b", "Organization B private corpus text."),
        (None, "Approved global corpus text."),
    }
    assert [
        document.document_id
        for document in store.documents(RetrievalFilters(organization_id="org-a"))
    ] == ["shared-document"]
    assert {
        document.document_id
        for document in store.documents(
            RetrievalFilters(organization_id="org-a", include_global=True)
        )
    } == {"shared-document"}
    assert store.documents(RetrievalFilters()) == []
    with pytest.raises(ValueError, match="cannot be ingested with an organization hint"):
        store.upsert([legacy_with_org_hint], None)

    with factory() as session:
        legacy_row = session.query(RAGDocumentRecord).filter_by(
            document_id="legacy-org-hint"
        ).one()
        assert legacy_row.scope == "unclassified"
        assert legacy_row.organization_id == "org-a"
    engine.dispose()


def test_verified_global_visibility_requires_explicit_policy_and_approval(tmp_path):
    engine, factory, store = _vector_store(tmp_path)
    approved_global = DocumentChunk(
        "global-only",
        "chunk",
        "Verified global retrieval content.",
        source="global-source",
        scope="global",
        scope_approval_reference=GLOBAL_APPROVAL,
    )
    with pytest.raises(ValueError, match="require an approval reference"):
        DocumentChunk("unapproved-global", "chunk", "Not approved.", scope="global")

    with pytest.raises(ValueError, match="requires an approval reference"):
        store.upsert([approved_global], None)

    with pytest.raises(ValueError, match="pinned repository demo corpus"):
        store.upsert(
            [approved_global],
            None,
            global_approval_reference=GLOBAL_APPROVAL,
        )
    _insert_global_fixture(factory, approved_global)
    _insert_global_fixture(
        factory,
        DocumentChunk(
            "global-untrusted-reference",
            "chunk",
            "Global-looking content with an untrusted reference.",
            source="untrusted-global-source",
            scope="global",
            scope_approval_reference="external:unverified",
        ),
        approval_reference="external:unverified",
    )
    assert store.documents(RetrievalFilters()) == []
    assert [
        item.document_id
        for item in store.documents(RetrievalFilters(include_global=True))
    ] == ["global-only"]
    assert store.documents(
        RetrievalFilters(organization_id="org-a")
    ) == []
    assert [
        item.document_id
        for item in store.documents(
            RetrievalFilters(organization_id="org-a", include_global=True)
        )
    ] == ["global-only"]
    engine.dispose()


def test_tenant_upsert_identity_isolated_for_same_document_and_source_keys(tmp_path):
    engine, factory, store = _vector_store(tmp_path)
    document_a = DocumentChunk(
        "shared-document",
        "shared-chunk",
        "Tenant A original text.",
        source="shared-source",
        organization_id="org-a",
        scope="organization",
    )
    document_b = DocumentChunk(
        "shared-document",
        "shared-chunk",
        "Tenant B original text.",
        source="shared-source",
        organization_id="org-b",
        scope="organization",
    )
    store.upsert([document_b], None)
    store.upsert([document_a], None)
    replacement_a = DocumentChunk(
        "shared-document",
        "shared-chunk",
        "Tenant A replacement text.",
        source="shared-source",
        organization_id="org-a",
        scope="organization",
    )
    store.upsert([replacement_a], None)

    a_records = store.documents(RetrievalFilters(organization_id="org-a"))
    b_records = store.documents(RetrievalFilters(organization_id="org-b"))
    assert [document.text for document in a_records] == ["Tenant A replacement text."]
    assert [document.text for document in b_records] == ["Tenant B original text."]
    with factory() as session:
        assert session.query(func.count(RAGDocumentRecord.id)).scalar() == 2
    engine.dispose()


def test_replace_source_deactivates_only_the_requesting_organization(tmp_path):
    engine, _, store = _vector_store(tmp_path)
    tenant_a = DocumentChunk(
        "a-old",
        "chunk",
        "Tenant A old source content.",
        source="same-source",
        organization_id="org-a",
        scope="organization",
    )
    tenant_b = DocumentChunk(
        "b-old",
        "chunk",
        "Tenant B source content.",
        source="same-source",
        organization_id="org-b",
        scope="organization",
    )
    store.upsert([tenant_a, tenant_b], None)

    replacement_a = DocumentChunk(
        "a-new",
        "chunk",
        "Tenant A replacement source content.",
        source="same-source",
        organization_id="org-a",
        scope="organization",
    )
    store.upsert([replacement_a], None, replace_source=True)

    assert {
        document.document_id
        for document in store.documents(RetrievalFilters(organization_id="org-a"))
    } == {"a-new"}
    assert {
        document.document_id
        for document in store.documents(RetrievalFilters(organization_id="org-b"))
    } == {"b-old"}
    engine.dispose()


def test_conflicting_replacements_remain_isolated_between_organizations(tmp_path):
    engine, _, store = _vector_store(tmp_path)
    tenant_a = DocumentChunk(
        "shared-document",
        "shared-chunk",
        "Tenant A version one.",
        source="shared-source",
        organization_id="org-a",
        scope="organization",
    )
    tenant_b = DocumentChunk(
        "shared-document",
        "shared-chunk",
        "Tenant B version one.",
        source="shared-source",
        organization_id="org-b",
        scope="organization",
    )
    store.upsert([tenant_a, tenant_b], None)

    store.upsert(
        [
            DocumentChunk(
                "shared-document",
                "shared-chunk",
                "Tenant A version two.",
                source="shared-source",
                organization_id="org-a",
                scope="organization",
            )
        ],
        None,
        replace_source=True,
    )
    store.upsert(
        [
            DocumentChunk(
                "shared-document",
                "shared-chunk",
                "Tenant B version two.",
                source="shared-source",
                organization_id="org-b",
                scope="organization",
            )
        ],
        None,
        replace_source=True,
    )

    assert [
        document.text
        for document in store.documents(RetrievalFilters(organization_id="org-a"))
    ] == ["Tenant A version two."]
    assert [
        document.text
        for document in store.documents(RetrievalFilters(organization_id="org-b"))
    ] == ["Tenant B version two."]
    engine.dispose()


def test_repeated_tenant_ingestion_is_idempotent(tmp_path):
    engine, factory, store = _vector_store(tmp_path)
    document = DocumentChunk(
        "tenant-document",
        "chunk",
        "Tenant A corpus content.",
        source="tenant-source",
        organization_id="org-a",
        scope="organization",
    )

    store.upsert([document], None)
    store.upsert([document], None)

    with factory() as session:
        assert session.query(func.count(RAGDocumentRecord.id)).scalar() == 1
    engine.dispose()


def test_unscoped_retrieval_excludes_job_scoped_chunks():
    documents = [
        DocumentChunk(
            "ai-only",
            "chunk",
            "Redis cache invalidation prevents stale values.",
            job_id="ai_engineer",
        )
    ]

    result = HybridRetriever(documents).retrieve("Redis cache invalidation")

    assert result.chunks == []
    assert result.fallback_reason == "empty_filtered_corpus"


def test_provider_failure_keeps_lexical_results_and_labels_reduced_capability():
    class BrokenEmbedding:
        model = "broken"
        version = "broken-v1"
        dimension = 3

        def embed(self, text):
            assert text
            raise EmbeddingProviderError("provider unavailable")

    documents = [
        DocumentChunk(
            "redis",
            "cache",
            "Redis cache invalidation handles stale values.",
            scope="global",
            scope_approval_reference=GLOBAL_APPROVAL,
        )
    ]
    result = HybridRetriever(
        documents,
        dense=DenseRetriever(BrokenEmbedding()),
    ).retrieve(
        "Redis cache invalidation",
        RetrievalFilters(include_global=True),
    )

    assert result.chunks
    assert result.dense_count == 0
    assert result.fallback_reason == "embedding_provider_failed:EmbeddingProviderError"


def test_empty_corpus_returns_no_results_and_chunk_ids_are_document_scoped():
    empty = HybridRetriever().retrieve("database transactions")
    assert empty.chunks == []
    assert empty.lexical_count == 0
    assert empty.fallback_reason == "empty_filtered_corpus"

    documents = [
        DocumentChunk(
            "document-a",
            "shared-chunk",
            "Redis cache invalidation.",
            scope="global",
            scope_approval_reference=GLOBAL_APPROVAL,
        ),
        DocumentChunk(
            "document-b",
            "shared-chunk",
            "Redis cache consistency.",
            scope="global",
            scope_approval_reference=GLOBAL_APPROVAL,
        ),
    ]
    result = HybridRetriever(documents).retrieve(
        "Redis cache", RetrievalFilters(include_global=True)
    )
    assert len(result.chunks) == 2


def test_question_context_uses_answer_question_history_and_job_scope():
    class CapturingRetriever:
        def retrieve(self, query, filters=None, top_k=5):
            self.query = query
            self.filters = filters
            self.top_k = top_k
            return RetrievalResult(
                query,
                [],
                0,
                0,
                False,
                fallback_reason="semantic_embeddings_unconfigured_lexical_only",
            )

    retriever = CapturingRetriever()
    service = RAGQuestionService(retriever)
    result = service.retrieve_context(
        "AI Engineer",
        "python",
        ["cancellation"],
        question="How did you handle an async service failure?",
        answer_evidence=["I used bounded queues"],
        previous_questions=["Describe your event loop"],
        difficulty="hard",
    )

    assert "bounded queues" in retriever.query
    assert "async service failure" in retriever.query
    assert "Describe your event loop" in retriever.query
    assert retriever.filters.job_id == "ai_engineer"
    assert retriever.filters.competency == "python"
    assert retriever.top_k == settings.RETRIEVAL_TOP_K
    assert result.fallback_reason == "semantic_embeddings_unconfigured_lexical_only"
    assert "[LEXICAL_ONLY]" in service.format_context(result)


def test_demo_corpus_is_versioned_and_contains_requested_document_types():
    docs = load_demo_corpus()

    assert len(docs) >= 15
    assert {doc.source_type for doc in docs} >= {
        "interview_question",
        "competency_description",
        "evaluation_rubric",
        "technical_knowledge",
        "follow_up_guidance",
    }
    assert all(doc.document_id and doc.chunk_id and doc.version and doc.source for doc in docs)


def test_global_demo_corpus_must_match_the_pinned_digest(tmp_path, monkeypatch):
    import json

    from rag import corpus_loader

    altered_path = tmp_path / "demo_v1.json"
    payload = json.loads(corpus_loader.DEMO_CORPUS_PATH.read_text(encoding="utf-8"))
    payload["documents"][0]["text"] += " altered"
    altered_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(corpus_loader, "DEMO_CORPUS_PATH", altered_path)

    with pytest.raises(ValueError, match="approved digest"):
        corpus_loader.load_demo_corpus(altered_path)


def test_question_retrieval_reads_persisted_vectors_and_respects_job_scope(tmp_path):
    engine, _, store = _vector_store(tmp_path)
    provider = FakeSemanticEmbedding()
    documents = load_demo_corpus()
    store.upsert(
        documents,
        provider,
        replace_source=True,
        global_approval_reference=(
            "repository:rag/corpus/demo_v1.json#demo-v1"
        ),
    )
    service = RAGQuestionService(
        HybridRetriever(
            dense=DenseRetriever(provider),
            vector_store=store,
        )
    )

    result = service.retrieve_context(
        "AI Engineer",
        "python",
        ["cancellation", "backpressure"],
        question="How did you prevent overload?",
        answer_evidence=["I used bounded queues"],
        previous_questions=["Describe the event loop"],
        difficulty="medium",
    )
    context = service.format_context(result)

    assert result.chunks
    assert all(item.chunk.competency == "python" for item in result.chunks)
    assert all(item.chunk.job_id == "ai_engineer" for item in result.chunks)
    assert "UNTRUSTED_CONTEXT" in context
    assert all(item.chunk.job_id != "software_engineer" for item in result.chunks)

    from services.interview_service import InterviewService

    interview_service = InterviewService.__new__(InterviewService)
    interview_service.rag = service
    graph_context = interview_service._retrieve_graph_context({
        "position": "AI Engineer",
        "current_competency": "python",
        "identified_gaps": ["cancellation", "backpressure"],
        "current_question": "How did you prevent overload?",
        "candidate_answer": "I used bounded queues",
        "asked_questions": ["Describe the event loop"],
        "difficulty": "medium",
    })
    assert "UNTRUSTED_CONTEXT" in graph_context
    assert "RETRIEVAL_CAPABILITY" not in graph_context
    engine.dispose()


@pytest.mark.live
@pytest.mark.skipif(
    not settings.OPENAI_API_KEY or settings.EMBEDDING_PROVIDER.lower() != "openai",
    reason="live embedding smoke test requires OPENAI_API_KEY and EMBEDDING_PROVIDER=openai",
)
def test_live_openai_embedding_smoke():
    provider = OpenAIEmbeddingProvider()
    vector = provider.embed("A short retrieval smoke-test query.")
    assert len(vector) == settings.EMBEDDING_DIMENSION
