# Phase 7 Hybrid RAG Report

**Date:** 2026-09-02  
**Status:** PARTIAL, locally verified

## Audit Findings

Before Phase 7, `RAGQuestionService` used a mutable in-memory position dictionary. There was no
document model, source attribution, embedding interface, dense retrieval, BM25, metadata filtering,
fusion, reranking, ingestion, or retrieval evaluation. The LangGraph and evaluation boundaries
were preserved.

## Retrieval Architecture

```text
Query
  -> metadata filter
  -> dense retrieval       -+
  -> BM25 lexical retrieval -+-> RRF candidate fusion -> reranker -> top K
```

The public abstraction is `HybridRetriever.retrieve(query, filters, top_k) -> RetrievalResult`.
LangGraph and application code do not depend on a vector database implementation.

## Document Model

`DocumentChunk` carries source-attributable `document_id` and `chunk_id`, text, competency,
job role, source type, version, language, and tags. `RetrievedChunk` preserves the source chunk
and dense/lexical/reranked scores. Default local content includes question guidance and a rubric
chunk; the model supports job requirements and technical knowledge through the same metadata shape.

## Dense Retrieval

`EmbeddingProvider` is the production boundary. `HashEmbeddingProvider` is a deterministic local
embedding implementation that produces normalized dense vectors and cosine-like dot-product
ranking. It is suitable for repeatable tests, not a claim of semantic embedding quality. A hosted
embedding model is **NOT IMPLEMENTED**.

## Lexical Retrieval

`BM25Retriever` uses the declared `rank-bm25` implementation over tokenized document chunks.
This preserves exact terminology behavior for technical terms such as Redis and PostgreSQL
isolation levels.

## Fusion and Filtering

Metadata filtering occurs before both retrieval passes. Dense and lexical candidates are
combined with Reciprocal Rank Fusion using $1/(60 + rank)$, deduplicated by `chunk_id`, and then
sent to the reranker. Scores from the two retrieval systems are not directly added.

## Reranking

`Reranker` is an injectable interface. `DeterministicReranker` actually reorders candidates using
query-term coverage plus the best retrieval signal; it is a deterministic test/local reranker,
not a production cross-encoder. A learned production reranker is **NOT IMPLEMENTED**.

## Interview Integration

`RAGQuestionService` is now backed by `HybridRetriever` and remains compatible with the existing
`InterviewService` contract. Retrieved, source-attributed question guidance is used for relevant
questions and follow-up selection. The graph remains responsible for semantic adaptive decisions;
the service/retrieval boundary prevents a vector store from leaking into graph state.

## Evaluation

A retrieval test set is represented by deterministic fixtures and covers BM25 terminology,
dense scoring, metadata filtering, fusion deduplication, and reranking order. Formal Recall@K,
Precision@K, MRR, NDCG, latency, and cost benchmarks are **NOT IMPLEMENTED** in this phase.
No production retrieval quality is claimed.

## Tests and Verification

```text
python -m pytest
42 passed, 83 warnings

Focused retrieval/integration run:
17 passed, 44 warnings

python -m alembic downgrade -1
python -m alembic upgrade head
python -m alembic current
8b2f_evidence_assessments (head)

git diff --check
passed
```

Touched retrieval files report no editor diagnostics.

## Status and Limitations

| Capability | Status |
|---|---|
| Retrieval abstraction | IMPLEMENTED |
| Attributed document chunks | IMPLEMENTED |
| Metadata filtering | IMPLEMENTED |
| BM25 lexical retrieval | IMPLEMENTED locally |
| Deterministic dense retrieval | IMPLEMENTED for local testing |
| Dense production embedding provider | NOT IMPLEMENTED |
| RRF candidate fusion | IMPLEMENTED |
| Deterministic reranker | IMPLEMENTED locally |
| Learned production reranker | NOT IMPLEMENTED |
| Ingestion/parser/chunking pipeline | NOT IMPLEMENTED |
| Vector database/pgvector runtime | NOT IMPLEMENTED |
| Golden retrieval metrics | NOT IMPLEMENTED |
| PostgreSQL verification | BLOCKED |
| Voice, browser, Redis, Celery, Kubernetes | NOT IMPLEMENTED |

## PostgreSQL Status

**POSTGRESQL VERIFICATION: BLOCKED.** Docker/PostgreSQL is unavailable in this environment.
Migration and tests were executed against local SQLite only.
