"""Policy-neutral candidate data retention: configurable rules, preview, erasure.

No retention period or rule is built in. Rules come only from
``settings.RETENTION_RULES_JSON``; when it is unset or empty nothing is ever
selected. The product/legal decisions this module deliberately leaves open are
listed in docs/RETENTION_POLICY_DECISIONS.md.

Rule format (JSON list)::

    [{"id": "unsuccessful-default",
      "category": "unsuccessful_candidate",
      "age_basis": "hiring_outcome_decided_at",
      "min_age_days": <int from configuration>,
      "organization_ids": ["<org id>"]}]      # optional; omitted = every org

Categories (which candidates a rule may select):
- ``unsuccessful_candidate``: at least one application, every application has
  a hiring outcome, and every outcome is ``rejected`` or ``withdrawn``.
- ``candidate_without_hire``: no ``hired`` outcome in the organization (open or
  unresolved applications included).

Age bases (which timestamp must be older than ``min_age_days``):
- ``hiring_outcome_decided_at``: latest hiring-outcome decision.
- ``application_last_activity``: latest application update or outcome decision.

Every evaluation is scoped to one organization. Candidates under legal hold
and candidates whose erasure already completed are excluded. Candidates whose
retention erasure started but left files behind are retried.

Default runs are dry runs (counts and candidate ids only, never names, emails
or text). Erasure runs only when ``RETENTION_EXECUTION_ENABLED`` is true, at
least one rule is configured, and the caller passes ``execute=True``
(CLI: ``--execute``). No scheduler invokes this module.

CLI::

    python -m services.retention_policy --organization-id <org> [--execute]
    python -m services.retention_policy --all-organizations [--execute]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select

from config import settings
from services.candidate_data_deletion import (
    CandidateDataDeletionService,
    erased_candidate_email,
)
from services.database import (
    AccessAuditRecord,
    CandidateRecord,
    OrganizationRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignHiringOutcomeRecord,
    SessionLocal,
)
from services.tenant_authorization import TenantContext

logger = logging.getLogger(__name__)

RETENTION_SCOPE_VERSION = "retention-v1"
CATEGORIES = frozenset({"unsuccessful_candidate", "candidate_without_hire"})
AGE_BASES = frozenset({"hiring_outcome_decided_at", "application_last_activity"})
_UNSUCCESSFUL_OUTCOMES = frozenset({"rejected", "withdrawn"})
_RULE_KEYS = frozenset({"id", "category", "age_basis", "min_age_days", "organization_ids"})
_REQUIRED_RULE_KEYS = frozenset({"id", "category", "age_basis", "min_age_days"})
_RULE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_AUDIT_REASON_PREFIX = "retention_rule:"
_LEGAL_HOLD_REASON_MAX = 500


class RetentionConfigError(ValueError):
    """The configured retention rules are invalid; nothing is evaluated."""


class RetentionExecutionDisabled(RuntimeError):
    """Destructive execution was requested but is not enabled."""


@dataclass(frozen=True)
class RetentionRule:
    id: str
    category: str
    age_basis: str
    min_age_days: int
    organization_ids: tuple[str, ...] | None = None

    def applies_to(self, organization_id: str) -> bool:
        return self.organization_ids is None or organization_id in self.organization_ids


def load_rules(raw: str | None = None, *, use_settings: bool = True) -> list[RetentionRule]:
    """Parse and validate rules. ``None``/blank configuration means no rules."""
    if raw is None and use_settings:
        raw = settings.RETENTION_RULES_JSON
    if raw is None or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RetentionConfigError("RETENTION_RULES_JSON is not valid JSON") from exc
    if not isinstance(data, list):
        raise RetentionConfigError("RETENTION_RULES_JSON must be a JSON list")
    rules = [_parse_rule(index, item) for index, item in enumerate(data)]
    if len({rule.id for rule in rules}) != len(rules):
        raise RetentionConfigError("RETENTION_RULES_JSON contains duplicate rule ids")
    return rules


def _parse_rule(index: int, item: Any) -> RetentionRule:
    where = f"rule[{index}]"
    if not isinstance(item, dict):
        raise RetentionConfigError(f"{where} must be an object")
    unknown = set(item) - _RULE_KEYS
    missing = _REQUIRED_RULE_KEYS - set(item)
    if unknown or missing:
        raise RetentionConfigError(
            f"{where}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}"
        )
    rule_id = item["id"]
    if not isinstance(rule_id, str) or not _RULE_ID.match(rule_id):
        raise RetentionConfigError(f"{where}: id must match {_RULE_ID.pattern}")
    if item["category"] not in CATEGORIES:
        raise RetentionConfigError(f"{where}: category must be one of {sorted(CATEGORIES)}")
    if item["age_basis"] not in AGE_BASES:
        raise RetentionConfigError(f"{where}: age_basis must be one of {sorted(AGE_BASES)}")
    days = item["min_age_days"]
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise RetentionConfigError(f"{where}: min_age_days must be a positive integer")
    return RetentionRule(
        id=rule_id,
        category=item["category"],
        age_basis=item["age_basis"],
        min_age_days=days,
        organization_ids=_parse_organization_ids(where, item.get("organization_ids")),
    )


def _parse_organization_ids(where: str, org_ids: Any) -> tuple[str, ...] | None:
    if org_ids is None:
        return None
    if (
        not isinstance(org_ids, list)
        or not org_ids
        or not all(isinstance(value, str) and value for value in org_ids)
    ):
        raise RetentionConfigError(f"{where}: organization_ids must be a non-empty list of ids")
    return tuple(org_ids)


@dataclass
class RuleSelection:
    rule_id: str
    candidate_ids: list[str] = field(default_factory=list)
    excluded_legal_hold: int = 0
    excluded_already_erased: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "selected_count": len(self.candidate_ids),
            "excluded_legal_hold": self.excluded_legal_hold,
            "excluded_already_erased": self.excluded_already_erased,
            "candidate_ids": list(self.candidate_ids),
        }


@dataclass
class RetentionReport:
    """Identifiers and counts only; never names, emails or free text."""

    organization_id: str
    mode: str
    run_id: str
    rules: list[RuleSelection] = field(default_factory=list)
    # candidate_id -> rule_id of a retention erasure that left files behind.
    pending_retry: dict[str, str] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    deferred_count: int = 0

    def planned(self) -> list[tuple[str, str]]:
        planned = list(self.pending_retry.items())
        for selection in self.rules:
            planned.extend((cid, selection.rule_id) for cid in selection.candidate_ids)
        return planned

    def as_dict(self) -> dict[str, Any]:
        summary: dict[str, int] = {}
        for item in self.results:
            summary[item["status"]] = summary.get(item["status"], 0) + 1
        return {
            "scope_version": RETENTION_SCOPE_VERSION,
            "organization_id": self.organization_id,
            "mode": self.mode,
            "run_id": self.run_id,
            "rules": [selection.as_dict() for selection in self.rules],
            "pending_retry_count": len(self.pending_retry),
            "pending_retry_candidate_ids": sorted(self.pending_retry),
            "results": list(self.results),
            "result_counts": summary,
            "deferred_count": self.deferred_count,
            "complete": all(item["status"] == "completed" for item in self.results)
            and self.deferred_count == 0,
        }


def _require_execution_enabled(rules: list[RetentionRule]) -> None:
    if not settings.RETENTION_EXECUTION_ENABLED:
        raise RetentionExecutionDisabled(
            "RETENTION_EXECUTION_ENABLED is false; only dry runs are allowed"
        )
    if not rules:
        raise RetentionExecutionDisabled("No retention rules are configured")


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class RetentionPolicyService:
    @classmethod
    def evaluate(
        cls,
        organization_id: str,
        rules: list[RetentionRule],
        *,
        now: datetime | None = None,
        mode: str = "dry_run",
    ) -> RetentionReport:
        """Select eligible candidates of one organization. Read-only."""
        now = now or datetime.now(UTC)
        report = RetentionReport(
            organization_id=organization_id, mode=mode, run_id=str(uuid4())
        )
        applicable = [rule for rule in rules if rule.applies_to(organization_id)]
        if not applicable:
            return report
        with SessionLocal() as session:
            candidates, applications, outcomes = cls._load_organization_state(
                session, organization_id
            )
            erased = [
                cid for cid, row in candidates.items()
                if row.email == erased_candidate_email(cid)
            ]
            completion = cls._erasure_completion(session, organization_id, erased)
            retention_started = cls._retention_rule_by_candidate(
                session, organization_id, erased
            )

        assigned: set[str] = set()
        for candidate_id in erased:
            if (
                not completion.get(candidate_id, False)
                and candidate_id in retention_started
                and not candidates[candidate_id].legal_hold
            ):
                report.pending_retry[candidate_id] = retention_started[candidate_id]
                assigned.add(candidate_id)

        for rule in applicable:
            selection = RuleSelection(rule_id=rule.id)
            cutoff = now - timedelta(days=rule.min_age_days)
            for candidate_id in sorted(set(candidates) - assigned):
                if not cls._matches(
                    rule,
                    applications.get(candidate_id, []),
                    outcomes.get(candidate_id, {}),
                    cutoff,
                ):
                    continue
                row = candidates[candidate_id]
                if row.legal_hold:
                    selection.excluded_legal_hold += 1
                elif row.email == erased_candidate_email(candidate_id):
                    # Completed, or erased by someone else and not ours to retry.
                    selection.excluded_already_erased += 1
                else:
                    selection.candidate_ids.append(candidate_id)
                    assigned.add(candidate_id)
            report.rules.append(selection)
        return report

    @staticmethod
    def _load_organization_state(session, organization_id: str):
        candidates = {
            row.id: row
            for row in session.execute(
                select(
                    CandidateRecord.id,
                    CandidateRecord.email,
                    CandidateRecord.legal_hold,
                ).where(CandidateRecord.organization_id == organization_id)
            ).all()
        }
        applications: dict[str, list[tuple[str, datetime | None]]] = {}
        for app_id, candidate_id, updated_at in session.execute(
            select(
                RecruitmentApplicationRecord.id,
                RecruitmentApplicationRecord.candidate_id,
                RecruitmentApplicationRecord.updated_at,
            ).where(RecruitmentApplicationRecord.organization_id == organization_id)
        ).all():
            applications.setdefault(candidate_id, []).append((app_id, _aware(updated_at)))
        outcomes: dict[str, dict[str, tuple[str, datetime | None]]] = {}
        for candidate_id, app_id, outcome, decided_at in session.execute(
            select(
                RecruitmentApplicationRecord.candidate_id,
                RecruitmentCampaignHiringOutcomeRecord.application_id,
                RecruitmentCampaignHiringOutcomeRecord.outcome,
                RecruitmentCampaignHiringOutcomeRecord.decided_at,
            )
            .join(
                RecruitmentApplicationRecord,
                (RecruitmentApplicationRecord.id
                 == RecruitmentCampaignHiringOutcomeRecord.application_id)
                & (RecruitmentApplicationRecord.organization_id
                   == RecruitmentCampaignHiringOutcomeRecord.organization_id),
            )
            .where(RecruitmentCampaignHiringOutcomeRecord.organization_id == organization_id)
        ).all():
            outcomes.setdefault(candidate_id, {})[app_id] = (outcome, _aware(decided_at))
        return candidates, applications, outcomes

    @staticmethod
    def _matches(
        rule: RetentionRule,
        applications: list[tuple[str, datetime | None]],
        outcomes: dict[str, tuple[str, datetime | None]],
        cutoff: datetime,
    ) -> bool:
        values = [outcome for outcome, _ in outcomes.values()]
        if rule.category == "unsuccessful_candidate":
            if not applications:
                return False
            if any(app_id not in outcomes for app_id, _ in applications):
                return False
            if not all(value in _UNSUCCESSFUL_OUTCOMES for value in values):
                return False
        elif rule.category == "candidate_without_hire":
            if "hired" in values:
                return False
        else:  # pragma: no cover - validated by load_rules
            return False

        decided = [at for _, at in outcomes.values() if at is not None]
        if rule.age_basis == "hiring_outcome_decided_at":
            stamps = decided
        else:
            stamps = decided + [at for _, at in applications if at is not None]
        if not stamps:
            return False
        return max(stamps) <= cutoff

    @staticmethod
    def _erasure_completion(
        session, organization_id: str, candidate_ids: list[str]
    ) -> dict[str, bool]:
        """For erased candidates: True when the latest erasure attempt completed."""
        if not candidate_ids:
            return {}
        latest: dict[str, dict[str, datetime]] = {}
        for resource_id, action, created_at in session.execute(
            select(
                AccessAuditRecord.resource_id,
                AccessAuditRecord.action,
                func.max(AccessAuditRecord.created_at),
            )
            .where(
                AccessAuditRecord.organization_id == organization_id,
                AccessAuditRecord.resource_type == "candidate",
                AccessAuditRecord.resource_id.in_(candidate_ids),
                AccessAuditRecord.action.in_(
                    (
                        "candidate_personal_data_erasure_started",
                        "candidate_personal_data_erasure_completed",
                        "candidate_personal_data_erasure_incomplete",
                    )
                ),
            )
            .group_by(AccessAuditRecord.resource_id, AccessAuditRecord.action)
        ).all():
            latest.setdefault(resource_id, {})[action] = _aware(created_at)
        result: dict[str, bool] = {}
        for candidate_id in candidate_ids:
            stamps = latest.get(candidate_id, {})
            completed = stamps.get("candidate_personal_data_erasure_completed")
            others = [
                stamps[key]
                for key in (
                    "candidate_personal_data_erasure_started",
                    "candidate_personal_data_erasure_incomplete",
                )
                if key in stamps
            ]
            result[candidate_id] = completed is not None and all(
                completed >= other for other in others
            )
        return result

    @staticmethod
    def _retention_rule_by_candidate(
        session, organization_id: str, candidate_ids: list[str]
    ) -> dict[str, str]:
        if not candidate_ids:
            return {}
        rows = session.execute(
            select(AccessAuditRecord.resource_id, AccessAuditRecord.support_reason)
            .where(
                AccessAuditRecord.organization_id == organization_id,
                AccessAuditRecord.resource_type == "candidate",
                AccessAuditRecord.resource_id.in_(candidate_ids),
                AccessAuditRecord.action == "retention_erasure_started",
            )
            .order_by(AccessAuditRecord.created_at)
        ).all()
        found: dict[str, str] = {}
        for resource_id, reason in rows:
            rule_id = (reason or "").removeprefix(_AUDIT_REASON_PREFIX)
            found[resource_id] = rule_id
        return found

    @classmethod
    def run(
        cls,
        organization_id: str,
        *,
        execute: bool = False,
        rules: list[RetentionRule] | None = None,
        now: datetime | None = None,
    ) -> RetentionReport:
        """Preview (default) or execute retention for one organization."""
        rules = load_rules() if rules is None else rules
        if execute:
            _require_execution_enabled(rules)
        report = cls.evaluate(
            organization_id, rules, now=now, mode="execute" if execute else "dry_run"
        )
        if not execute:
            cls._audit(
                organization_id,
                action="retention_preview",
                resource_type="retention_run",
                resource_id=report.run_id,
                outcome="dry_run",
                reason=_AUDIT_REASON_PREFIX + ",".join(rule.id for rule in rules)
                if rules else None,
            )
            logger.info(
                "Retention preview",
                extra={
                    "organization_id": organization_id,
                    "retention_run_id": report.run_id,
                    "selected_count": len(report.planned()),
                },
            )
            return report

        planned = report.planned()
        limit = settings.RETENTION_MAX_CANDIDATES_PER_RUN
        report.deferred_count = max(0, len(planned) - limit)
        for candidate_id, rule_id in planned[:limit]:
            report.results.append(cls._erase_one(organization_id, candidate_id, rule_id))
        logger.info(
            "Retention execution finished",
            extra={
                "organization_id": organization_id,
                "retention_run_id": report.run_id,
                "attempted_count": len(report.results),
                "deferred_count": report.deferred_count,
            },
        )
        return report

    @classmethod
    def _erase_one(cls, organization_id: str, candidate_id: str, rule_id: str) -> dict[str, Any]:
        reason = _AUDIT_REASON_PREFIX + rule_id
        cls._audit(organization_id, "retention_erasure_started", "candidate",
                   candidate_id, "pending", reason)
        result: dict[str, Any] = {"candidate_id": candidate_id, "rule_id": rule_id}
        try:
            erased = CandidateDataDeletionService.erase_personal_data_automated(
                candidate_id, organization_id
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if exc.status_code == status.HTTP_409_CONFLICT:
                result.update(status="blocked", reason=str(detail.get("reason", "blocked")))
                cls._audit(organization_id, "retention_erasure_blocked", "candidate",
                           candidate_id, "blocked", reason)
            else:
                result.update(status="failed", reason=f"http_{exc.status_code}")
                cls._audit(organization_id, "retention_erasure_failed", "candidate",
                           candidate_id, "failed", reason)
            return result
        except Exception as exc:  # noqa: BLE001 - report and continue with the next candidate
            logger.warning(
                "Retention erasure failed",
                extra={"candidate_id": candidate_id, "failure_type": type(exc).__name__},
            )
            result.update(status="failed", reason=type(exc).__name__)
            cls._audit(organization_id, "retention_erasure_failed", "candidate",
                       candidate_id, "failed", reason)
            return result
        if erased.complete:
            result.update(status="completed")
            cls._audit(organization_id, "retention_erasure_completed", "candidate",
                       candidate_id, "completed", reason)
        else:
            result.update(status="deletion_pending",
                          pending_file_count=erased.pending_file_count)
            cls._audit(organization_id, "retention_erasure_pending", "candidate",
                       candidate_id, "partial", reason)
        return result

    @staticmethod
    def _audit(
        organization_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        reason: str | None,
    ) -> None:
        # Automated: no actor. Identifiers and rule ids only.
        with SessionLocal.begin() as session:
            session.add(
                AccessAuditRecord(
                    actor_user_id=None,
                    organization_id=organization_id,
                    requested_organization_id=organization_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    outcome=outcome,
                    support_reason=reason,
                )
            )


class CandidateLegalHoldService:
    """Set or clear a candidate's legal hold (organization administrators only)."""

    @staticmethod
    def _authorize(context: TenantContext) -> None:
        if context.role != "org_admin" or context.is_platform_admin or context.is_automated:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Legal hold requires an organization administrator",
            )

    @classmethod
    def set_hold(cls, candidate_id: str, context: TenantContext, reason: str) -> dict[str, Any]:
        cls._authorize(context)
        reason = (reason or "").strip()
        if (
            not reason
            or len(reason) > _LEGAL_HOLD_REASON_MAX
            or any(character in reason for character in "\r\n")
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Legal hold reason must be a single line of 1-500 characters",
            )
        return cls._apply(candidate_id, context, hold=True, reason=reason)

    @classmethod
    def clear_hold(cls, candidate_id: str, context: TenantContext) -> dict[str, Any]:
        cls._authorize(context)
        return cls._apply(candidate_id, context, hold=False, reason=None)

    @staticmethod
    def _apply(
        candidate_id: str, context: TenantContext, *, hold: bool, reason: str | None
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        with SessionLocal.begin() as session:
            candidate = session.execute(
                select(CandidateRecord)
                .where(
                    CandidateRecord.id == candidate_id,
                    CandidateRecord.organization_id == context.organization_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if candidate is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found"
                )
            candidate.legal_hold = hold
            candidate.legal_hold_reason = reason
            candidate.legal_hold_set_by_user_id = context.actor_user_id if hold else None
            candidate.legal_hold_set_at = now if hold else None
            # The hold reason is never copied into the audit row.
            session.add(
                AccessAuditRecord(
                    actor_user_id=context.actor_user_id,
                    organization_id=context.organization_id,
                    requested_organization_id=context.organization_id,
                    action="candidate_legal_hold_set" if hold else "candidate_legal_hold_cleared",
                    resource_type="candidate",
                    resource_id=candidate.id,
                    outcome="allowed",
                    support_reason=context.support_reason,
                )
            )
        return {
            "candidate_id": candidate_id,
            "legal_hold": hold,
            "legal_hold_set_at": now.isoformat() if hold else None,
        }


def _organization_ids(args: argparse.Namespace) -> list[str]:
    if args.organization_id:
        return [args.organization_id]
    with SessionLocal() as session:
        return list(
            session.execute(select(OrganizationRecord.id).order_by(OrganizationRecord.id)).scalars()
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.retention_policy",
        description="Preview (default) or execute configured candidate retention rules.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--organization-id")
    target.add_argument("--all-organizations", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Erase selected candidates (also requires RETENTION_EXECUTION_ENABLED=true).",
    )
    args = parser.parse_args(argv)
    try:
        rules = load_rules()
    except RetentionConfigError as exc:
        print(json.dumps({"error": "invalid_retention_rules", "detail": str(exc)}))
        return 2
    reports = []
    try:
        if args.execute:
            _require_execution_enabled(rules)
        for organization_id in _organization_ids(args):
            reports.append(
                RetentionPolicyService.run(organization_id, execute=args.execute, rules=rules)
            )
    except RetentionExecutionDisabled as exc:
        print(json.dumps({"error": "retention_execution_disabled", "detail": str(exc)}))
        return 3
    output = [report.as_dict() for report in reports]
    print(json.dumps({"rules_configured": len(rules), "organizations": output}, indent=2))
    return 0 if all(item["complete"] for item in output) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
