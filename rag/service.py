"""Local hybrid retrieval pipeline for interview knowledge and question guidance."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal, Protocol

from rank_bm25 import BM25Okapi

from config import settings

DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE = (
    "repository:rag/corpus/demo_v1.json#demo-v1"
)


CorpusScope = Literal["organization", "global", "unclassified"]


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
    difficulty: str | None = None
    job_id: str | None = None
    organization_id: str | None = None
    source: str = "demo-corpus"
    active: bool = True
    scope: CorpusScope = "unclassified"
    scope_approval_reference: str | None = None

    def __post_init__(self) -> None:
        if self.scope == "organization":
            if (
                not isinstance(self.organization_id, str)
                or not self.organization_id.strip()
            ):
                raise ValueError("organization-scoped corpus chunks require an organization")
            if self.scope_approval_reference is not None:
                raise ValueError("organization-scoped corpus chunks cannot use global approval")
        elif self.scope == "global":
            if self.organization_id is not None:
                raise ValueError("global corpus chunks cannot belong to an organization")
            if (
                not isinstance(self.scope_approval_reference, str)
                or not self.scope_approval_reference.strip()
            ):
                raise ValueError("global corpus chunks require an approval reference")
        elif self.scope == "unclassified":
            if self.scope_approval_reference is not None:
                raise ValueError("unclassified corpus chunks cannot carry an approved scope")
        else:
            raise ValueError("invalid corpus scope")


@dataclass(frozen=True)
class RetrievalFilters:
    competency: str | None = None
    job_role: str | None = None
    source_type: str | None = None
    version: str | None = None
    language: str | None = None
    difficulty: str | None = None
    job_id: str | None = None
    organization_id: str | None = None
    include_global: bool = False
    active: bool = True


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
    retrieval_version: str = "hybrid-rrf-v2"
    embedding_version: str = "not_configured"
    reranker_version: str = "term-coverage-v1"
    fallback_reason: str | None = None


class EmbeddingProvider(Protocol):
    dimension: int
    model: str
    version: str

    def embed(self, text: str) -> list[float]:
        ...


class EmbeddingProviderError(RuntimeError):
    """An embedding provider failed or returned a vector outside its contract."""


class HashEmbeddingProvider:
    """Deterministic test-only embedding. Never selected by application configuration."""

    dimension = 128
    model = "hash-test-only"
    version = "test-only-v1"

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in _tokens(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0 if digest[4] % 2 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


class OpenAIEmbeddingProvider:
    """OpenAI text embeddings with bounded network timeout and strict vector checks."""

    def __init__(self, client: Any | None = None) -> None:
        if not settings.OPENAI_API_KEY.strip() and client is None:
            raise EmbeddingProviderError("OpenAI embedding credentials are not configured")
        if settings.EMBEDDING_PROVIDER.lower() != "openai":
            raise EmbeddingProviderError("configured embedding provider is not OpenAI")
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url=settings.OPENAI_BASE_URL,
                timeout=settings.EMBEDDING_TIMEOUT_SECONDS,
                max_retries=0,
            )
        self._client = client
        self.model = settings.EMBEDDING_MODEL
        self.dimension = settings.EMBEDDING_DIMENSION
        self.version = f"{self.model}:{self.dimension}"

    def embed(self, text: str) -> list[float]:
        if not text.strip():
            raise EmbeddingProviderError("cannot embed empty text")
        try:
            response = self._client.embeddings.create(
                model=self.model,
                input=text,
                dimensions=self.dimension,
            )
            vector = list(response.data[0].embedding)
        except Exception as exc:
            raise EmbeddingProviderError(f"embedding request failed: {exc.__class__.__name__}") from exc
        return _validate_vector(vector, self.dimension)


def configured_embedding_provider() -> EmbeddingProvider | None:
    provider_name = settings.EMBEDDING_PROVIDER.lower()
    if provider_name == "none":
        return None
    if provider_name != "openai":
        raise EmbeddingProviderError(f"unsupported embedding provider: {provider_name}")
    if not settings.OPENAI_API_KEY.strip():
        raise EmbeddingProviderError(
            "EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY"
        )
    return OpenAIEmbeddingProvider()


class DenseRetriever:
    def __init__(self, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.embedding_provider = embedding_provider
        self._cache: dict[str, list[float]] = {}

    def _embed(self, text: str) -> list[float]:
        if self.embedding_provider is None:
            raise EmbeddingProviderError("semantic embedding provider is not configured")
        if text not in self._cache:
            self._cache[text] = _validate_vector(
                self.embedding_provider.embed(text), self.embedding_provider.dimension
            )
        return self._cache[text]

    def retrieve(
        self, query: str, documents: list[DocumentChunk], filters: RetrievalFilters, top_k: int
    ) -> list[RetrievedChunk]:
        if self.embedding_provider is None:
            raise EmbeddingProviderError("semantic embedding provider is not configured")
        filtered = [document for document in documents if _matches(document, filters)]
        if not filtered:
            return []
        query_vector = self._embed(query)
        scored = []
        for document in filtered:
            document_vector = self._embed(document.text)
            score = _cosine(query_vector, document_vector)
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
            ((document, score) for document, score in zip(filtered, scores, strict=True) if score != 0),
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
        return sorted(rescored, key=lambda item: item.score, reverse=True)[:top_k]


def _validate_corpus_ingestion_scope(
    documents: list[DocumentChunk],
    global_approval_reference: str | None,
) -> None:
    if any(
        document.scope == "unclassified"
        and document.organization_id is not None
        for document in documents
    ):
        raise ValueError(
            "unclassified corpus chunks cannot be ingested with an organization hint"
        )

    has_global_documents = any(document.scope == "global" for document in documents)
    if not has_global_documents:
        if global_approval_reference is not None:
            raise ValueError(
                "global corpus approval reference supplied without global documents"
            )
        return

    if (
        not isinstance(global_approval_reference, str)
        or not global_approval_reference.strip()
    ):
        raise ValueError("global corpus ingestion requires an approval reference")
    if global_approval_reference != DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE:
        raise ValueError(
            "global ingestion is restricted to the pinned repository demo corpus"
        )
    if any(
        document.scope == "global"
        and document.scope_approval_reference != global_approval_reference
        for document in documents
    ):
        raise ValueError("global corpus approval reference does not match its documents")

    from rag.corpus_loader import load_demo_corpus

    if documents != load_demo_corpus():
        raise ValueError(
            "global ingestion must use the exact pinned repository demo corpus"
        )


class SQLAlchemyVectorStore:
    """Portable durable vectors with exact cosine scan for the small MVP corpus."""

    def __init__(self, session_factory: Any | None = None) -> None:
        configured_store = settings.VECTOR_STORE_PROVIDER.lower()
        if configured_store not in {"sqlalchemy_json", "supabase"}:
            raise ValueError(f"unsupported vector store provider: {configured_store}")
        if configured_store == "supabase":
            logging.getLogger(__name__).warning(
                "VECTOR_STORE_PROVIDER=supabase is a legacy placeholder; using the configured "
                "SQLAlchemy database with durable JSON vectors and exact cosine scan"
            )
        if session_factory is None:
            from services.database import SessionLocal

            session_factory = SessionLocal
        self._session_factory = session_factory

    def upsert(
        self,
        documents: list[DocumentChunk],
        embedding_provider: EmbeddingProvider | None,
        *,
        replace_source: bool = False,
        global_approval_reference: str | None = None,
    ) -> int:
        from services.database import RAGDocumentRecord

        _validate_corpus_ingestion_scope(documents, global_approval_reference)

        indexed = 0
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            if replace_source:
                source_scopes = {
                    (document.scope, document.organization_id, document.source)
                    for document in documents
                }
                for scope, organization_id, source in source_scopes:
                    replacement_query = session.query(RAGDocumentRecord).filter(
                        RAGDocumentRecord.scope == scope,
                        RAGDocumentRecord.source == source,
                    )
                    if organization_id is None:
                        replacement_query = replacement_query.filter(
                            RAGDocumentRecord.organization_id.is_(None)
                        )
                    else:
                        replacement_query = replacement_query.filter(
                            RAGDocumentRecord.organization_id == organization_id
                        )
                    replacement_query.update(
                        {"active": False}, synchronize_session=False
                    )
            for document in documents:
                existing = (
                    session.query(RAGDocumentRecord)
                    .filter_by(
                        scope=document.scope,
                        organization_id=document.organization_id,
                        document_id=document.document_id,
                        chunk_id=document.chunk_id,
                        version=document.version,
                    )
                    .one_or_none()
                )
                embedding = None
                model = dimension = embedding_version = None
                unchanged_embedding = (
                    existing is not None
                    and existing.text == document.text
                    and existing.embedding_json is not None
                )
                current_embedding = (
                    unchanged_embedding
                    and embedding_provider is not None
                    and existing.embedding_model == embedding_provider.model
                    and existing.embedding_dimension == embedding_provider.dimension
                    and existing.embedding_version == embedding_provider.version
                )
                if current_embedding:
                    embedding = existing.embedding_json
                    model = existing.embedding_model
                    dimension = existing.embedding_dimension
                    embedding_version = existing.embedding_version
                elif embedding_provider is not None:
                    vector = _validate_vector(
                        embedding_provider.embed(document.text), embedding_provider.dimension
                    )
                    embedding = json.dumps(vector, separators=(",", ":"))
                    model = embedding_provider.model
                    dimension = embedding_provider.dimension
                    embedding_version = embedding_provider.version
                elif unchanged_embedding:
                    embedding = existing.embedding_json
                    model = existing.embedding_model
                    dimension = existing.embedding_dimension
                    embedding_version = existing.embedding_version
                values = {
                    "text": document.text,
                    "document_type": document.source_type,
                    "competency_id": document.competency,
                    "job_id": document.job_id or _scope_id(document.job_role) or None,
                    "organization_id": document.organization_id,
                    "scope": document.scope,
                    "scope_approval_reference": (
                        global_approval_reference
                        if document.scope == "global"
                        else None
                    ),
                    "difficulty": document.difficulty,
                    "source": document.source,
                    "active": document.active,
                    "language": document.language,
                    "tags_json": json.dumps(document.tags),
                    "embedding_json": embedding,
                    "embedding_model": model,
                    "embedding_dimension": dimension,
                    "embedding_version": embedding_version,
                    "updated_at": now,
                }
                if existing is None:
                    session.add(
                        RAGDocumentRecord(
                            document_id=document.document_id,
                            chunk_id=document.chunk_id,
                            version=document.version,
                            created_at=now,
                            **values,
                        )
                    )
                else:
                    for key, value in values.items():
                        setattr(existing, key, value)
                indexed += int(embedding is not None)
        return indexed

    def documents(self, filters: RetrievalFilters) -> list[DocumentChunk]:
        from services.database import RAGDocumentRecord

        with self._session_factory() as session:
            rows = self._filtered_query(session.query(RAGDocumentRecord), RAGDocumentRecord, filters).all()
            return [self._to_chunk(row) for row in rows]

    def retrieve(
        self,
        query: str,
        filters: RetrievalFilters,
        top_k: int,
        embedding_provider: EmbeddingProvider,
    ) -> list[RetrievedChunk]:
        from services.database import RAGDocumentRecord

        with self._session_factory() as session:
            rows = (
                self._filtered_query(session.query(RAGDocumentRecord), RAGDocumentRecord, filters)
                .filter(
                    RAGDocumentRecord.embedding_model == embedding_provider.model,
                    RAGDocumentRecord.embedding_dimension == embedding_provider.dimension,
                    RAGDocumentRecord.embedding_version == embedding_provider.version,
                    RAGDocumentRecord.embedding_json.is_not(None),
                )
                .all()
            )
        if not rows:
            return []
        query_vector = _validate_vector(
            embedding_provider.embed(query), embedding_provider.dimension
        )
        scored: list[RetrievedChunk] = []
        for row in rows:
            try:
                vector = _validate_vector(json.loads(row.embedding_json), embedding_provider.dimension)
            except (json.JSONDecodeError, TypeError, EmbeddingProviderError) as exc:
                raise EmbeddingProviderError(
                    f"stored vector is invalid for {row.document_id}:{row.chunk_id}"
                ) from exc
            score = _cosine(query_vector, vector)
            scored.append(RetrievedChunk(self._to_chunk(row), score, dense_score=score))
        return sorted(scored, key=lambda item: item.dense_score, reverse=True)[:top_k]

    @staticmethod
    def _filtered_query(query: Any, model: Any, filters: RetrievalFilters) -> Any:
        query = query.filter(model.active.is_(filters.active))
        job_id = filters.job_id or (_scope_id(filters.job_role) if filters.job_role else None)
        for column, value, allow_global in (
            (model.competency_id, filters.competency, True),
            (model.document_type, filters.source_type, False),
            (model.version, filters.version, False),
            (model.language, filters.language, False),
            (model.difficulty, filters.difficulty, True),
        ):
            if value is not None:
                if allow_global:
                    query = query.filter((column == value) | column.is_(None))
                else:
                    query = query.filter(column == value)
        if job_id is None:
            query = query.filter(model.job_id.is_(None))
        else:
            query = query.filter((model.job_id == job_id) | model.job_id.is_(None))
        if filters.organization_id is None:
            if filters.include_global:
                query = query.filter(
                    model.scope == "global",
                    model.organization_id.is_(None),
                    model.scope_approval_reference
                    == DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE,
                )
            else:
                query = query.filter(False)
        else:
            scope_filter = (
                (model.scope == "organization")
                & (model.organization_id == filters.organization_id)
            )
            if filters.include_global:
                scope_filter = scope_filter | (
                    (model.scope == "global")
                    & model.organization_id.is_(None)
                    & (
                        model.scope_approval_reference
                        == DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE
                    )
                )
            query = query.filter(scope_filter)
        return query

    @staticmethod
    def _to_chunk(row: Any) -> DocumentChunk:
        return DocumentChunk(
            document_id=row.document_id,
            chunk_id=row.chunk_id,
            text=row.text,
            competency=row.competency_id,
            job_role=row.job_id,
            source_type=row.document_type,
            version=row.version,
            language=row.language,
            tags=tuple(json.loads(row.tags_json)),
            difficulty=row.difficulty,
            job_id=row.job_id,
            organization_id=row.organization_id,
            scope=row.scope,
            scope_approval_reference=row.scope_approval_reference,
            source=row.source,
            active=row.active,
        )


@dataclass
class HybridRetriever:
    documents: list[DocumentChunk] = field(default_factory=list)
    dense: DenseRetriever = field(default_factory=DenseRetriever)
    lexical: BM25Retriever = field(default_factory=BM25Retriever)
    reranker: Reranker = field(default_factory=DeterministicReranker)
    vector_store: SQLAlchemyVectorStore | None = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__), repr=False)

    def retrieve(
        self, query: str, filters: RetrievalFilters | None = None, top_k: int = 5
    ) -> RetrievalResult:
        started = perf_counter()
        if not query.strip() or top_k <= 0:
            return RetrievalResult(query, [], 0, 0, False, fallback_reason="empty_query_or_invalid_top_k")
        active_filters = filters or RetrievalFilters()
        documents = (
            self.vector_store.documents(active_filters)
            if self.vector_store is not None
            else [document for document in self.documents if _matches(document, active_filters)]
        )
        embedding_provider = self.dense.embedding_provider
        fallback_reason = None
        dense_results: list[RetrievedChunk] = []
        if embedding_provider is not None:
            try:
                dense_results = (
                    self.vector_store.retrieve(query, active_filters, top_k, embedding_provider)
                    if self.vector_store is not None
                    else self.dense.retrieve(query, documents, active_filters, top_k)
                )
                if self.vector_store is not None and not dense_results:
                    fallback_reason = "no_matching_semantic_vectors"
            except Exception as exc:
                self.logger.exception(
                    "semantic retrieval failed; continuing with lexical results",
                    extra={"failure_type": exc.__class__.__name__},
                )
                fallback_reason = f"embedding_provider_failed:{exc.__class__.__name__}"
        else:
            fallback_reason = "semantic_embeddings_unconfigured_lexical_only"
        lexical_results = self.lexical.retrieve(query, documents, active_filters, top_k)
        if not documents:
            fallback_reason = "empty_filtered_corpus"
        candidates: dict[tuple[CorpusScope, str | None, str, str], RetrievedChunk] = {}
        for rank, candidate in enumerate(dense_results, start=1):
            identity = _chunk_identity(candidate.chunk)
            candidates[identity] = RetrievedChunk(
                candidate.chunk, 1 / (60 + rank), candidate.dense_score, candidate.lexical_score, rank
            )
        for rank, candidate in enumerate(lexical_results, start=1):
            identity = _chunk_identity(candidate.chunk)
            previous = candidates.get(identity)
            rrf_score = 1 / (60 + rank)
            candidates[identity] = RetrievedChunk(
                candidate.chunk,
                (previous.score if previous else 0.0) + rrf_score,
                previous.dense_score if previous else 0.0,
                candidate.lexical_score,
                previous.rank if previous else rank,
            )
        try:
            reranked = self.reranker.rerank(query, list(candidates.values()), top_k)
            reranked_used = True
        except Exception as exc:
            self.logger.exception(
                "reranking failed; preserving reciprocal-rank-fusion order",
                extra={"failure_type": exc.__class__.__name__},
            )
            reranked = sorted(candidates.values(), key=lambda item: item.score, reverse=True)[:top_k]
            reranked_used = False
            fallback_reason = "reranker_failed" if fallback_reason is None else f"{fallback_reason};reranker_failed"
        result = RetrievalResult(
            query,
            reranked,
            len(dense_results),
            len(lexical_results),
            reranked_used,
            embedding_version=(
                embedding_provider.version if embedding_provider is not None else "not_configured"
            ),
            fallback_reason=fallback_reason,
        )
        self.logger.info(
            "retrieval completed",
            extra={
                "retrieval_strategy": "hybrid_rrf" if dense_results else "lexical_only",
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


class RetrievalEngine(Protocol):
    def retrieve(
        self,
        query: str,
        filters: RetrievalFilters | None = None,
        top_k: int = 5,
    ) -> RetrievalResult:
        ...


class RAGQuestionService:
    """Compatibility facade backed by hybrid retrieval over attributed chunks."""

    def __init__(self, retriever: RetrievalEngine | None = None) -> None:
        self.retriever = retriever or HybridRetriever(
            dense=DenseRetriever(configured_embedding_provider()),
            vector_store=SQLAlchemyVectorStore(),
        )
        self.max_chunks = settings.RETRIEVAL_TOP_K
        self.max_context_chars = 1200

    @staticmethod
    def build_gap_query(position: str, competency: str | None, gaps: list[str] | None) -> str:
        gap_text = " ".join(gaps or ["interview evidence"])
        return f"{position} {competency or 'general'} {gap_text}".strip()

    def retrieve_context(
        self,
        position: str,
        competency: str | None,
        gaps: list[str] | None,
        *,
        question: str | None = None,
        answer_evidence: list[str] | None = None,
        previous_questions: list[str] | None = None,
        difficulty: str | None = None,
        job_id: str | None = None,
        organization_id: str | None = None,
    ) -> RetrievalResult:
        query = " ".join(
            item
            for item in (
                self.build_gap_query(position, competency, gaps),
                question or "",
                " ".join(answer_evidence or []),
                " ".join(previous_questions or []),
                difficulty or "",
            )
            if item
        )
        try:
            scope_id = job_id or _scope_id(position)
            return self.retriever.retrieve(
                query,
                RetrievalFilters(
                    competency=competency,
                    job_id=scope_id or None,
                    organization_id=organization_id,
                    difficulty=difficulty,
                    include_global=True,
                ),
                top_k=self.max_chunks,
            )
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "RAG question context retrieval failed",
                extra={"failure_type": exc.__class__.__name__},
            )
            return RetrievalResult(
                query, [], 0, 0, False, fallback_reason=f"retrieval_failed:{exc.__class__.__name__}"
            )

    def format_context(self, result: RetrievalResult) -> str:
        parts = []
        lexical_only = result.fallback_reason is not None and (
            result.fallback_reason.startswith("semantic_embeddings_unconfigured")
            or result.fallback_reason.startswith("no_matching_semantic_vectors")
            or result.fallback_reason.startswith("embedding_provider_failed")
        )
        capability = "[LEXICAL_ONLY]\n" if lexical_only else (
            "[RETRIEVAL_DEGRADED]\n" if result.fallback_reason is not None else ""
        )
        remaining = max(0, self.max_context_chars - len(capability))
        for item in result.chunks:
            attribution = f"[UNTRUSTED_CONTEXT source={item.chunk.document_id} chunk={item.chunk.chunk_id}] "
            text = (attribution + item.chunk.text)[:remaining]
            parts.append(text)
            remaining -= len(text)
            if remaining <= 0:
                break
        return capability + "\n".join(parts)

    def retrieve_relevant_questions(self, position: str, skills: dict[str, object]) -> list[str]:
        competency = next(iter(skills), None)
        gaps = skills.get(competency, {}).get("gaps", []) if competency and isinstance(skills.get(competency), dict) else []
        try:
            result = self.retriever.retrieve(
                self.build_gap_query(position, competency, gaps),
                RetrievalFilters(
                    competency=competency,
                    job_id=_scope_id(position) or None,
                    source_type="interview_question",
                    include_global=True,
                ),
                top_k=4,
            )
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "RAG relevant-question retrieval failed",
                extra={"failure_type": exc.__class__.__name__},
            )
            return []
        return [item.chunk.text for item in result.chunks]

    def generate_follow_up_question(self, position: str, answer: str, skills: dict[str, object]) -> str:
        competency = next(iter(skills), None)
        gaps = skills.get(competency, {}).get("gaps", []) if competency and isinstance(skills.get(competency), dict) else []
        result = self.retrieve_context(
            position,
            competency,
            gaps,
            answer_evidence=[answer],
        )
        return result.chunks[0].chunk.text if result.chunks else "Please provide a concrete example and measurable result."


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def _scope_id(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise EmbeddingProviderError("embedding vector dimensions do not match")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise EmbeddingProviderError("embedding vector has zero magnitude")
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _validate_vector(vector: list[float], expected_dimension: int) -> list[float]:
    if len(vector) != expected_dimension:
        raise EmbeddingProviderError(
            f"embedding dimension mismatch: expected {expected_dimension}, received {len(vector)}"
        )
    if any(not math.isfinite(value) for value in vector):
        raise EmbeddingProviderError("embedding contains a non-finite value")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise EmbeddingProviderError("embedding vector has zero magnitude")
    return vector


def _matches(document: DocumentChunk, filters: RetrievalFilters) -> bool:
    if document.active != filters.active:
        return False
    if not _matches_job_scope(document, filters):
        return False
    if filters.difficulty is not None and document.difficulty not in (None, filters.difficulty):
        return False
    if filters.organization_id is None:
        if not filters.include_global or not _is_verified_global(document):
            return False
    elif not (
        document.scope == "organization"
        and document.organization_id == filters.organization_id
        or (filters.include_global and _is_verified_global(document))
    ):
        return False
    return all(
        expected is None or actual == expected
        for actual, expected in (
            (document.competency, filters.competency),
            (document.source_type, filters.source_type),
            (document.version, filters.version),
            (document.language, filters.language),
        )
    )


def _is_verified_global(document: DocumentChunk) -> bool:
    return (
        document.scope == "global"
        and document.organization_id is None
        and document.scope_approval_reference
        == DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE
    )


def _chunk_identity(
    document: DocumentChunk,
) -> tuple[CorpusScope, str | None, str, str]:
    return (
        document.scope,
        document.organization_id,
        document.document_id,
        document.chunk_id,
    )


def _matches_job_scope(document: DocumentChunk, filters: RetrievalFilters) -> bool:
    if filters.job_id is not None:
        actual_job_id = document.job_id or (_scope_id(document.job_role) or None)
        return actual_job_id is None or actual_job_id == filters.job_id
    if filters.job_role is not None:
        actual_job_id = _scope_id(document.job_id or document.job_role) or None
        return (
            actual_job_id is None
            or actual_job_id == _scope_id(filters.job_role)
        )
    return document.job_id is None and document.job_role is None
