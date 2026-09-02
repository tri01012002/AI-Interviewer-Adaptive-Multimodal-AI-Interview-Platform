# Phase 7.5 RAG Audit

**Date:** 2026-09-02

## Actual Component Classification

| Component | Classification | Evidence and limitation |
|---|---|---|
| `DocumentChunk` | REAL IMPLEMENTATION | Immutable attributed chunk model with document/chunk IDs and metadata |
| `EmbeddingProvider` | INTERFACE / ABSTRACTION | Provider boundary only; no hosted embedding implementation |
| `HashEmbeddingProvider` | DETERMINISTIC TEST IMPLEMENTATION | Produces normalized vectors for repeatable local tests; not semantic production quality |
| `DenseRetriever` | REAL LOCAL IMPLEMENTATION | Scores filtered chunks with embedding dot products |
| `BM25Retriever` | REAL LOCAL IMPLEMENTATION | Uses `rank_bm25`, not substring matching |
| `HybridRetriever` | REAL LOCAL IMPLEMENTATION | Early metadata filtering, RRF fusion, deduplication, reranker call |
| `Reranker` | INTERFACE / ABSTRACTION | Injectable provider boundary |
| `DeterministicReranker` | DETERMINISTIC TEST IMPLEMENTATION | Reorders by query-term coverage and retrieval signal; not a learned cross-encoder |
| `RAGQuestionService` | REAL LOCAL INTEGRATION | Uses the hybrid retriever for interview guidance and follow-up selection |
| Corpus | DETERMINISTIC TEST IMPLEMENTATION | Small in-memory synthetic chunks; no ingestion lifecycle |
| Persistent vector store | NOT IMPLEMENTED | Deferred until corpus ingestion/lifecycle exists |
| Document ingestion | NOT IMPLEMENTED | No parser/chunker/enrichment pipeline exists |
| Retrieval metrics | IMPLEMENTED IN PHASE 7.5 | Independent Recall, Precision, MRR, and NDCG functions |
| Golden dataset | DETERMINISTIC TEST IMPLEMENTATION | Synthetic interview-domain fixture, not production quality evidence |
| PostgreSQL verification | NOT VERIFIED | PostgreSQL/Docker unavailable locally |

## Boundary Decisions

The current phase adds evaluation and safety controls only. It does not add a vector database,
pgvector tables, ingestion infrastructure, external embedding APIs, learned reranking, voice,
Redis, Celery, browser automation, or Kubernetes.

Persistent vector storage is explicitly deferred until document ingestion and corpus versioning are
implemented. The current local corpus is sufficient to test retrieval behavior and metric code,
not to establish production retrieval quality.
