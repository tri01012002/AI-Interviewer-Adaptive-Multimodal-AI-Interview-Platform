"""Local hybrid retrieval pipeline for interview knowledge and question guidance."""

from __future__ import annotations

import hashlib
import math
import re
import logging
from time import perf_counter
from dataclasses import dataclass, field
from typing import Protocol

from rank_bm25 import BM25Okapi


@dataclass(frozen=True)
class DocumentChunk:
    document_id: str
    chunk_id: str
    text: str
    competency: str | None = None
    job_role: str | None = None
    source_type: str = "question_guidance"
    version: str = "v1"
    language: str = "en"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalFilters:
    competency: str | None = None
    job_role: str | None = None
    source_type: str | None = None
    version: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: DocumentChunk
    score: float
    dense_score: float = 0.0
    lexical_score: float = 0.0
    rank: int = 0


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    chunks: list[RetrievedChunk]
    dense_count: int
    lexical_count: int
    reranked: bool
    retrieval_version: str = "hybrid-v1"
    embedding_version: str = "deterministic-v1"
    reranker_version: str = "term-coverage-v1"
    fallback_reason: str | None = None


class EmbeddingProvider(Protocol):
    dimension: int

    def embed(self, text: str) -> list[float]:
        ...


class HashEmbeddingProvider:
    """Deterministic dense test embedding; replace with a hosted model in production."""

    dimension = 128

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in _tokens(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0 if digest[4] % 2 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


class DenseRetriever:
    def __init__(self, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.embedding_provider = embedding_provider or HashEmbeddingProvider()

    def retrieve(
        self, query: str, documents: list[DocumentChunk], filters: RetrievalFilters, top_k: int
    ) -> list[RetrievedChunk]:
        filtered = [document for document in documents if _matches(document, filters)]
        query_vector = self.embedding_provider.embed(query)
        scored = []
        for document in filtered:
            score = _dot(query_vector, self.embedding_provider.embed(document.text))
            scored.append(RetrievedChunk(document, score, dense_score=score))
        return sorted(scored, key=lambda item: item.score, reverse=True)[:top_k]


class BM25Retriever:
    def retrieve(
        self, query: str, documents: list[DocumentChunk], filters: RetrievalFilters, top_k: int
    ) -> list[RetrievedChunk]:
        filtered = [document for document in documents if _matches(document, filters)]
        if not filtered:
            return []
        index = BM25Okapi([_tokens(document.text) for document in filtered])
        scores = index.get_scores(_tokens(query))
        ranked = sorted(
            ((document, score) for document, score in zip(filtered, scores) if score != 0),
            key=lambda item: item[1],
            reverse=True,
        )
        return [RetrievedChunk(document, float(score), lexical_score=float(score)) for document, score in ranked[:top_k]]


class Reranker(Protocol):
    name: str

    def rerank(self, query: str, candidates: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        ...


class DeterministicReranker:
    """Actual candidate reorder using query-term coverage, not list truncation."""

    name = "term-coverage-v1"

    def rerank(self, query: str, candidates: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        query_terms = set(_tokens(query))
        rescored = []
        for candidate in candidates:
            terms = set(_tokens(candidate.chunk.text))
            coverage = len(query_terms & terms) / max(len(query_terms), 1)
            score = (0.7 * coverage) + (0.3 * max(candidate.dense_score, candidate.lexical_score, 0.0))
            rescored.append(RetrievedChunk(candidate.chunk, score, candidate.dense_score, candidate.lexical_score))
        return [item for item in sorted(rescored, key=lambda item: item.score, reverse=True)[:top_k]]


@dataclass
class HybridRetriever:
    documents: list[DocumentChunk] = field(default_factory=list)
    dense: DenseRetriever = field(default_factory=DenseRetriever)
    lexical: BM25Retriever = field(default_factory=BM25Retriever)
    reranker: Reranker = field(default_factory=DeterministicReranker)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__), repr=False)

    def retrieve(
        self, query: str, filters: RetrievalFilters | None = None, top_k: int = 5
    ) -> RetrievalResult:
        started = perf_counter()
        if not query.strip() or top_k <= 0:
            return RetrievalResult(query, [], 0, 0, False, fallback_reason="empty_query_or_invalid_top_k")
        active_filters = filters or RetrievalFilters()
        dense_results = self.dense.retrieve(query, self.documents, active_filters, top_k)
        lexical_results = self.lexical.retrieve(query, self.documents, active_filters, top_k)
        candidates: dict[str, RetrievedChunk] = {}
        for rank, candidate in enumerate(dense_results, start=1):
            candidates[candidate.chunk.chunk_id] = RetrievedChunk(
                candidate.chunk, 1 / (60 + rank), candidate.dense_score, candidate.lexical_score, rank
            )
        for rank, candidate in enumerate(lexical_results, start=1):
            previous = candidates.get(candidate.chunk.chunk_id)
            rrf_score = 1 / (60 + rank)
            candidates[candidate.chunk.chunk_id] = RetrievedChunk(
                candidate.chunk,
                (previous.score if previous else 0.0) + rrf_score,
                previous.dense_score if previous else 0.0,
                candidate.lexical_score,
                previous.rank if previous else rank,
            )
        try:
            reranked = self.reranker.rerank(query, list(candidates.values()), top_k)
            reranked_used = True
            fallback_reason = None
        except Exception:
            reranked = sorted(candidates.values(), key=lambda item: item.score, reverse=True)[:top_k]
            reranked_used = False
            fallback_reason = "reranker_failed"
        result = RetrievalResult(query, reranked, len(dense_results), len(lexical_results), reranked_used, fallback_reason=fallback_reason)
        self.logger.info(
            "retrieval completed",
            extra={
                "retrieval_strategy": "hybrid_rrf",
                "candidate_count": len(candidates),
                "selected_count": len(reranked),
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "reranker_used": reranked_used,
                "fallback_reason": fallback_reason,
                "retrieval_version": result.retrieval_version,
                "embedding_version": result.embedding_version,
                "reranker_version": result.reranker_version,
            },
        )
        return result


class RAGQuestionService:
    """Compatibility facade backed by hybrid retrieval over attributed chunks."""

    def __init__(self, retriever: HybridRetriever | None = None) -> None:
        self.retriever = retriever or HybridRetriever(_default_documents())
        self.max_chunks = 3
        self.max_context_chars = 1200

    @staticmethod
    def build_gap_query(position: str, competency: str | None, gaps: list[str] | None) -> str:
        gap_text = " ".join(gaps or ["interview evidence"])
        return f"{position} {competency or 'general'} {gap_text}".strip()

    def retrieve_context(self, position: str, competency: str | None, gaps: list[str] | None) -> RetrievalResult:
        query = self.build_gap_query(position, competency, gaps)
        try:
            return self.retriever.retrieve(query, top_k=self.max_chunks)
        except Exception:
            return RetrievalResult(query, [], 0, 0, False, fallback_reason="retrieval_failed")

    def format_context(self, result: RetrievalResult) -> str:
        parts = []
        remaining = self.max_context_chars
        for item in result.chunks:
            attribution = f"[UNTRUSTED_CONTEXT source={item.chunk.document_id} chunk={item.chunk.chunk_id}] "
            text = (attribution + item.chunk.text)[:remaining]
            parts.append(text)
            remaining -= len(text)
            if remaining <= 0:
                break
        return "\n".join(parts)

    def retrieve_relevant_questions(self, position: str, skills: dict[str, object]) -> list[str]:
        competency = next(iter(skills), None)
        gaps = skills.get(competency, {}).get("gaps", []) if competency and isinstance(skills.get(competency), dict) else []
        try:
            result = self.retriever.retrieve(self.build_gap_query(position, competency, gaps), RetrievalFilters(job_role=position.lower() or None), top_k=4)
        except Exception:
            return []
        return [item.chunk.text for item in result.chunks]

    def generate_follow_up_question(self, position: str, answer: str, skills: dict[str, object]) -> str:
        competency = next(iter(skills), None)
        gaps = skills.get(competency, {}).get("gaps", []) if competency and isinstance(skills.get(competency), dict) else []
        result = self.retrieve_context(position, competency, gaps)
        return result.chunks[0].chunk.text if result.chunks else "Please provide a concrete example and measurable result."


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _matches(document: DocumentChunk, filters: RetrievalFilters) -> bool:
    return all(
        expected is None or actual == expected
        for actual, expected in (
            (document.competency, filters.competency),
            (document.job_role, filters.job_role),
            (document.source_type, filters.source_type),
            (document.version, filters.version),
            (document.language, filters.language),
        )
    )


def _default_documents() -> list[DocumentChunk]:
    return [
        DocumentChunk("ai-engineer", "ai-q1", "Tell me about a model you deployed to production and how you evaluated it.", job_role="ai engineer"),
        DocumentChunk("ai-engineer", "ai-q2", "How did you optimize model inference latency for real users?", job_role="ai engineer"),
        DocumentChunk("ai-engineer", "ai-q3", "Describe a trade-off you made between accuracy and performance.", job_role="ai engineer"),
        DocumentChunk("data-scientist", "ds-q1", "What experiment design did you use for model validation and monitoring?", job_role="data scientist"),
        DocumentChunk("software-engineer", "se-q1", "Walk me through a production incident you debugged end-to-end.", job_role="software engineer"),
        DocumentChunk("distributed-systems", "redis-rubric", "Redis caching evidence should include invalidation, consistency, and failure handling.", competency="distributed_systems", source_type="rubric"),
    ]
