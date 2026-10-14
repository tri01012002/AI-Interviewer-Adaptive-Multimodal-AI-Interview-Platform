"""Persistence operations for recruiter-owned jobs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from services.database import JobRecord, SessionLocal


class JobStore:
    @classmethod
    def create(cls, payload: dict[str, Any], owner_user_id: str) -> dict[str, Any]:
        with SessionLocal.begin() as session:
            job = JobRecord(
                owner_user_id=owner_user_id,
                title=payload["title"].strip(),
                description=payload["description"].strip(),
                requirements_json=json.dumps(payload.get("requirements", [])),
            )
            session.add(job)
            session.flush()
            return cls._to_dict(job)

    @classmethod
    def list(cls, actor_user_id: str, role: str) -> list[dict[str, Any]]:
        query = select(JobRecord).order_by(JobRecord.created_at.desc())
        if role != "admin":
            query = query.where(JobRecord.owner_user_id == actor_user_id)
        with SessionLocal.begin() as session:
            return [cls._to_dict(job) for job in session.execute(query).scalars().all()]

    @classmethod
    def get(cls, job_id: str, actor_user_id: str, role: str) -> dict[str, Any] | None:
        query = select(JobRecord).where(JobRecord.id == job_id)
        if role != "admin":
            query = query.where(JobRecord.owner_user_id == actor_user_id)
        with SessionLocal.begin() as session:
            job = session.execute(query).scalar_one_or_none()
            return cls._to_dict(job) if job is not None else None

    @classmethod
    def approve(cls, job_id: str, supervisor_id: str) -> dict[str, Any] | None:
        with SessionLocal.begin() as session:
            job = session.get(JobRecord, job_id)
            if job is None:
                return None
            if job.status != "draft":
                raise ValueError(f"Job cannot be approved from status '{job.status}'")
            job.status = "approved"
            job.approved_by = supervisor_id
            job.approved_at = datetime.now(UTC)
            session.flush()
            return cls._to_dict(job)

    @classmethod
    def get_approved_for_browser(cls, job_id: str) -> dict[str, Any] | None:
        with SessionLocal.begin() as session:
            job = session.execute(
                select(JobRecord).where(
                    JobRecord.id == job_id,
                    JobRecord.status == "approved",
                )
            ).scalar_one_or_none()
            return cls._to_dict(job) if job is not None else None

    @staticmethod
    def _to_dict(job: JobRecord) -> dict[str, Any]:
        return {
            "id": job.id,
            "owner_user_id": job.owner_user_id,
            "title": job.title,
            "description": job.description,
            "requirements": json.loads(job.requirements_json),
            "status": job.status,
            "approved_by": job.approved_by,
            "approved_at": job.approved_at,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }