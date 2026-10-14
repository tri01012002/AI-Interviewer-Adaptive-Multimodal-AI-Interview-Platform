"""Explicit opt-in smoke checks for configured live OpenAI services."""

import logging
import math
import os
from time import perf_counter

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from config import settings

logger = logging.getLogger(__name__)


def _require_live_provider(provider_name: str) -> None:
    if os.getenv("RUN_LIVE_AI_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_AI_TESTS=1 to enable live provider requests.")
    if settings.LLM_PROVIDER != "openai" and provider_name == "LLM":
        pytest.skip("Set LLM_PROVIDER=openai to enable the live LLM smoke test.")
    if settings.EMBEDDING_PROVIDER != "openai" and provider_name == "embedding":
        pytest.skip("Set EMBEDDING_PROVIDER=openai to enable live embedding requests.")
    if not settings.OPENAI_API_KEY:
        pytest.skip("OPENAI_API_KEY is not configured; live provider validation is blocked.")


@pytest.mark.live
def test_live_llm_returns_a_valid_structured_evaluation(monkeypatch):
    _require_live_provider("LLM")

    from evaluation.service import LLMEvaluator, Rubric
    from integrations.llm_providers.provider import provider_from_settings

    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 0)
    monkeypatch.setattr(settings, "OPENAI_MAX_TOKENS", 900)
    provider = provider_from_settings(settings)
    assert provider is not None

    answer = (
        "I added bounded concurrency to the Python API and measured a 35 percent reduction "
        "in p95 latency during load testing."
    )
    started = perf_counter()
    result = LLMEvaluator(provider).evaluate(
        "How did you improve API responsiveness?",
        answer,
        "python",
        Rubric(competency="python"),
    )
    latency_ms = round((perf_counter() - started) * 1000, 2)

    assert result.assessment.evaluator_type == "llm"
    assert 0 <= result.assessment.score <= 5
    assert all(item.text in answer for item in result.evidence)
    assert 0.0 <= result.assessment.confidence <= 1.0
    assert result.assessment.next_action.value in {
        "DIG_DEEPER",
        "CHANGE_COMPETENCY",
        "INCREASE_DIFFICULTY",
        "DECREASE_DIFFICULTY",
        "FINISH",
    }
    logger.info(
        "live LLM smoke result",
        extra={
            "provider": provider.provider_name,
            "model": provider.model,
            "latency_ms": latency_ms,
            "status": "passed",
            "evaluation_schema": "StructuredAnswerEvaluation",
            "score": result.assessment.score,
            "confidence": result.assessment.confidence,
            "next_action": result.assessment.next_action.value,
            "evidence_count": len(result.evidence),
            "schema_validated": True,
        },
    )


@pytest.mark.live
def test_live_synthetic_evaluation_quality_cases(monkeypatch):
    _require_live_provider("LLM")

    from evaluation.service import LLMEvaluator, Rubric
    from integrations.llm_providers.provider import provider_from_settings

    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 0)
    monkeypatch.setattr(settings, "OPENAI_MAX_TOKENS", 900)
    provider = provider_from_settings(settings)
    assert provider is not None
    evaluator = LLMEvaluator(provider)
    cases = (
        (
            "strong_specific",
            "The candidate implemented a Python async worker, capped concurrency at 20, "
            "and measured p95 latency falling from 800 ms to 300 ms.",
            "Expect concrete ownership, design details, and a measured outcome.",
        ),
        (
            "weak_missing_evidence",
            "The candidate says they are good at Python but gives no example or result.",
            "Expect insufficient evidence and follow-up questions.",
        ),
        (
            "partial_rubric_coverage",
            "The candidate used asyncio for network requests but gives no cancellation, "
            "backpressure, failure handling, or measured outcome.",
            "Expect partial evidence and explicit missing trade-off or outcome details.",
        ),
        (
            "irrelevant",
            "The candidate describes organizing a weekend picnic and choosing snacks.",
            "Expect little or no evidence for Python concurrency.",
        ),
        (
            "competency_gap",
            "The candidate launched parallel requests but cannot explain overload control, "
            "timeouts, or what happens when a dependency fails.",
            "Expect the overload and failure-isolation gap to remain visible.",
        ),
        (
            "keyword_only",
            "Python, asyncio, Redis, Kafka, concurrency, cache, and backpressure.",
            "Expect keywords alone not to establish owned implementation or impact.",
        ),
    )
    actual = []
    for case_id, answer, expected in cases:
        started = perf_counter()
        result = evaluator.evaluate(
            "Describe a system you built to handle concurrent Python service requests.",
            answer,
            "python",
            Rubric(competency="python"),
        )
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        assert result.assessment.evaluator_type == "llm"
        assert 0 <= result.assessment.score <= 5
        assert 0.0 <= result.assessment.confidence <= 1.0
        assert all(item.text in answer for item in result.evidence)
        actual.append(
            {
                "case_id": case_id,
                "expected_behavior": expected,
                "score": result.assessment.score,
                "status": result.assessment.status.value,
                "confidence": result.assessment.confidence,
                "next_action": result.assessment.next_action.value,
                "evidence_count": len(result.evidence),
                "gaps": result.assessment.gaps,
                "schema_validated": True,
                "latency_ms": elapsed_ms,
            }
        )
    logger.info(
        "live synthetic evaluation quality cases",
        extra={
            "provider": provider.provider_name,
            "model": provider.model,
            "case_count": len(actual),
            "results": actual,
            "human_review": "not_performed",
        },
    )


@pytest.mark.live
def test_live_embedding_is_persisted_and_retrieves_demo_corpus(tmp_path, monkeypatch):
    _require_live_provider("embedding")

    from rag.corpus_loader import load_demo_corpus
    from rag.service import (
        RetrievalFilters,
        SQLAlchemyVectorStore,
        configured_embedding_provider,
    )
    from services.database import RAGDocumentRecord

    monkeypatch.setattr(settings, "EMBEDDING_TIMEOUT_SECONDS", 10.0)
    provider = configured_embedding_provider()
    assert provider is not None

    class CountingEmbeddingProvider:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.model = wrapped.model
            self.version = wrapped.version
            self.dimension = wrapped.dimension
            self.call_count = 0

        def embed(self, text):
            self.call_count += 1
            vector = self._wrapped.embed(text)
            assert len(vector) == self.dimension
            assert all(math.isfinite(float(value)) for value in vector)
            return vector

    counting_provider = CountingEmbeddingProvider(provider)
    documents = load_demo_corpus()
    document = next(
        chunk
        for chunk in documents
        if chunk.document_id == "python-async-io-knowledge"
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'live-embedding-smoke.db'}")
    RAGDocumentRecord.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = SQLAlchemyVectorStore(factory)

    started = perf_counter()
    assert store.upsert(
        documents,
        counting_provider,
        global_approval_reference=(
            "repository:rag/corpus/demo_v1.json#demo-v1"
        ),
    ) == len(documents)
    stored_at = perf_counter()
    results = store.retrieve(
        "Keep simultaneous web requests responsive without overloading dependent services.",
        filters=RetrievalFilters(
            competency="python",
            job_id="ai_engineer",
            include_global=True,
        ),
        top_k=3,
        embedding_provider=counting_provider,
    )
    latency_ms = round((perf_counter() - started) * 1000, 2)

    fresh_store = SQLAlchemyVectorStore(factory)
    fresh_results = fresh_store.retrieve(
        "Keep simultaneous web requests responsive without overloading dependent services.",
        filters=RetrievalFilters(
            competency="python",
            job_id="ai_engineer",
            include_global=True,
        ),
        top_k=3,
        embedding_provider=counting_provider,
    )
    unscoped_results = fresh_store.retrieve(
        "Keep simultaneous web requests responsive without overloading dependent services.",
        top_k=3,
        embedding_provider=counting_provider,
    )

    with factory() as session:
        stored = session.execute(
            select(RAGDocumentRecord).where(
                RAGDocumentRecord.document_id == document.document_id
            )
        ).scalar_one()
        assert stored.embedding_model == provider.model
        assert stored.embedding_dimension == provider.dimension
        assert stored.embedding_version == provider.version
        assert stored.embedding_json is not None

    assert results
    assert results[0].chunk.document_id == document.document_id
    assert results[0].dense_score > 0
    assert fresh_results[0].chunk.document_id == document.document_id
    assert fresh_results[0].dense_score > 0
    assert not unscoped_results
    assert counting_provider.call_count == len(documents) + 2
    logger.info(
        "live embedding smoke result",
        extra={
            "provider": provider.__class__.__name__,
            "model": provider.model,
            "dimension": provider.dimension,
            "embedding_persist_ms": round((stored_at - started) * 1000, 2),
            "query_and_retrieval_ms": round((perf_counter() - stored_at) * 1000, 2),
            "latency_ms": latency_ms,
            "retrieved_document_id": results[0].chunk.document_id,
            "fresh_session_retrieved_document_id": fresh_results[0].chunk.document_id,
            "unscoped_job_document_excluded": all(
                item.chunk.document_id != document.document_id for item in unscoped_results
            ),
            "embedding_api_call_count": counting_provider.call_count,
            "status": "passed",
        },
    )
    engine.dispose()
