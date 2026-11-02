"""Load and validate the versioned, synthetic interview retrieval corpus."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from config import settings
from rag.service import DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE, DocumentChunk

DEMO_CORPUS_PATH = Path(__file__).resolve().parent / "corpus" / "demo_v1.json"
DEMO_CORPUS_APPROVED_SHA256 = (
    "46706d61232929b75252b27768e3895dc5b31f5296f938985bd0409ee5e45282"
)


def load_demo_corpus(path: Path = DEMO_CORPUS_PATH) -> list[DocumentChunk]:
    raw_content = path.read_bytes()
    payload: Any = json.loads(raw_content.decode("utf-8"))
    version = payload.get("version") if isinstance(payload, dict) else None
    if version != settings.RAG_DEMO_CORPUS_VERSION:
        raise ValueError(
            f"demo corpus version mismatch: configured {settings.RAG_DEMO_CORPUS_VERSION}, found {version}"
        )
    rows = payload.get("documents")
    if not isinstance(rows, list) or not rows:
        raise ValueError("demo corpus must contain at least one document chunk")
    scope = payload.get("scope", "unclassified")
    approval_reference = payload.get("scope_approval_reference")
    if scope not in {"organization", "global", "unclassified"}:
        raise ValueError("demo corpus has an invalid scope")
    if scope == "global":
        if (
            approval_reference != DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE
            or path.resolve() != DEMO_CORPUS_PATH.resolve()
        ):
            raise ValueError(
                "global scope is permitted only for the approved repository demo corpus"
            )
        if hashlib.sha256(raw_content).hexdigest() != DEMO_CORPUS_APPROVED_SHA256:
            raise ValueError("repository demo corpus does not match its approved digest")
    elif approval_reference is not None:
        raise ValueError("only global demo corpus records may carry an approval reference")
    documents: list[DocumentChunk] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        document = _document_from_row(row, version, scope, approval_reference)
        identity = (document.document_id, document.chunk_id, document.version)
        if identity in seen:
            raise ValueError(f"duplicate demo corpus chunk identity: {identity}")
        seen.add(identity)
        documents.append(document)
    return documents


def _document_from_row(
    row: Any,
    version: str,
    scope: str,
    scope_approval_reference: str | None,
) -> DocumentChunk:
    if not isinstance(row, dict):
        raise ValueError("demo corpus entries must be objects")
    required = ("document_id", "chunk_id", "text", "document_type", "source")
    if any(not isinstance(row.get(field), str) or not row[field].strip() for field in required):
        raise ValueError("demo corpus entry is missing a required string field")
    if not isinstance(row.get("active"), bool):
        raise ValueError(f"active must be boolean for corpus chunk {row['document_id']}:{row['chunk_id']}")
    difficulty = row.get("difficulty")
    if difficulty not in (None, "easy", "medium", "hard"):
        raise ValueError(f"invalid difficulty for corpus chunk {row['document_id']}:{row['chunk_id']}")
    for optional_field in ("competency_id", "job_id", "organization_id"):
        value = row.get(optional_field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{optional_field} must be a string or null")
    tags = row.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError(f"tags must be a list of strings for corpus chunk {row['document_id']}:{row['chunk_id']}")
    job_id = row.get("job_id")
    return DocumentChunk(
        document_id=row["document_id"],
        chunk_id=row["chunk_id"],
        text=row["text"],
        competency=row.get("competency_id"),
        job_id=job_id,
        organization_id=row.get("organization_id"),
        job_role=job_id,
        source_type=row["document_type"],
        version=version,
        language=row.get("language", "en"),
        tags=tuple(tags),
        difficulty=difficulty,
        source=row["source"],
        active=row["active"],
        scope=scope,
        scope_approval_reference=scope_approval_reference,
    )
