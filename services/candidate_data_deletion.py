"""Authorized, tenant-scoped erasure of a candidate's personal data.

Scope ("candidate-erasure-v1"); see docs/CANDIDATE_DATA_DELETION.md for the full
inventory and the decisions that still need product-owner/legal approval.

Erased (deleted or overwritten with a fixed placeholder):
- the original CV files in private CV storage;
- report exports of the candidate's interviews in this organization
  (``STORAGE_PATH/reports/<org>/<interview>/`` and legacy
  ``STORAGE_PATH/report_<uuid>.json|txt`` files whose header names one of
  those interviews); new exports of an erased interview are refused (410);
- candidate profile name, email, phone and resume URL, and the link to the
  candidate's user account (the candidate-role membership in this organization
  is revoked; the global user account is not deleted because it may belong to
  other organizations);
- candidate invitation name and email;
- extracted CV text, parsed CV output and the CV content hash;
- screening evidence quotes, explanations, uncertainty notes and review notes;
- application failure messages;
- communication recipient, subject, body and template snapshot;
- candidate-supplied availability windows;
- interview answers, evidence quotes, assessment strengths/gaps/rationale,
  generated question text and the free-text parts of the interview state;
- free-text hiring outcome reasons.

Retained as a non-PII tombstone (pending policy approval): row identifiers,
statuses, timestamps, numeric scores, hiring outcome values (campaign headcount
and target-achievement counts depend on them), schedule time ranges, event
rows and access-audit rows. Rows are not deleted because composite foreign keys
chain applications, communications, availability, schedules, interviews and
hiring outcomes; redacting in place keeps every constraint intact.

The operation is ordered so that "completed" is never reported while a CV file
or report export remains: (1) one transaction redacts the database and records
a ``pending`` audit row, (2) CV files and report exports are deleted, (3) a
final audit row records ``completed`` only when every file is verifiably gone,
otherwise
``deletion_pending``. Re-invoking the operation is safe and finishes any
pending work.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, update

from config import settings
from services.candidate_cv_storage import CandidateCVStorage
from services.database import (
    AccessAuditRecord,
    CandidateAvailabilityRequestRecord,
    CandidateAvailabilityResponseRecord,
    CandidateCommunicationRecord,
    CandidateCriterionAssessmentRecord,
    CandidateCVDocumentRecord,
    CandidateInvitationRecord,
    CandidateRecord,
    CandidateScreeningRecord,
    InterviewAssessmentRecord,
    InterviewEvidenceRecord,
    InterviewQuestionRecord,
    InterviewRecord,
    InterviewScheduleRecord,
    InterviewTurnRecord,
    OrganizationMembershipRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignHiringOutcomeRecord,
    SessionLocal,
)
from services.report_service import delete_candidate_report_exports
from services.tenant_authorization import TenantContext

logger = logging.getLogger(__name__)

ERASURE_SCOPE_VERSION = "candidate-erasure-v1"
ERASED_TEXT = "[erased]"
ERASED_EMAIL_DOMAIN = "erased.invalid"
ERASED_SHA256 = "0" * 64
ERASED_FAILURE_CODE = "candidate_data_erased"
AUTOMATED_ERASURE_ACTOR = "system:retention"

# Interview-state keys that carry no candidate-authored content. Everything
# else (history, answers, extracted evidence, agent reasoning, generated
# questions, per-skill evidence) is dropped.
_RETAINED_INTERVIEW_STATE_KEYS = frozenset(
    {
        "interview_id",
        "candidate_id",
        "organization_id",
        "campaign_id",
        "application_id",
        "schedule_id",
        "objective_version",
        "plan_version",
        "rubric_snapshot",
        "position",
        "mode",
        "status",
        "overall_score",
        "difficulty",
        "current_competency",
        "interview_plan",
        "question_budget",
        "question_source",
        "started_at",
        "created_at",
        "updated_at",
    }
)

_OPEN_APPLICATION_STATUSES = ("pending", "retryable_failure", "running")
_CANCELLABLE_COMMUNICATION_STATUSES = (
    "draft",
    "awaiting_approval",
    "approved",
    "retryable_failure",
)
_OPEN_AVAILABILITY_STATUSES = ("pending_response", "responded", "reviewed")
_IN_FLIGHT_SCHEDULE_STATUSES = ("proposed", "pending_confirmation", "reschedule_pending")


class CandidateErasureBlocked(Exception):
    """Erasure cannot run now because candidate work is in flight."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class CandidateErasureResult:
    candidate_id: str
    status: str
    already_erased: bool
    pending_file_count: int
    redacted_records: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.status == "completed"

    def as_response(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status,
            "already_erased": self.already_erased,
            "retryable": not self.complete,
            "pending_file_count": self.pending_file_count,
            "scope_version": ERASURE_SCOPE_VERSION,
            "redacted_records": self.redacted_records,
        }


def erased_candidate_email(candidate_id: str) -> str:
    return f"erased-{candidate_id}@{ERASED_EMAIL_DOMAIN}"


class CandidateDataDeletionService:
    @classmethod
    def erase_personal_data(
        cls, candidate_id: str, context: TenantContext
    ) -> CandidateErasureResult:
        cls._authorize(context)
        try:
            storage_keys, interview_ids, already_erased, counts = cls._redact_database(
                candidate_id, context
            )
        except CandidateErasureBlocked as blocked:
            cls._audit(context, candidate_id, "candidate_personal_data_erasure_blocked", "blocked")
            logger.info(
                "Candidate erasure blocked",
                extra={
                    "candidate_id": candidate_id,
                    "organization_id": context.organization_id,
                    "blocked_reason": blocked.reason,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "candidate_erasure_blocked",
                    "reason": blocked.reason,
                },
            ) from blocked

        pending_files, files_found = cls._delete_files(storage_keys)
        pending_exports, exports_found = cls._delete_report_exports(
            context.organization_id, candidate_id, interview_ids
        )
        pending_files += pending_exports
        files_found += exports_found
        result = CandidateErasureResult(
            candidate_id=candidate_id,
            status="completed" if pending_files == 0 else "deletion_pending",
            # True only when a previous request had already finished everything.
            already_erased=already_erased and files_found == 0,
            pending_file_count=pending_files,
            redacted_records=counts,
        )
        if result.complete:
            cls._audit(context, candidate_id, "candidate_personal_data_erasure_completed", "completed")
        else:
            cls._audit(context, candidate_id, "candidate_personal_data_erasure_incomplete", "partial")
        logger.info(
            "Candidate erasure finished",
            extra={
                "candidate_id": candidate_id,
                "organization_id": context.organization_id,
                "erasure_status": result.status,
                "pending_file_count": pending_files,
            },
        )
        return result

    @classmethod
    def erase_personal_data_automated(
        cls, candidate_id: str, organization_id: str
    ) -> CandidateErasureResult:
        """Erase on behalf of an automated, server-side job (retention rules).

        Never reachable from HTTP: the request path always builds its context
        from ``tenant_context``. Audit rows carry no actor (``actor_user_id``
        NULL) because no person initiated the erasure; the caller records its
        own audit event with the rule that triggered it.
        """
        context = TenantContext(
            actor_user_id=AUTOMATED_ERASURE_ACTOR,
            organization_id=organization_id,
            role="org_admin",
            is_automated=True,
        )
        return cls.erase_personal_data(candidate_id, context)

    @staticmethod
    def _authorize(context: TenantContext) -> None:
        # Only the organization's own administrator may erase. Platform support
        # (role "platform_admin") is denied until product/legal approve a
        # support-initiated erasure path; the support preflight still audits it.
        if context.role != "org_admin" or context.is_platform_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Candidate data erasure requires an organization administrator",
            )

    @classmethod
    def _redact_database(
        cls, candidate_id: str, context: TenantContext
    ) -> tuple[list[str], list[str], bool, dict[str, int]]:
        organization_id = context.organization_id
        now = datetime.now(UTC)
        counts: dict[str, int] = {}
        with SessionLocal.begin() as session:
            candidate = session.execute(
                select(CandidateRecord)
                .where(
                    CandidateRecord.id == candidate_id,
                    CandidateRecord.organization_id == organization_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if candidate is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found"
                )
            if candidate.legal_hold:
                # Legal hold always wins over erasure (manual or automated).
                raise CandidateErasureBlocked("legal_hold")
            already_erased = candidate.email == erased_candidate_email(candidate.id)

            applications = list(
                session.execute(
                    select(RecruitmentApplicationRecord).where(
                        RecruitmentApplicationRecord.organization_id == organization_id,
                        RecruitmentApplicationRecord.candidate_id == candidate.id,
                    )
                ).scalars()
            )
            application_ids = [item.id for item in applications]
            interviews = list(
                session.execute(
                    select(InterviewRecord).where(
                        InterviewRecord.organization_id == organization_id,
                        InterviewRecord.candidate_id == candidate.id,
                    )
                ).scalars()
            )
            interview_ids = [item.id for item in interviews]

            cls._assert_not_in_flight(
                session, organization_id, candidate.id, applications, application_ids,
                interview_ids, now,
            )

            # Candidate profile and account link.
            linked_user_id = candidate.user_id
            candidate.name = ERASED_TEXT
            candidate.email = erased_candidate_email(candidate.id)
            candidate.phone = None
            candidate.resume_url = None
            candidate.user_id = None
            candidate.updated_at = now
            counts["candidates"] = 1
            if linked_user_id is not None:
                counts["memberships_revoked"] = session.execute(
                    update(OrganizationMembershipRecord)
                    .where(
                        OrganizationMembershipRecord.organization_id == organization_id,
                        OrganizationMembershipRecord.user_id == linked_user_id,
                        OrganizationMembershipRecord.role == "candidate",
                        OrganizationMembershipRecord.status == "active",
                    )
                    .values(status="revoked", revoked_at=now)
                ).rowcount

            invitations = list(
                session.execute(
                    select(CandidateInvitationRecord).where(
                        CandidateInvitationRecord.organization_id == organization_id,
                        CandidateInvitationRecord.candidate_id == candidate.id,
                    )
                ).scalars()
            )
            for invitation in invitations:
                invitation.candidate_name = None
                invitation.email = f"erased-{invitation.id}@{ERASED_EMAIL_DOMAIN}"
                if invitation.consumed_at is None and invitation.revoked_at is None:
                    invitation.revoked_at = now
            counts["invitations"] = len(invitations)

            storage_keys: list[str] = []
            if application_ids:
                storage_keys = cls._redact_applications(
                    session, organization_id, applications, application_ids, now, counts
                )
            if interview_ids:
                cls._redact_interviews(session, interviews, interview_ids, counts)

            session.add(
                cls._audit_record(
                    context, candidate.id, "candidate_personal_data_erasure_started", "pending"
                )
            )
        return storage_keys, interview_ids, already_erased, counts

    @staticmethod
    def _has_outstanding_confirmed_slot(
        session, organization_id: str, candidate_id: str, now: datetime
    ) -> bool:
        """A confirmed, not-yet-ended slot is active work only while its interview
        is outstanding: campaign interviews may be taken before the slot, and a
        completed or decided one must not block erasure."""
        for schedule_id, application_id in session.execute(
            select(InterviewScheduleRecord.id, InterviewScheduleRecord.application_id).where(
                InterviewScheduleRecord.organization_id == organization_id,
                InterviewScheduleRecord.candidate_id == candidate_id,
                InterviewScheduleRecord.status == "confirmed_internal",
                InterviewScheduleRecord.end_at > now,
            )
        ).all():
            if not CandidateDataDeletionService._schedule_work_is_finished(
                session, organization_id, schedule_id, application_id
            ):
                return True
        return False

    @staticmethod
    def _schedule_work_is_finished(
        session, organization_id: str, schedule_id: str, application_id: str | None
    ) -> bool:
        """True when the slot's interview completed or a hiring outcome exists."""
        if application_id is not None and session.execute(
            select(RecruitmentCampaignHiringOutcomeRecord.id).where(
                RecruitmentCampaignHiringOutcomeRecord.organization_id == organization_id,
                RecruitmentCampaignHiringOutcomeRecord.application_id == application_id,
            ).limit(1)
        ).scalar_one_or_none() is not None:
            return True
        state_json = session.execute(
            select(InterviewRecord.state_json).where(
                InterviewRecord.organization_id == organization_id,
                InterviewRecord.schedule_id == schedule_id,
            ).limit(1)
        ).scalar_one_or_none()
        if state_json is None:
            return False
        try:
            return json.loads(state_json).get("status") == "completed"
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _assert_not_in_flight(
        session,
        organization_id: str,
        candidate_id: str,
        applications: list[RecruitmentApplicationRecord],
        application_ids: list[str],
        interview_ids: list[str],
        now: datetime,
    ) -> None:
        for application in applications:
            lease = application.lease_expires_at
            if lease is not None and lease.tzinfo is None:
                lease = lease.replace(tzinfo=UTC)
            if application.status == "running" and lease is not None and lease > now:
                raise CandidateErasureBlocked("cv_processing_in_progress")
        if application_ids:
            sending = session.execute(
                select(CandidateCommunicationRecord.id).where(
                    CandidateCommunicationRecord.organization_id == organization_id,
                    CandidateCommunicationRecord.application_id.in_(application_ids),
                    CandidateCommunicationRecord.status == "sending",
                ).limit(1)
            ).scalar_one_or_none()
            if sending is not None:
                raise CandidateErasureBlocked("communication_send_in_progress")
            pending_schedule = session.execute(
                select(InterviewScheduleRecord.id).where(
                    InterviewScheduleRecord.organization_id == organization_id,
                    InterviewScheduleRecord.candidate_id == candidate_id,
                    InterviewScheduleRecord.status.in_(_IN_FLIGHT_SCHEDULE_STATUSES),
                ).limit(1)
            ).scalar_one_or_none()
            if pending_schedule is not None:
                raise CandidateErasureBlocked("interview_schedule_active")
            if CandidateDataDeletionService._has_outstanding_confirmed_slot(
                session, organization_id, candidate_id, now
            ):
                raise CandidateErasureBlocked("interview_schedule_active")
        if interview_ids:
            fresh_after = now - timedelta(seconds=settings.TURN_PROCESSING_LEASE_SECONDS)
            in_flight = session.execute(
                select(InterviewTurnRecord.id).where(
                    InterviewTurnRecord.interview_id.in_(interview_ids),
                    InterviewTurnRecord.status.in_(("received", "processing")),
                    InterviewTurnRecord.updated_at > fresh_after,
                ).limit(1)
            ).scalar_one_or_none()
            if in_flight is not None:
                raise CandidateErasureBlocked("interview_turn_in_progress")

    @staticmethod
    def _redact_applications(
        session,
        organization_id: str,
        applications: list[RecruitmentApplicationRecord],
        application_ids: list[str],
        now: datetime,
        counts: dict[str, int],
    ) -> list[str]:
        for application in applications:
            application.failure_message = None
            if application.status in _OPEN_APPLICATION_STATUSES:
                # Stop any further CV processing; the worker's lease-fenced
                # writes can no longer match this row.
                application.status = "failed"
                application.failure_code = ERASED_FAILURE_CODE
                application.lease_token = None
                application.lease_expires_at = None
                application.available_at = None
            application.updated_at = now
        counts["applications"] = len(applications)

        documents = list(
            session.execute(
                select(CandidateCVDocumentRecord).where(
                    CandidateCVDocumentRecord.organization_id == organization_id,
                    CandidateCVDocumentRecord.application_id.in_(application_ids),
                )
            ).scalars()
        )
        for document in documents:
            document.extracted_text = None
            document.parsed_output_json = None
            document.sha256 = ERASED_SHA256
        counts["cv_documents"] = len(documents)

        screening_ids = list(
            session.execute(
                select(CandidateScreeningRecord.id).where(
                    CandidateScreeningRecord.organization_id == organization_id,
                    CandidateScreeningRecord.application_id.in_(application_ids),
                )
            ).scalars()
        )
        if screening_ids:
            counts["screening_assessments"] = session.execute(
                update(CandidateCriterionAssessmentRecord)
                .where(CandidateCriterionAssessmentRecord.screening_id.in_(screening_ids))
                .values(
                    evidence_json="[]",
                    explanation=ERASED_TEXT,
                    uncertainty=ERASED_TEXT,
                    review_note=None,
                )
            ).rowcount

        communications = list(
            session.execute(
                select(CandidateCommunicationRecord).where(
                    CandidateCommunicationRecord.organization_id == organization_id,
                    CandidateCommunicationRecord.application_id.in_(application_ids),
                )
            ).scalars()
        )
        for communication in communications:
            communication.recipient_email = (
                f"erased-{communication.id}@{ERASED_EMAIL_DOMAIN}"
            )
            communication.subject = ERASED_TEXT
            communication.body = ERASED_TEXT
            communication.template_snapshot_json = "{}"
            if communication.status in _CANCELLABLE_COMMUNICATION_STATUSES:
                communication.status = "cancelled"
                communication.lease_token = None
                communication.lease_expires_at = None
                communication.available_at = None
            communication.updated_at = now
        counts["communications"] = len(communications)

        requests = list(
            session.execute(
                select(CandidateAvailabilityRequestRecord).where(
                    CandidateAvailabilityRequestRecord.organization_id == organization_id,
                    CandidateAvailabilityRequestRecord.application_id.in_(application_ids),
                )
            ).scalars()
        )
        for request in requests:
            if request.status in _OPEN_AVAILABILITY_STATUSES:
                request.status = "cancelled"
                request.updated_at = now
        if requests:
            counts["availability_responses"] = session.execute(
                update(CandidateAvailabilityResponseRecord)
                .where(
                    CandidateAvailabilityResponseRecord.organization_id == organization_id,
                    CandidateAvailabilityResponseRecord.application_id.in_(application_ids),
                )
                .values(windows_json="[]", updated_at=now)
            ).rowcount
        counts["availability_requests"] = len(requests)

        counts["hiring_outcomes"] = session.execute(
            update(RecruitmentCampaignHiringOutcomeRecord)
            .where(
                RecruitmentCampaignHiringOutcomeRecord.organization_id == organization_id,
                RecruitmentCampaignHiringOutcomeRecord.application_id.in_(application_ids),
            )
            .values(reason=ERASED_TEXT)
        ).rowcount
        return [document.storage_key for document in documents]

    @staticmethod
    def _redact_interviews(
        session,
        interviews: list[InterviewRecord],
        interview_ids: list[str],
        counts: dict[str, int],
    ) -> None:
        for interview in interviews:
            try:
                state = json.loads(interview.state_json)
            except (TypeError, ValueError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            redacted = {
                key: value
                for key, value in state.items()
                if key in _RETAINED_INTERVIEW_STATE_KEYS
            }
            redacted["current_question"] = ERASED_TEXT
            # A completed status makes the interview service reject new turns,
            # so no new candidate content can be appended after erasure.
            redacted["status"] = "completed"
            redacted["personal_data_erased"] = True
            redacted["erasure_scope_version"] = ERASURE_SCOPE_VERSION
            interview.state_json = json.dumps(redacted)
            interview.current_question = ERASED_TEXT
        counts["interviews"] = len(interviews)
        counts["interview_turns"] = session.execute(
            update(InterviewTurnRecord)
            .where(
                InterviewTurnRecord.interview_id.in_(interview_ids),
                InterviewTurnRecord.candidate_answer.is_not(None),
            )
            .values(candidate_answer=ERASED_TEXT)
        ).rowcount
        counts["interview_questions"] = session.execute(
            update(InterviewQuestionRecord)
            .where(InterviewQuestionRecord.interview_id.in_(interview_ids))
            .values(question_text=ERASED_TEXT)
        ).rowcount
        counts["interview_evidence"] = session.execute(
            update(InterviewEvidenceRecord)
            .where(InterviewEvidenceRecord.interview_id.in_(interview_ids))
            .values(evidence_text=ERASED_TEXT)
        ).rowcount
        counts["interview_assessments"] = session.execute(
            update(InterviewAssessmentRecord)
            .where(InterviewAssessmentRecord.interview_id.in_(interview_ids))
            .values(strengths_json="[]", gaps_json="[]", rationale=ERASED_TEXT)
        ).rowcount

    @staticmethod
    def _delete_files(storage_keys: list[str]) -> tuple[int, int]:
        """Delete CV files; return (files still present, files found before deletion)."""
        pending = 0
        found = 0
        for storage_key in storage_keys:
            try:
                if CandidateCVStorage.exists(storage_key):
                    found += 1
            except (OSError, RuntimeError):
                found += 1
            try:
                CandidateCVStorage.delete(storage_key)
            except (OSError, RuntimeError) as exc:
                logger.warning(
                    "Candidate CV file deletion failed; erasure remains pending",
                    extra={"failure_type": type(exc).__name__},
                )
            try:
                still_present = CandidateCVStorage.exists(storage_key)
            except (OSError, RuntimeError):
                still_present = True
            if still_present:
                pending += 1
        return pending, found

    @staticmethod
    def _delete_report_exports(
        organization_id: str, candidate_id: str, interview_ids: list[str]
    ) -> tuple[int, int]:
        """Delete report exports; return (exports still present, exports found)."""
        try:
            return delete_candidate_report_exports(
                organization_id, candidate_id, interview_ids
            )
        except OSError as exc:
            # Storage could not be inspected: completion cannot be verified.
            logger.warning(
                "Report export deletion failed; erasure remains pending",
                extra={"failure_type": type(exc).__name__},
            )
            return 1, 1

    @staticmethod
    def _audit_record(
        context: TenantContext, candidate_id: str, action: str, outcome: str
    ) -> AccessAuditRecord:
        # Identifiers only: never names, emails, CV text or answers.
        return AccessAuditRecord(
            actor_user_id=None if context.is_automated else context.actor_user_id,
            organization_id=context.organization_id,
            requested_organization_id=context.organization_id,
            action=action,
            resource_type="candidate",
            resource_id=candidate_id,
            outcome=outcome,
            support_reason=context.support_reason,
        )

    @classmethod
    def _audit(
        cls, context: TenantContext, candidate_id: str, action: str, outcome: str
    ) -> None:
        with SessionLocal.begin() as session:
            session.add(cls._audit_record(context, candidate_id, action, outcome))
