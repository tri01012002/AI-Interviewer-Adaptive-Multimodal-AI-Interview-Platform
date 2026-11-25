"""Deterministic retrieval probes for interview-time RAG quality and tenant isolation.

Runs the fixed probe set in ``rag/corpus/retrieval_probes_v1.json`` through the
production path (``RAGQuestionService.retrieve_context`` -> ``HybridRetriever`` ->
``SQLAlchemyVectorStore``) against a throwaway SQLite database. Retrieval is
lexical-only: no embedding provider is configured and no network call is made.

Usage: ``python -m rag.retrieval_probes`` prints the JSON report.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

PROBES_PATH = Path(__file__).resolve().parent / "corpus" / "retrieval_probes_v1.json"
DEMO_CORPUS_PATH = Path(__file__).resolve().parent / "corpus" / "demo_v1.json"


def load_probes(path: Path = PROBES_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _insert_corpus(factory: Any, service_module: ModuleType, payload: dict[str, Any]) -> None:
    from services.database import RAGDocumentRecord

    demo = json.loads(DEMO_CORPUS_PATH.read_text(encoding="utf-8"))
    approval = service_module.DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE
    # Global rows are inserted as the approved ingestion would store them.
    with factory.begin() as session:
        for row in demo["documents"]:
            session.add(
                RAGDocumentRecord(
                    document_id=row["document_id"],
                    chunk_id=row["chunk_id"],
                    text=row["text"],
                    document_type=row["document_type"],
                    competency_id=row.get("competency_id"),
                    job_id=row.get("job_id"),
                    organization_id=None,
                    scope="global",
                    scope_approval_reference=approval,
                    difficulty=row.get("difficulty"),
                    source=row["source"],
                    version=demo["version"],
                    active=bool(row["active"]),
                    language="en",
                    tags_json=json.dumps(row.get("tags", [])),
                )
            )
    # Tenant rows go through the real organization ingestion path.
    store = service_module.SQLAlchemyVectorStore(factory)
    store.upsert(
        [
            service_module.DocumentChunk(
                document_id=row["document_id"],
                chunk_id=row["chunk_id"],
                text=row["text"],
                competency=row.get("competency"),
                job_id=row.get("job_id"),
                organization_id=row["organization_id"],
                scope="organization",
                source="retrieval-probe-fixture",
            )
            for row in payload["tenant_documents"]
        ],
        None,
    )


def _isolation_violations(probe: dict[str, Any], chunks: list[Any], service_module: ModuleType) -> list[str]:
    organization_id = probe.get("organization_id")
    violations = []
    for item in chunks:
        chunk = item.chunk
        if chunk.scope == "global":
            if (
                chunk.organization_id is not None
                or chunk.scope_approval_reference
                != service_module.DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE
            ):
                violations.append(f"{chunk.document_id}:unapproved_global")
            continue
        if organization_id is None or chunk.organization_id != organization_id:
            violations.append(f"{chunk.document_id}:foreign_tenant")
        elif (chunk.job_id or "").startswith("campaign:"):
            # No probe names a campaign, so campaign-only chunks must never appear.
            violations.append(f"{chunk.document_id}:foreign_campaign")
    return violations


def run_probes(service_module: ModuleType | None = None, k: int | None = None) -> dict[str, Any]:
    """Evaluate every probe; ``service_module`` lets a baseline copy be measured."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from services.database import RAGDocumentRecord

    if service_module is None:
        import rag.service as service_module

    payload = load_probes()
    top_k = int(k or payload.get("k", 5))
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'retrieval-probes.db'}")
        try:
            RAGDocumentRecord.__table__.create(engine)
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            _insert_corpus(factory, service_module, payload)
            service = service_module.RAGQuestionService(
                service_module.HybridRetriever(
                    dense=service_module.DenseRetriever(None),
                    vector_store=service_module.SQLAlchemyVectorStore(factory),
                )
            )
            service.max_chunks = top_k
            cases = []
            for probe in payload["probes"]:
                result = service.retrieve_context(
                    probe["position"],
                    probe["competency"],
                    probe["gaps"],
                    organization_id=probe.get("organization_id"),
                )
                ranked = []
                for item in result.chunks:
                    if item.chunk.document_id not in ranked:
                        ranked.append(item.chunk.document_id)
                relevant = set(probe["relevant_document_ids"])
                hits = relevant & set(ranked[:top_k])
                cases.append(
                    {
                        "id": probe["id"],
                        "kind": probe["kind"],
                        "retrieved_document_ids": ranked,
                        "hit": bool(hits),
                        "recall": (len(hits) / len(relevant)) if relevant else None,
                        "isolation_violations": _isolation_violations(
                            probe, result.chunks, service_module
                        ),
                        "job_scoped_document_ids": sorted(
                            {
                                item.chunk.document_id
                                for item in result.chunks
                                if item.chunk.job_id is not None
                            }
                        ),
                        "fallback_reason": result.fallback_reason,
                    }
                )
        finally:
            engine.dispose()
    positives = [case for case in cases if case["kind"] == "positive"]
    negatives = [case for case in cases if case["kind"] == "negative"]
    return {
        "dataset_version": payload["dataset_version"],
        "k": top_k,
        "retrieval_mode": "lexical_only",
        "positive_probe_count": len(positives),
        "negative_probe_count": len(negatives),
        "hit_rate_at_k": _ratio(sum(case["hit"] for case in positives), len(positives)),
        "zero_result_rate": _ratio(
            sum(not case["retrieved_document_ids"] for case in positives), len(positives)
        ),
        "relevant_doc_recall_at_k": _ratio(
            sum(case["recall"] for case in positives), len(positives)
        ),
        "tenant_isolation_violations": sum(len(case["isolation_violations"]) for case in cases),
        "negative_job_scope_violations": sum(
            len(case["job_scoped_document_ids"]) for case in negatives
        ),
        "cases": cases,
    }


def _ratio(numerator: float, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def load_service_module(path: str | Path, name: str = "rag_service_probe_baseline") -> ModuleType:
    """Load an alternate copy of rag/service.py (e.g. a pre-change baseline)."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load service module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":  # pragma: no cover - manual measurement entry point
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-path", help="alternate rag/service.py to measure")
    parser.add_argument("--summary", action="store_true", help="omit per-probe details")
    arguments = parser.parse_args()
    module = load_service_module(arguments.service_path) if arguments.service_path else None
    report = run_probes(module)
    if arguments.summary:
        report = {key: value for key, value in report.items() if key != "cases"} | {
            "cases": {
                case["id"]: {
                    "hit": case["hit"],
                    "retrieved": case["retrieved_document_ids"],
                    "isolation_violations": case["isolation_violations"],
                    "job_scoped": case["job_scoped_document_ids"],
                }
                for case in report["cases"]
            }
        }
    print(json.dumps(report, indent=2))
