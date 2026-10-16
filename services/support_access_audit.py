"""Fail-closed audit preflight for privileged and report-export requests."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from services.auth_service import decode_token
from services.database import (
    AccessAuditRecord,
    OrganizationRecord,
    SessionLocal,
)
from services.user_store import UserStore
from utils import get_logger

logger = get_logger(__name__)

_AUDITED_PREFIXES = (
    "/api/v1/candidates",
    "/api/v1/candidate-invitations",
    "/api/v1/recruitment-campaigns",
    "/api/v1/recruitment-campaign-executions",
    "/api/v1/interview",
    "/api/v1/interviews",
)


class MandatoryAuditUnavailable(RuntimeError):
    """A required audit preflight could not be committed."""


class DuplicateAuditRequest(RuntimeError):
    """An idempotency key was already used for a protected request."""


def audit_request_id(
    request: Request,
    actor_user_id: str,
    organization_id: str | None = None,
) -> str:
    key = request.headers.get("idempotency-key")
    if key is None:
        return str(uuid4())
    if not key.strip() or len(key) > 256 or "\r" in key or "\n" in key:
        raise ValueError("Invalid Idempotency-Key")
    material = "\0".join(
        (
            actor_user_id,
            request.method.upper(),
            request.url.path,
            organization_id or "",
            key,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def persist_required_audit(
    *,
    request_id: str,
    actor_user_id: str,
    organization_id: str | None,
    requested_organization_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str,
    support_reason: str | None = None,
) -> None:
    try:
        with SessionLocal.begin() as session:
            existing = session.execute(
                select(AccessAuditRecord.id).where(AccessAuditRecord.request_id == request_id)
            ).scalar_one_or_none()
            if existing is not None:
                raise DuplicateAuditRequest()
            persisted_organization_id = None
            if organization_id is not None:
                persisted_organization_id = session.execute(
                    select(OrganizationRecord.id).where(OrganizationRecord.id == organization_id)
                ).scalar_one_or_none()
            session.add(
                AccessAuditRecord(
                    actor_user_id=actor_user_id,
                    organization_id=persisted_organization_id,
                    requested_organization_id=requested_organization_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    outcome="attempted",
                    support_reason=support_reason,
                    request_id=request_id,
                )
            )
    except DuplicateAuditRequest:
        raise
    except SQLAlchemyError as exc:
        logger.error(
            "Mandatory access audit preflight failed",
            extra={
                "audit_action": action,
                "failure_type": type(exc).__name__,
            },
        )
        raise MandatoryAuditUnavailable() from exc


async def audit_platform_support_access(request: Request, call_next) -> Response:
    path = request.url.path
    if not _is_audited_path(path):
        return await call_next(request)

    user = _platform_admin_for_request(request)
    if user is None:
        return await call_next(request)

    resource_type, resource_id = _resource_from_path(path)
    requested_organization_id = _requested_organization(request)
    reason = _support_reason(request)
    action = _support_action(path, resource_type)
    try:
        request_id = audit_request_id(
            request,
            user["id"],
            requested_organization_id,
        )
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid Idempotency-Key"},
        )
    try:
        persist_required_audit(
            request_id=request_id,
            actor_user_id=user["id"],
            organization_id=requested_organization_id,
            requested_organization_id=requested_organization_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            support_reason=reason,
        )
    except DuplicateAuditRequest:
        return JSONResponse(
            status_code=409,
            content={"detail": "This protected request was already attempted"},
        )
    except MandatoryAuditUnavailable:
        return JSONResponse(
            status_code=503,
            content={"detail": "Required access audit is temporarily unavailable"},
        )

    request.state.mandatory_audit_request_id = request_id
    request.state.mandatory_audit_action = action
    return await call_next(request)


def _is_audited_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _AUDITED_PREFIXES)


def _platform_admin_for_request(request: Request) -> dict | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        claims = decode_token(token)
    except HTTPException:
        return None
    user = UserStore.get_by_email(str(claims.get("sub", "")))
    return user if user is not None and user["role"] == "admin" else None


def _requested_organization(request: Request) -> str | None:
    organization_id = request.headers.get("x-organization-id", "").strip()
    return organization_id if 0 < len(organization_id) <= 256 else None


def _support_reason(request: Request) -> str | None:
    reason = request.headers.get("x-support-reason", "").strip()
    return reason if 0 < len(reason) <= 500 and "\r" not in reason and "\n" not in reason else None


def _support_action(path: str, resource_type: str) -> str:
    return (
        "report_export_attempt"
        if resource_type == "interview" and path.endswith("/report")
        else "support_access_attempt"
    )


def _resource_from_path(path: str) -> tuple[str, str]:
    parts = [part for part in path.split("/") if part]
    for collection, resource_type in (
        ("interview", "interview"),
        ("interviews", "interview"),
        ("candidates", "candidate"),
        ("candidate-invitations", "candidate_invitation"),
        ("recruitment-campaigns", "recruitment_campaign"),
        ("recruitment-campaign-executions", "recruitment_campaign_execution"),
    ):
        if collection in parts:
            index = parts.index(collection)
            resource_id = parts[index + 1][:128] if len(parts) > index + 1 else "collection"
            return resource_type, resource_id
    return "unknown", "collection"
