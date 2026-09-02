"""Run the synthetic golden retrieval benchmark and write JSON results."""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

from rag.dataset import DATASET_VERSION, GOLDEN_CASES, GOLDEN_DOCUMENTS
from rag.evaluation import evaluate_ranking
from rag.service import BM25Retriever, DenseRetriever, DeterministicReranker, HybridRetriever, RetrievalResult


class NoOpReranker:
    name = "none"

    def rerank(self, query, candidates, top_k):
        return sorted(candidates, key=lambda item: item.score, reverse=True)[:top_k]


def _retrieve(strategy: str, query: str, filters, top_k: int) -> RetrievalResult:
    if strategy == "bm25":
        started = perf_counter()
        chunks = BM25Retriever().retrieve(query, GOLDEN_DOCUMENTS, filters, top_k)
        elapsed = (perf_counter() - started) * 1000
        return RetrievalResult(query, chunks, 0, len(chunks), False, fallback_reason=f"latency_ms={elapsed:.4f}")
    if strategy == "dense":
        started = perf_counter()
        chunks = DenseRetriever().retrieve(query, GOLDEN_DOCUMENTS, filters, top_k)
        elapsed = (perf_counter() - started) * 1000
        return RetrievalResult(query, chunks, len(chunks), 0, False, fallback_reason=f"latency_ms={elapsed:.4f}")
    retriever = HybridRetriever(GOLDEN_DOCUMENTS, reranker=DeterministicReranker() if strategy == "hybrid_reranker" else NoOpReranker())
    return retriever.retrieve(query, filters, top_k)


def run(output_path: str = "rag_evaluation_results.json") -> dict:
    results = {}
    for strategy in ("bm25", "dense", "hybrid", "hybrid_reranker"):
        metric_rows = []
        latencies = []
        for case in GOLDEN_CASES:
            started = perf_counter()
            result = _retrieve(strategy, case["query"], case["filters"], 10)
            latencies.append((perf_counter() - started) * 1000)
            ranked_ids = [item.chunk.chunk_id for item in result.chunks]
            row = {"query": case["query"], "metrics": {str(k): evaluate_ranking(ranked_ids, case["relevance"], k) for k in (1, 3, 5, 10)}}
            metric_rows.append(row)
        results[strategy] = {
            "dataset_version": DATASET_VERSION,
            "rows": metric_rows,
            "mean_latency_ms": sum(latencies) / len(latencies),
            "p50_latency_ms": sorted(latencies)[len(latencies) // 2],
            "p95_latency_ms": sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))],
            "max_latency_ms": max(latencies),
        }
    payload = {"dataset_version": DATASET_VERSION, "strategies": results}
    Path(output_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


if __name__ == "__main__":
    run()
