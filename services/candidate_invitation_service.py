"""Audited recruiter invitations for linking verified candidate accounts."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from services.database import (
    AccessAuditRecord,
    CandidateInvitationRecord,
    CandidateRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    SessionLocal,
    UserRecord,
)

INVITATION_TTL = timedelta(hours=72)
INVITATION_COOLDOWN = timedelta(minutes=1)
INVITATION_ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_RECRUITER_INVITATIONS_PER_HOUR = 20
MAX_REDEMPTION_ATTEMPTS_PER_WINDOW = 20
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
UNAVAILABLE_MESSAGE = "Invitation is invalid or unavailable"


class InvitationOperationError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _InvalidLink(Exception):
    pass


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CandidateInvitationService:
    @staticmethod
    def _attempt_link(
        session,
        token: str,
        candidate_user_id: str,
        now: datetime,
        requested_organization_id: str | None = None,
    ) -> str | None:
        user = session.get(UserRecord, candidate_user_id)
        if (
            not TOKEN_PATTERN.fullmatch(token)
            or user is None
            or user.role != "candidate"
            or user.email_verified_at is None
        ):
            return None

        token_hash = _token_digest(token)
        invitation = session.execute(
            select(CandidateInvitationRecord)
            .where(CandidateInvitationRecord.token_hash == token_hash)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            invitation is None
            or invitation.organization_id is None
            or (
                requested_organization_id is not None
                and requested_organization_id != invitation.organization_id
            )
            or invitation.consumed_at is not None
            or invitation.revoked_at is not None
            or _aware_utc(invitation.expires_at) <= now
            or normalize_email(invitation.email) != normalize_email(user.email)
        ):
            return None

        organization = session.execute(
            select(OrganizationRecord).where(
                OrganizationRecord.id == invitation.organization_id,
                OrganizationRecord.status == "active",
            )
        ).scalar_one_or_none()
        recruiter_membership = session.execute(
            select(OrganizationMembershipRecord).where(
                OrganizationMembershipRecord.organization_id == invitation.organization_id,
                OrganizationMembershipRecord.user_id == invitation.recruiter_user_id,
                OrganizationMembershipRecord.role == "recruiter",
                OrganizationMembershipRecord.status == "active",
            )
        ).scalar_one_or_none()
        if organization is None or recruiter_membership is None:
            return None

        active_memberships = list(
            session.execute(
                select(OrganizationMembershipRecord)
                .join(
                    OrganizationRecord,
                    OrganizationRecord.id
                    == OrganizationMembershipRecord.organization_id,
                )
                .where(
                    OrganizationMembershipRecord.user_id == candidate_user_id,
                    OrganizationMembershipRecord.status == "active",
                    OrganizationRecord.status == "active",
                )
            ).scalars()
        )
        if len(active_memberships) > 1 and requested_organization_id is None:
            return None
        if requested_organization_id is None and active_memberships and (
            active_memberships[0].organization_id != invitation.organization_id
            or active_memberships[0].role != "candidate"
        ):
            return None

        try:
            with session.begin_nested():
                membership = session.execute(
                    select(OrganizationMembershipRecord)
                    .where(
                        OrganizationMembershipRecord.organization_id
                        == invitation.organization_id,
                        OrganizationMembershipRecord.user_id == candidate_user_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if membership is not None:
                    if membership.role != "candidate" or membership.status == "active":
                        raise _InvalidLink
                    membership.status = "active"
                    membership.revoked_at = None
                    membership.granted_by_user_id = invitation.recruiter_user_id
                else:
                    membership = OrganizationMembershipRecord(
                        organization_id=invitation.organization_id,
                        user_id=candidate_user_id,
                        role="candidate",
                        status="active",
                        granted_by_user_id=invitation.recruiter_user_id,
                    )
                    session.add(membership)
                    session.flush()

                if invitation.candidate_id is None:
                    if not invitation.candidate_name:
                        raise _InvalidLink
                    candidate = CandidateRecord(
                        name=invitation.candidate_name,
                        email=normalize_email(user.email),
                        organization_id=invitation.organization_id,
                        owner_user_id=invitation.recruiter_user_id,
                        user_id=candidate_user_id,
                    )
                    session.add(candidate)
                    session.flush()
                    candidate_id = candidate.id
                else:
                    candidate = session.execute(
                        select(CandidateRecord)
                        .where(
                            CandidateRecord.id == invitation.candidate_id,
                            CandidateRecord.organization_id
                            == invitation.organization_id,
                            CandidateRecord.owner_user_id
                            == invitation.recruiter_user_id,
                            CandidateRecord.user_id.is_(None),
                            CandidateRecord.email == normalize_email(user.email),
                        )
                        .with_for_update()
                    ).scalar_one_or_none()
                    if candidate is None:
                        raise _InvalidLink
                    candidate.user_id = candidate_user_id
                    candidate_id = candidate.id

                consumed = session.execute(
                    update(CandidateInvitationRecord)
                    .where(
                        CandidateInvitationRecord.id == invitation.id,
                        CandidateInvitationRecord.organization_id
                        == invitation.organization_id,
                        CandidateInvitationRecord.consumed_at.is_(None),
                        CandidateInvitationRecord.revoked_at.is_(None),
                        CandidateInvitationRecord.expires_at > now,
                    )
                    .values(consumed_at=now)
                    .execution_options(synchronize_session=False)
                )
                if consumed.rowcount != 1:
                    raise _InvalidLink
                invitation.candidate_id = candidate_id
                session.add(
                    AccessAuditRecord(
                        actor_user_id=candidate_user_id,
                        organization_id=invitation.organization_id,
                        requested_organization_id=invitation.organization_id,
                        action="redeem",
                        resource_type="candidate",
                        resource_id=candidate_id,
                    )
                )
                return candidate_id
        except (_InvalidLink, IntegrityError):
            return None

    @classmethod
    def create_for_organization(
        cls,
        organization_id: str,
        candidate_name: str,
        email: str,
        recruiter_user_id: str,
        role: str,
    ) -> dict[str, Any]:
        if role != "recruiter":
            raise InvitationOperationError(403, "Recruiter membership required")
        normalized_email = normalize_email(email)
        normalized_name = candidate_name.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_email):
            raise InvitationOperationError(422, "A valid candidate email is required")
        if not normalized_name:
            raise InvitationOperationError(422, "Candidate name is required")

        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        try:
            with SessionLocal.begin() as session:
                recruiter_membership = session.execute(
                    select(OrganizationMembershipRecord)
                    .join(
                        OrganizationRecord,
                        OrganizationRecord.id
                        == OrganizationMembershipRecord.organization_id,
                    )
                    .where(
                        OrganizationMembershipRecord.organization_id == organization_id,
                        OrganizationMembershipRecord.user_id == recruiter_user_id,
                        OrganizationMembershipRecord.role == "recruiter",
                        OrganizationMembershipRecord.status == "active",
                        OrganizationRecord.status == "active",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if recruiter_membership is None:
                    raise InvitationOperationError(403, "Recruiter membership required")

                existing_candidate = session.execute(
                    select(CandidateRecord.id).where(
                        CandidateRecord.organization_id == organization_id,
                        CandidateRecord.email == normalized_email,
                    )
                ).scalar_one_or_none()
                if existing_candidate is not None:
                    raise InvitationOperationError(
                        409,
                        "A mapped candidate profile already exists; use its invitation route",
                    )

                last_invitation = session.execute(
                    select(CandidateInvitationRecord.created_at)
                    .where(
                        CandidateInvitationRecord.organization_id == organization_id,
                        CandidateInvitationRecord.email == normalized_email,
                    )
                    .order_by(CandidateInvitationRecord.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if (
                    last_invitation is not None
                    and _aware_utc(last_invitation) > now - INVITATION_COOLDOWN
                ):
                    raise InvitationOperationError(
                        429, "Invitation creation is temporarily limited"
                    )

                invitation_count = session.execute(
                    select(func.count(CandidateInvitationRecord.id)).where(
                        CandidateInvitationRecord.organization_id == organization_id,
                        CandidateInvitationRecord.recruiter_user_id == recruiter_user_id,
                        CandidateInvitationRecord.created_at >= now - timedelta(hours=1),
                    )
                ).scalar_one()
                if invitation_count >= MAX_RECRUITER_INVITATIONS_PER_HOUR:
                    raise InvitationOperationError(
                        429, "Invitation creation is temporarily limited"
                    )

                session.execute(
                    update(CandidateInvitationRecord)
                    .where(
                        CandidateInvitationRecord.organization_id == organization_id,
                        CandidateInvitationRecord.email == normalized_email,
                        CandidateInvitationRecord.consumed_at.is_(None),
                        CandidateInvitationRecord.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
                invitation = CandidateInvitationRecord(
                    recruiter_user_id=recruiter_user_id,
                    organization_id=organization_id,
                    candidate_name=normalized_name,
                    email=normalized_email,
                    token_hash=_token_digest(token),
                    expires_at=now + INVITATION_TTL,
                )
                session.add(invitation)
                session.flush()
                session.add(
                    AccessAuditRecord(
                        actor_user_id=recruiter_user_id,
                        organization_id=organization_id,
                        requested_organization_id=organization_id,
                        action="create",
                        resource_type="candidate_invitation",
                        resource_id=invitation.id,
                    )
                )
                return {
                    "invitation_id": invitation.id,
                    "token": token,
                    "expires_at": invitation.expires_at,
                }
        except IntegrityError as exc:
            raise InvitationOperationError(
                429, "Invitation creation is temporarily limited"
            ) from exc

    @classmethod
    def create(
        cls,
        candidate_id: str,
        recruiter_user_id: str,
        role: str,
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        if role != "recruiter" or not organization_id:
            raise InvitationOperationError(403, "Recruiter membership required")

        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        try:
            with SessionLocal.begin() as session:
                recruiter = session.execute(
                    select(OrganizationMembershipRecord.id)
                    .join(
                        OrganizationRecord,
                        OrganizationRecord.id
                        == OrganizationMembershipRecord.organization_id,
                    )
                    .where(
                        OrganizationMembershipRecord.organization_id == organization_id,
                        OrganizationMembershipRecord.user_id == recruiter_user_id,
                        OrganizationMembershipRecord.role == "recruiter",
                        OrganizationMembershipRecord.status == "active",
                        OrganizationRecord.status == "active",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if recruiter is None:
                    raise InvitationOperationError(403, "Recruiter membership required")
                candidate = session.execute(
                    select(CandidateRecord).where(
                        CandidateRecord.id == candidate_id,
                        CandidateRecord.organization_id == organization_id,
                        CandidateRecord.owner_user_id == recruiter_user_id,
                    ).with_for_update()
                ).scalar_one_or_none()
                if candidate is None:
                    raise InvitationOperationError(404, "Candidate not found")
                if candidate.user_id is not None:
                    raise InvitationOperationError(
                        409, "Candidate profile is not eligible for an invitation"
                    )

                last_candidate_invitation = session.execute(
                    select(CandidateInvitationRecord.created_at)
                    .where(CandidateInvitationRecord.candidate_id == candidate_id)
                    .order_by(CandidateInvitationRecord.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if (
                    last_candidate_invitation is not None
                    and _aware_utc(last_candidate_invitation)
                    > now - INVITATION_COOLDOWN
                ):
                    raise InvitationOperationError(
                        429, "Invitation creation is temporarily limited"
                    )

                recruiter_invitation_count = session.execute(
                    select(func.count(CandidateInvitationRecord.id)).where(
                        CandidateInvitationRecord.recruiter_user_id
                        == recruiter_user_id,
                        CandidateInvitationRecord.organization_id == organization_id,
                        CandidateInvitationRecord.created_at
                        >= now - timedelta(hours=1),
                    )
                ).scalar_one()
                if recruiter_invitation_count >= MAX_RECRUITER_INVITATIONS_PER_HOUR:
                    raise InvitationOperationError(
                        429, "Invitation creation is temporarily limited"
                    )

                session.execute(
                    update(CandidateInvitationRecord)
                    .where(
                        CandidateInvitationRecord.candidate_id == candidate_id,
                        CandidateInvitationRecord.consumed_at.is_(None),
                        CandidateInvitationRecord.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
                invitation = CandidateInvitationRecord(
                    candidate_id=candidate_id,
                    recruiter_user_id=recruiter_user_id,
                    organization_id=organization_id,
                    email=normalize_email(candidate.email),
                    token_hash=_token_digest(token),
                    expires_at=now + INVITATION_TTL,
                )
                session.add(invitation)
                session.flush()
                session.add(
                    AccessAuditRecord(
                        actor_user_id=recruiter_user_id,
                        organization_id=organization_id,
                        requested_organization_id=organization_id,
                        action="create",
                        resource_type="candidate_invitation",
                        resource_id=invitation.id,
                    )
                )
                return {
                    "invitation_id": invitation.id,
                    "token": token,
                    "expires_at": invitation.expires_at,
                }
        except IntegrityError as exc:
            raise InvitationOperationError(
                429, "Invitation creation is temporarily limited"
            ) from exc

    @classmethod
    def revoke(
        cls,
        candidate_id: str,
        recruiter_user_id: str,
        role: str,
        organization_id: str | None = None,
    ) -> None:
        if role != "recruiter" or not organization_id:
            raise InvitationOperationError(403, "Recruiter membership required")

        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            recruiter_membership = session.execute(
                select(OrganizationMembershipRecord.id)
                .join(
                    OrganizationRecord,
                    OrganizationRecord.id
                    == OrganizationMembershipRecord.organization_id,
                )
                .where(
                    OrganizationMembershipRecord.organization_id == organization_id,
                    OrganizationMembershipRecord.user_id == recruiter_user_id,
                    OrganizationMembershipRecord.role == "recruiter",
                    OrganizationMembershipRecord.status == "active",
                    OrganizationRecord.status == "active",
                )
            ).scalar_one_or_none()
            if recruiter_membership is None:
                raise InvitationOperationError(403, "Recruiter membership required")
            candidate = session.execute(
                select(CandidateRecord.id).where(
                    CandidateRecord.id == candidate_id,
                    CandidateRecord.organization_id == organization_id,
                    CandidateRecord.owner_user_id == recruiter_user_id,
                ).with_for_update()
            ).scalar_one_or_none()
            if candidate is None:
                raise InvitationOperationError(404, "Candidate not found")
            invitation_id = session.execute(
                update(CandidateInvitationRecord)
                .where(
                    CandidateInvitationRecord.candidate_id == candidate_id,
                    CandidateInvitationRecord.organization_id == organization_id,
                    CandidateInvitationRecord.recruiter_user_id == recruiter_user_id,
                    CandidateInvitationRecord.consumed_at.is_(None),
                    CandidateInvitationRecord.revoked_at.is_(None),
                )
                .values(revoked_at=now)
                .returning(CandidateInvitationRecord.id)
            ).scalar_one_or_none()
            if invitation_id is None:
                raise InvitationOperationError(
                    404, "Candidate invitation not found"
                )
            session.add(
                AccessAuditRecord(
                    actor_user_id=recruiter_user_id,
                    organization_id=organization_id,
                    requested_organization_id=organization_id,
                    action="revoke",
                    resource_type="candidate_invitation",
                    resource_id=invitation_id,
                )
            )

    @classmethod
    def revoke_invitation(
        cls,
        invitation_id: str,
        recruiter_user_id: str,
        role: str,
        organization_id: str | None = None,
    ) -> None:
        if role != "recruiter" or not organization_id:
            raise InvitationOperationError(403, "Recruiter membership required")
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            invitation = session.execute(
                select(CandidateInvitationRecord)
                .join(
                    OrganizationMembershipRecord,
                    (OrganizationMembershipRecord.organization_id
                     == CandidateInvitationRecord.organization_id)
                    & (OrganizationMembershipRecord.user_id
                       == CandidateInvitationRecord.recruiter_user_id),
                )
                .where(
                    CandidateInvitationRecord.id == invitation_id,
                    CandidateInvitationRecord.organization_id == organization_id,
                    CandidateInvitationRecord.recruiter_user_id == recruiter_user_id,
                    CandidateInvitationRecord.consumed_at.is_(None),
                    CandidateInvitationRecord.revoked_at.is_(None),
                    OrganizationMembershipRecord.role == "recruiter",
                    OrganizationMembershipRecord.status == "active",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if invitation is None:
                raise InvitationOperationError(404, UNAVAILABLE_MESSAGE)
            invitation.revoked_at = now
            session.add(
                AccessAuditRecord(
                    actor_user_id=recruiter_user_id,
                    organization_id=organization_id,
                    requested_organization_id=organization_id,
                    action="revoke",
                    resource_type="candidate_invitation",
                    resource_id=invitation.id,
                )
            )

    @classmethod
    def redeem(
        cls,
        token: str,
        candidate_user_id: str,
        requested_organization_id: str | None = None,
    ) -> dict[str, str]:
        now = datetime.now(UTC)
        invalid = False
        rate_limited = False
        linked_candidate_id: str | None = None
        try:
            with SessionLocal.begin() as session:
                locked_user_id = session.execute(
                    select(UserRecord.id)
                    .where(UserRecord.id == candidate_user_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if locked_user_id is None:
                    invalid = True
                else:
                    recent_attempts = session.execute(
                        select(func.count(AccessAuditRecord.id)).where(
                            AccessAuditRecord.actor_user_id == candidate_user_id,
                            AccessAuditRecord.action
                            == "candidate_invitation_redemption_attempt",
                            AccessAuditRecord.created_at
                            >= now - INVITATION_ATTEMPT_WINDOW,
                        )
                    ).scalar_one()
                    if recent_attempts >= MAX_REDEMPTION_ATTEMPTS_PER_WINDOW:
                        rate_limited = True
                    else:
                        session.add(
                            AccessAuditRecord(
                                actor_user_id=candidate_user_id,
                                action="candidate_invitation_redemption_attempt",
                                resource_type="user",
                                resource_id=candidate_user_id,
                            )
                        )
                        linked_candidate_id = cls._attempt_link(
                            session,
                            token,
                            candidate_user_id,
                            now,
                            requested_organization_id,
                        )
                        invalid = linked_candidate_id is None
        except IntegrityError as exc:
            raise InvitationOperationError(400, UNAVAILABLE_MESSAGE) from exc

        if rate_limited:
            raise InvitationOperationError(429, "Redemption is temporarily limited")
        if invalid or linked_candidate_id is None:
            raise InvitationOperationError(400, UNAVAILABLE_MESSAGE)
        return {"candidate_id": linked_candidate_id}

    @classmethod
    def verify_email_out_of_band(
        cls, user_id: str, admin_user_id: str, role: str
    ) -> dict[str, Any]:
        if role != "admin":
            raise InvitationOperationError(403, "Admin role required")
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            target = session.get(UserRecord, user_id)
            if target is None:
                raise InvitationOperationError(404, "User not found")
            if target.email_verified_at is not None:
                raise InvitationOperationError(409, "Email is already verified")
            target.email_verified_at = now
            session.add(
                AccessAuditRecord(
                    actor_user_id=admin_user_id,
                    action="verify_email_out_of_band",
                    resource_type="user",
                    resource_id=user_id,
                )
            )
            return {"user_id": user_id, "email_verified_at": now}
