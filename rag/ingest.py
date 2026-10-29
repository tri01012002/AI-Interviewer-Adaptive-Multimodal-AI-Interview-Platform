"""Upsert the demo corpus and indexed source-document chunks into runtime RAG."""

from __future__ import annotations

from rag.corpus_loader import (
    DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE,
    load_demo_corpus,
)
from rag.service import SQLAlchemyVectorStore, configured_embedding_provider


def ingest_demo_corpus() -> tuple[int, int, str]:
    documents = load_demo_corpus()
    return _ingest_documents(documents)


def ingest_runtime_corpus(
    indexed_root: Path = INDEXED_DOCUMENTS_PATH,
    *,
    include_demo: bool = True,
    lexical_only: bool = False,
) -> tuple[int, int, str]:
    """Ingest indexed documents, optionally alongside demo data or without embeddings."""
    documents = load_demo_corpus() if include_demo else []
    documents.extend(load_indexed_documents(indexed_root))
    return _ingest_documents(documents, lexical_only=lexical_only)


def _ingest_documents(
    documents: list[DocumentChunk],
    *,
    lexical_only: bool = False,
) -> tuple[int, int, str]:
    provider = None if lexical_only else configured_embedding_provider()
    indexed_vectors = SQLAlchemyVectorStore().upsert(
        documents,
        provider,
        replace_source=True,
        global_approval_reference=DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE,
    )
    if lexical_only:
        capability = "lexical-only: embedding disabled by request"
    elif provider is None:
        capability = "lexical-only:embedding credentials unavailable"
    else:
        capability = "semantic"
    return len(documents), indexed_vectors, capability


if __name__ == "__main__":
    count, vectors, capability = ingest_runtime_corpus()
    print(f"upserted {count} chunks; {vectors} semantic vectors; retrieval capability={capability}")
