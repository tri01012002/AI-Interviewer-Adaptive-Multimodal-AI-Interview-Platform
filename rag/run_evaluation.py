"""Measure retrieval quality against the version-controlled synthetic evaluation set."""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any

from rag.corpus_loader import DEMO_CORPUS_PATH, load_demo_corpus
from rag.evaluation import evaluate_ranking
from rag.service import (
    BM25Retriever,
    DenseRetriever,
    DeterministicReranker,
    HybridRetriever,
    RetrievalFilters,
    configured_embedding_provider,
)

EVALUATION_PATH = DEMO_CORPUS_PATH.with_name("retrieval_eval_v1.json")


def _filters(payload: dict[str, str]) -> RetrievalFilters:
    return RetrievalFilters(
        competency=payload.get("competency"),
        job_id=payload.get("job_id"),
        difficulty=payload.get("difficulty"),
        source_type=payload.get("document_type"),
        include_global=True,
    )


def _rank_documents(chunks: list[Any]) -> list[str]:
    ranked: list[str] = []
    for item in chunks:
        document_id = item.chunk.document_id
        if document_id not in ranked:
            ranked.append(document_id)
    return ranked


def run() -> dict[str, Any]:
    corpus = load_demo_corpus()
    dataset = json.loads(EVALUATION_PATH.read_text(encoding="utf-8"))
    provider = configured_embedding_provider()
    dense = DenseRetriever(provider)
    hybrid = HybridRetriever(
        documents=corpus,
        dense=dense,
        reranker=DeterministicReranker(),
    )
    cases = []
    for case in dataset["cases"]:
        query = case["query"]
        filters = _filters(case["filters"])
        started = perf_counter()
        lexical = BM25Retriever().retrieve(query, corpus, filters, top_k=len(corpus))
        combined = hybrid.retrieve(query, filters, top_k=len(corpus))
        elapsed_ms = (perf_counter() - started) * 1000
        lexical_ids = _rank_documents(lexical)
        combined_ids = _rank_documents(combined.chunks)
        relevant_ids = case["relevant_document_ids"]
        relevance = {document_id: 1.0 for document_id in relevant_ids}
        cases.append(
            {
                "query": query,
                "filters": case["filters"],
                "lexical": {
                    "ranked_document_ids": lexical_ids,
                    "metrics_at_5": evaluate_ranking(lexical_ids[:5], relevance, 5),
                },
                "hybrid": {
                    "ranked_document_ids": combined_ids,
                    "metrics_at_5": evaluate_ranking(combined_ids[:5], relevance, 5),
                    "dense_candidates": combined.dense_count,
                    "fallback_reason": combined.fallback_reason,
                },
                "forbidden_documents_excluded": not (
                    set(combined_ids) & set(case.get("forbidden_document_ids", []))
                ),
                "forbidden_document_ids": case.get("forbidden_document_ids", []),
                "expected_relevant_documents": relevant_ids,
                "latency_ms": round(elapsed_ms, 3),
            }
        )

    metric_cases = [
        case
        for case, source in zip(cases, dataset["cases"], strict=True)
        if source["relevant_document_ids"]
    ]
    semantic_query_count = sum(
        case["hybrid"]["dense_candidates"] > 0 for case in cases
    )
    semantic_retrieval_ran = provider is not None and semantic_query_count > 0

    def mean(metric: str) -> float:
        return sum(
            case["hybrid"]["metrics_at_5"][metric] for case in metric_cases
        ) / max(len(metric_cases), 1)

    return {
        "dataset_version": dataset["dataset_version"],
        "corpus_version": json.loads(DEMO_CORPUS_PATH.read_text(encoding="utf-8"))["version"],
        "embedding_provider": provider.__class__.__name__ if provider is not None else None,
        "embedding_model": provider.model if provider is not None else None,
        "semantic_embeddings_enabled": semantic_retrieval_ran,
        "semantic_query_count": semantic_query_count,
        "lexical_fallback_case_count": sum(
            case["hybrid"]["dense_candidates"] == 0 for case in cases
        ),
        "quality_claim": (
            "semantic hybrid retrieval measured; compare against the lexical baseline"
            if semantic_retrieval_ran
            else "embedding provider is not configured; lexical-only metrics measured"
            if provider is None
            else "embedding provider is configured but dense retrieval did not run; lexical-only metrics measured"
        ),
        "hybrid_mean_recall_at_5": mean("recall"),
        "hybrid_mean_mrr_at_5": mean("mrr"),
        "lexical_mean_recall_at_5": sum(
            case["lexical"]["metrics_at_5"]["recall"] for case in metric_cases
        ) / max(len(metric_cases), 1),
        "lexical_mean_mrr_at_5": sum(
            case["lexical"]["metrics_at_5"]["mrr"] for case in metric_cases
        ) / max(len(metric_cases), 1),
        "forbidden_scope_cases_passed": sum(
            case["forbidden_documents_excluded"]
            for case in cases
            if case["forbidden_document_ids"]
        ),
        "forbidden_scope_cases_total": sum(bool(case["forbidden_document_ids"]) for case in cases),
        "cases": cases,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
