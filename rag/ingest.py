"""Reproducibly upsert the version-controlled synthetic demo retrieval corpus."""

from __future__ import annotations

from rag.corpus_loader import (
    DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE,
    load_demo_corpus,
)
from rag.service import SQLAlchemyVectorStore, configured_embedding_provider


def ingest_demo_corpus() -> tuple[int, int, str]:
    documents = load_demo_corpus()
    provider = configured_embedding_provider()
    indexed_vectors = SQLAlchemyVectorStore().upsert(
        documents,
        provider,
        replace_source=True,
        global_approval_reference=DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE,
    )
    capability = "semantic" if provider is not None else "lexical-only:embedding credentials unavailable"
    return len(documents), indexed_vectors, capability


if __name__ == "__main__":
    count, vectors, capability = ingest_demo_corpus()
    print(f"upserted {count} chunks; {vectors} semantic vectors; retrieval capability={capability}")
