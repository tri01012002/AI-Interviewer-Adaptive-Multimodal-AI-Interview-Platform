"""Candidate operations scoped to the authenticated resource owner."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from services.database import (
    CandidateRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    SessionLocal,
)


class CandidateStore:
    @classmethod
    def create(
        cls,
        payload: dict[str, Any],
        actor_user_id: str | None = None,
        role: str = "recruiter",
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        """Reject direct profile creation; profiles are created during redemption."""
        raise ValueError(
            "Candidate profiles are created only through validated invitation redemption"
        )

    @classmethod
    def list(
        cls, actor_user_id: str, role: str, organization_id: str | None
    ) -> list[dict[str, Any]]:
        if not organization_id:
            return []
        query = (
            select(CandidateRecord)
            .join(
                OrganizationRecord,
                OrganizationRecord.id == CandidateRecord.organization_id,
            )
            .where(
                CandidateRecord.organization_id == organization_id,
                OrganizationRecord.status == "active",
            )
            .order_by(CandidateRecord.created_at.desc())
        )
        if role == "platform_admin":
            pass
        elif role == "recruiter":
            query = (
                query.join(
                    OrganizationMembershipRecord,
                    (OrganizationMembershipRecord.organization_id
                     == CandidateRecord.organization_id)
                    & (OrganizationMembershipRecord.user_id == actor_user_id),
                )
                .where(
                    CandidateRecord.owner_user_id == actor_user_id,
                    OrganizationMembershipRecord.role == "recruiter",
                    OrganizationMembershipRecord.status == "active",
                )
            )
        elif role == "candidate":
            query = (
                query.join(
                    OrganizationMembershipRecord,
                    (OrganizationMembershipRecord.organization_id
                     == CandidateRecord.organization_id)
                    & (OrganizationMembershipRecord.user_id == actor_user_id),
                )
                .where(
                    CandidateRecord.user_id == actor_user_id,
                    OrganizationMembershipRecord.role == "candidate",
                    OrganizationMembershipRecord.status == "active",
                )
            )
        else:
            return []
        with SessionLocal.begin() as session:
            rows = session.execute(query).scalars().all()
            return [cls._to_dict(row) for row in rows]

    @classmethod
    def get(
        cls,
        candidate_id: str,
        actor_user_id: str,
        role: str,
        organization_id: str | None,
    ) -> dict[str, Any] | None:
        if not organization_id:
            return None
        with SessionLocal.begin() as session:
            candidate = cls._authorized(
                session, candidate_id, actor_user_id, role, organization_id
            )
            if candidate is None:
                return None
            return cls._to_dict(candidate)

    @classmethod
    def update(
        cls,
        candidate_id: str,
        payload: dict[str, Any],
        actor_user_id: str,
        role: str,
        organization_id: str | None,
    ) -> dict[str, Any] | None:
        if not organization_id or role == "platform_admin":
            return None
        with SessionLocal.begin() as session:
            candidate = cls._authorized(
                session, candidate_id, actor_user_id, role, organization_id
            )
            if candidate is None:
                return None
            for field in ("name", "phone", "resume_url"):
                if field in payload and (field != "name" or payload[field] is not None):
                    setattr(candidate, field, payload[field])
            candidate.updated_at = datetime.now(UTC)
            session.flush()
            return cls._to_dict(candidate)

    @classmethod
    def delete(
        cls,
        candidate_id: str,
        actor_user_id: str,
        role: str,
        organization_id: str | None,
    ) -> bool:
        if role != "platform_admin" or not organization_id:
            return False
        return False

    @staticmethod
    def _authorized(
        session,
        candidate_id: str,
        actor_user_id: str,
        role: str,
        organization_id: str,
    ):
        query = (
            select(CandidateRecord)
            .join(
                OrganizationRecord,
                OrganizationRecord.id == CandidateRecord.organization_id,
            )
            .where(
                CandidateRecord.id == candidate_id,
                CandidateRecord.organization_id == organization_id,
                OrganizationRecord.status == "active",
            )
        )
        if role == "platform_admin":
            pass
        elif role == "recruiter":
            query = (
                query.join(
                    OrganizationMembershipRecord,
                    (OrganizationMembershipRecord.organization_id
                     == CandidateRecord.organization_id)
                    & (OrganizationMembershipRecord.user_id == actor_user_id),
                )
                .where(
                    CandidateRecord.owner_user_id == actor_user_id,
                    OrganizationMembershipRecord.role == "recruiter",
                    OrganizationMembershipRecord.status == "active",
                )
            )
        elif role == "candidate":
            query = (
                query.join(
                    OrganizationMembershipRecord,
                    (OrganizationMembershipRecord.organization_id
                     == CandidateRecord.organization_id)
                    & (OrganizationMembershipRecord.user_id == actor_user_id),
                )
                .where(
                    CandidateRecord.user_id == actor_user_id,
                    OrganizationMembershipRecord.role == "candidate",
                    OrganizationMembershipRecord.status == "active",
                )
            )
        else:
            return None
        return session.execute(query).scalar_one_or_none()

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
