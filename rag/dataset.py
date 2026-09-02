"""Synthetic golden retrieval fixture for local metric evaluation."""

from __future__ import annotations

from rag.service import DocumentChunk, RetrievalFilters

DATASET_VERSION = "golden-v1"

GOLDEN_DOCUMENTS = [
    DocumentChunk("doc-python", "python-async", "Python async programming uses asyncio for concurrent I/O-bound services.", competency="python", job_role="ai engineer", source_type="knowledge"),
    DocumentChunk("doc-python", "python-process", "Python multiprocessing isolates CPU-bound work across processes.", competency="python", job_role="ai engineer", source_type="knowledge"),
    DocumentChunk("doc-redis", "redis-cache", "Redis cache design requires TTL, invalidation, and consistency reasoning.", competency="distributed_systems", job_role="backend engineer", source_type="rubric"),
    DocumentChunk("doc-postgres", "postgres-isolation", "PostgreSQL isolation levels control visibility and transaction anomalies.", competency="databases", job_role="backend engineer", source_type="knowledge"),
    DocumentChunk("doc-ml", "ml-deploy", "Production model deployment should cover monitoring, rollback, and measurable latency.", competency="machine_learning", job_role="ai engineer", source_type="job_requirement"),
]

GOLDEN_CASES = [
    {
        "query": "How would you handle Python async I/O concurrency?",
        "competency": "python",
        "difficulty": "medium",
        "filters": RetrievalFilters(competency="python", job_role="ai engineer"),
        "relevance": {"python-async": 3, "python-process": 1},
    },
    {
        "query": "What does Redis cache invalidation require?",
        "competency": "distributed_systems",
        "difficulty": "medium",
        "filters": RetrievalFilters(competency="distributed_systems"),
        "relevance": {"redis-cache": 3},
    },
    {
        "query": "Explain PostgreSQL isolation levels and anomalies.",
        "competency": "databases",
        "difficulty": "hard",
        "filters": RetrievalFilters(competency="databases", source_type="knowledge"),
        "relevance": {"postgres-isolation": 3},
    },
    {
        "query": "How do you monitor production model latency?",
        "competency": "machine_learning",
        "difficulty": "medium",
        "filters": RetrievalFilters(job_role="ai engineer", source_type="job_requirement"),
        "relevance": {"ml-deploy": 3},
    },
]
