"""Candidate management store used by the API layer."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from services.database import CandidateRecord, SessionLocal


class CandidateStore:
    @classmethod
    def create(cls, payload: dict[str, Any]) -> dict[str, Any]:
        email = str(payload["email"]).strip().lower()
        with SessionLocal() as session:
            existing = session.execute(
                select(CandidateRecord).where(CandidateRecord.email == email)
            ).scalar_one_or_none()
            if existing is not None:
                return cls._to_dict(existing)

            candidate = CandidateRecord(
                name=payload["name"],
                email=email,
                phone=payload.get("phone"),
                resume_url=payload.get("resume_url"),
            )
            session.add(candidate)
            session.commit()
            session.refresh(candidate)
            return cls._to_dict(candidate)

    @classmethod
    def list(cls) -> list[dict[str, Any]]:
        with SessionLocal() as session:
            rows = session.execute(select(CandidateRecord).order_by(CandidateRecord.created_at.desc())).scalars().all()
            return [cls._to_dict(row) for row in rows]

    @classmethod
    def get(cls, candidate_id: str) -> dict[str, Any] | None:
        with SessionLocal() as session:
            candidate = session.get(CandidateRecord, candidate_id)
            return None if candidate is None else cls._to_dict(candidate)

    @classmethod
    def update(cls, candidate_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with SessionLocal() as session:
            candidate = session.get(CandidateRecord, candidate_id)
            if candidate is None:
                return None
            for field in ("name", "email", "phone", "resume_url"):
                if field in payload:
                    setattr(candidate, field, payload[field])
            candidate.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(candidate)
            return cls._to_dict(candidate)

    @classmethod
    def delete(cls, candidate_id: str) -> bool:
        with SessionLocal() as session:
            candidate = session.get(CandidateRecord, candidate_id)
            if candidate is None:
                return False
            session.delete(candidate)
            session.commit()
            return True

    @staticmethod
    def _to_dict(candidate: CandidateRecord) -> dict[str, Any]:
        return {
            "id": candidate.id,
            "name": candidate.name,
            "email": candidate.email,
            "phone": candidate.phone,
            "resume_url": candidate.resume_url,
            "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
            "updated_at": candidate.updated_at.isoformat() if candidate.updated_at else None,
        }
