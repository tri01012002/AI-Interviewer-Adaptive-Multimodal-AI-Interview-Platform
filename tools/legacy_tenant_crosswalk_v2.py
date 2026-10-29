"""Structural, aggregate-only validation for proposed legacy tenant crosswalk v2 files."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_CROSSWALK_BYTES = 10 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STATUSES = ("approved", "unmapped", "ambiguous", "quarantined")
APPROVAL_ROLES = (
    "source_custodian",
    "organization_data_owner",
    "security_data_steward",
)
RECORD_TYPES = {
    "user",
    "organization_membership",
    "candidate",
    "interview",
    "interview_question",
    "interview_turn",
    "interview_assessment",
    "interview_evidence",
    "interview_competency_state",
    "invitation",
    "report_export",
    "audit_event",
    "rag_document",
    "rag_chunk",
    "rag_embedding",
}
CHILD_RECORD_TYPES = {
    "interview_question",
    "interview_turn",
    "interview_assessment",
    "interview_evidence",
    "interview_competency_state",
    "rag_chunk",
    "rag_embedding",
}
REQUIRED_RELATIONSHIPS: dict[str, set[str]] = {
    "organization_membership": {"membership_user"},
    "interview": {
        "interview_candidate",
        "interview_recruiter",
        "interview_recruiter_membership",
    },
    "interview_question": {"child_interview"},
    "interview_turn": {"child_interview"},
    "interview_assessment": {"child_interview", "assessment_turn"},
    "interview_evidence": {"child_interview", "evidence_turn"},
    "interview_competency_state": {"child_interview"},
    "invitation": {"invitation_recruiter", "invitation_recruiter_membership"},
    "report_export": {
        "report_interview",
        "report_candidate",
        "report_actor",
        "report_actor_membership",
    },
    "audit_event": {"audit_resource"},
    "rag_chunk": {"rag_parent_document"},
    "rag_embedding": {"rag_parent_document"},
}
RELATION_TARGET_TYPES: dict[str, set[str]] = {
    "membership_user": {"user"},
    "candidate_account": {"user"},
    "candidate_owner": {"user"},
    "interview_candidate": {"candidate"},
    "interview_recruiter": {"user"},
    "interview_recruiter_membership": {"organization_membership"},
    "child_interview": {"interview"},
    "turn_question": {"interview_question"},
    "assessment_turn": {"interview_turn"},
    "evidence_turn": {"interview_turn"},
    "evidence_question": {"interview_question"},
    "invitation_recruiter": {"user"},
    "invitation_recruiter_membership": {"organization_membership"},
    "report_interview": {"interview"},
    "report_candidate": {"candidate"},
    "report_actor": {"user"},
    "report_actor_membership": {"organization_membership"},
    "audit_resource": {
        "candidate",
        "interview",
        "interview_question",
        "interview_turn",
        "interview_assessment",
        "interview_evidence",
        "interview_competency_state",
        "invitation",
        "report_export",
        "rag_document",
        "rag_chunk",
        "rag_embedding",
    },
    "audit_actor": {"user"},
    "audit_actor_membership": {"organization_membership"},
    "rag_parent_document": {"rag_document"},
}
ALLOWED_RELATIONSHIPS: dict[str, set[str]] = {
    "user": set(),
    "organization_membership": {"membership_user"},
    "candidate": {"candidate_account", "candidate_owner"},
    "interview": {
        "interview_candidate",
        "interview_recruiter",
        "interview_recruiter_membership",
    },
    "interview_question": {"child_interview"},
    "interview_turn": {"child_interview", "turn_question"},
    "interview_assessment": {"child_interview", "assessment_turn"},
    "interview_evidence": {
        "child_interview",
        "evidence_turn",
        "evidence_question",
    },
    "interview_competency_state": {"child_interview"},
    "invitation": {"invitation_recruiter", "invitation_recruiter_membership"},
    "report_export": {
        "report_interview",
        "report_candidate",
        "report_actor",
        "report_actor_membership",
    },
    "audit_event": {"audit_resource", "audit_actor", "audit_actor_membership"},
    "rag_document": set(),
    "rag_chunk": {"rag_parent_document"},
    "rag_embedding": {"rag_parent_document"},
}
ORGANIZATION_SCOPED_TYPES = {
    "organization_membership",
    "candidate",
    "interview",
    "invitation",
    "report_export",
}
TIME_SCOPED_TYPES = {"interview", "invitation", "report_export", "audit_event"}
SECURITY_REVIEW_TYPES = {
    "report_export",
    "audit_event",
    "rag_document",
    "rag_chunk",
    "rag_embedding",
}


class _DuplicatePropertyError(ValueError):
    pass


class _InvalidConstantError(ValueError):
    pass


@dataclass(frozen=True)
class ValidationResult:
    """Safe validation summary; it intentionally contains no input identifiers."""

    structurally_valid: bool
    record_count: int
    dispositions: dict[str, int]
    errors: dict[str, int]

    @property
    def authorization_status(self) -> str:
        return "not_authenticated_by_validator"

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_schema_version": 1,
            "crosswalk_schema_version": 2,
            "structurally_valid": self.structurally_valid,
            "authorization_status": self.authorization_status,
            "record_count": self.record_count,
            "dispositions": dict(self.dispositions),
            "errors": dict(self.errors),
        }


def _reject_duplicate_properties(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicatePropertyError
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise _InvalidConstantError


def _contains_surrogate(value: Any) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                return True
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
    return False


def _is_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 2048
        and not any(ord(character) < 32 for character in value)
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not _is_text(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _has_exact_keys(value: Any, expected: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == expected


def _source_key_tuple(value: Any) -> tuple[str, str, str, str] | None:
    if not _has_exact_keys(
        value,
        {"source_system", "source_snapshot_id", "record_type", "source_record_id"},
    ):
        return None
    if not all(
        _is_text(value[field])
        for field in ("source_system", "source_snapshot_id", "source_record_id")
    ):
        return None
    if (
        not isinstance(value["record_type"], str)
        or value["record_type"] not in RECORD_TYPES
    ):
        return None
    return (
        value["source_system"],
        value["source_snapshot_id"],
        value["record_type"],
        value["source_record_id"],
    )


def _valid_source_key(
    value: Any,
    snapshots: set[tuple[str, str]],
) -> tuple[str, str, str, str] | None:
    source_key = _source_key_tuple(value)
    if source_key is None or (source_key[0], source_key[1]) not in snapshots:
        return None
    return source_key


def _valid_evidence_reference(value: Any) -> bool:
    if not _has_exact_keys(
        value,
        {
            "locator",
            "sha256",
            "type",
            "issuer",
            "effective_from",
            "effective_to",
        },
    ):
        return False
    start = _parse_timestamp(value["effective_from"])
    end_value = value["effective_to"]
    end = None if end_value is None else _parse_timestamp(end_value)
    return (
        _is_text(value["locator"])
        and _is_digest(value["sha256"])
        and _is_text(value["type"])
        and _is_text(value["issuer"])
        and start is not None
        and (end_value is None or end is not None)
        and (end is None or start < end)
    )


def _record_shape_valid(
    record: Any,
    snapshots: set[tuple[str, str]],
    errors: Counter[str],
) -> tuple[tuple[str, str, str, str], dict[str, Any]] | None:
    if not isinstance(record, dict):
        errors["record_schema_invalid"] += 1
        return None

    source_key = _valid_source_key(record.get("source_key"), snapshots)
    disposition = record.get("disposition")
    if source_key is None:
        errors["record_schema_invalid"] += 1
        return None
    if disposition not in STATUSES:
        errors["invalid_disposition"] += 1
        return None

    record_type = source_key[2]
    required = {"source_key", "disposition", "relationships", "evidence_refs"}
    allowed = set(required)
    allowed.add("target_organization_id")
    if record_type in {"rag_document", "audit_event"}:
        allowed.add("scope")
    if record_type == "organization_membership":
        allowed.add("membership_role")
    if record_type in TIME_SCOPED_TYPES:
        allowed.add("relevant_at")
    if not required.issubset(record) or not set(record).issubset(allowed):
        errors["record_schema_invalid"] += 1
        return None

    approved = disposition == "approved"
    if not approved and "target_organization_id" in record:
        errors["target_on_nonapproved"] += 1
    if not approved and "scope" in record:
        errors["scope_on_nonapproved"] += 1
    if approved and record_type in ORGANIZATION_SCOPED_TYPES:
        if not _is_text(record.get("target_organization_id")):
            errors["target_organization_missing"] += 1
    if record_type == "organization_membership":
        membership_role = record.get("membership_role")
        if approved and (
            not isinstance(membership_role, str)
            or membership_role not in {"org_admin", "recruiter", "candidate"}
        ):
            errors["record_schema_invalid"] += 1
        elif not approved and membership_role is not None and (
            not isinstance(membership_role, str)
            or membership_role not in {"org_admin", "recruiter", "candidate"}
        ):
            errors["record_schema_invalid"] += 1
    elif "membership_role" in record:
        errors["record_schema_invalid"] += 1

    if record_type == "rag_document":
        scope = record.get("scope") if approved else None
        if approved and (
            not isinstance(scope, str) or scope not in {"global", "organization"}
        ):
            errors["record_schema_invalid"] += 1
        if approved and scope == "organization" and not _is_text(
            record.get("target_organization_id")
        ):
            errors["target_organization_missing"] += 1
        if approved and scope == "global" and "target_organization_id" in record:
            errors["scope_organization_conflict"] += 1
    elif record_type == "audit_event":
        scope = record.get("scope") if approved else None
        if approved and (
            not isinstance(scope, str) or scope not in {"organization", "platform"}
        ):
            errors["record_schema_invalid"] += 1
        if approved and scope == "organization" and not _is_text(
            record.get("target_organization_id")
        ):
            errors["target_organization_missing"] += 1
        if approved and scope == "platform" and "target_organization_id" in record:
            errors["scope_organization_conflict"] += 1
    elif approved and record_type not in ORGANIZATION_SCOPED_TYPES | CHILD_RECORD_TYPES | {
        "rag_document",
        "audit_event",
    }:
        if "target_organization_id" in record:
            errors["target_organization_unexpected"] += 1
    elif approved and record_type in CHILD_RECORD_TYPES and (
        "target_organization_id" in record or "scope" in record
    ):
        errors["child_scope_must_be_inherited"] += 1
    elif record_type not in {"rag_document", "audit_event"} and "scope" in record:
        errors["record_schema_invalid"] += 1

    relevant_at = record.get("relevant_at")
    if record_type in TIME_SCOPED_TYPES:
        parsed_relevant_at = _parse_timestamp(relevant_at)
        if approved and parsed_relevant_at is None:
            errors["record_time_invalid"] += 1
        elif relevant_at is not None and parsed_relevant_at is None:
            errors["record_time_invalid"] += 1
    elif "relevant_at" in record:
        errors["record_schema_invalid"] += 1

    relationships = record["relationships"]
    if not isinstance(relationships, list):
        errors["relationship_invalid"] += 1
    else:
        for relationship in relationships:
            if not _has_exact_keys(relationship, {"relation_type", "target_key"}):
                errors["relationship_invalid"] += 1
                continue
            relation_type = relationship["relation_type"]
            if not isinstance(relation_type, str) or (
                relation_type not in ALLOWED_RELATIONSHIPS[record_type]
            ) or _valid_source_key(relationship["target_key"], snapshots) is None:
                errors["relationship_invalid"] += 1

    evidence_refs = record["evidence_refs"]
    if not isinstance(evidence_refs, list):
        errors["evidence_invalid"] += 1
    else:
        if approved and not evidence_refs:
            errors["evidence_missing"] += 1
        for reference in evidence_refs:
            if not _valid_evidence_reference(reference):
                errors["evidence_invalid"] += 1
        if approved and record_type in TIME_SCOPED_TYPES:
            relevant = _parse_timestamp(relevant_at)
            if relevant is not None and not any(
                _valid_evidence_reference(reference)
                and _parse_timestamp(reference["effective_from"]) <= relevant
                and (
                    reference["effective_to"] is None
                    or relevant < _parse_timestamp(reference["effective_to"])
                )
                for reference in evidence_refs
            ):
                errors["effective_evidence_missing"] += 1

    return source_key, record


def _validate_relationship_graph(
    records: dict[tuple[str, str, str, str], dict[str, Any]],
    errors: Counter[str],
) -> None:
    for source_key, record in records.items():
        record_type = source_key[2]
        relationships = record.get("relationships")
        if not isinstance(relationships, list):
            continue

        by_type: dict[str, list[tuple[str, str, str, str]]] = {}
        for relationship in relationships:
            if not _has_exact_keys(relationship, {"relation_type", "target_key"}):
                continue
            relation_type = relationship["relation_type"]
            if not isinstance(relation_type, str):
                continue
            target_key = _source_key_tuple(relationship["target_key"])
            if target_key is not None:
                by_type.setdefault(relation_type, []).append(target_key)

        if any(len(targets) > 1 for targets in by_type.values()):
            errors["relationship_conflict"] += 1
        if record.get("disposition") != "approved":
            continue

        if not REQUIRED_RELATIONSHIPS.get(record_type, set()).issubset(by_type):
            errors["relationship_required_missing"] += 1
        for relation_type, targets in by_type.items():
            allowed_types = RELATION_TARGET_TYPES.get(relation_type, set())
            for target_key in targets:
                target = records.get(target_key)
                if target is None:
                    errors["relationship_unresolved"] += 1
                    continue
                if target_key[2] not in allowed_types:
                    errors["relationship_invalid"] += 1
                    continue
                if target.get("disposition") != "approved":
                    errors["relationship_parent_unapproved"] += 1
                    continue

                target_org = target.get("target_organization_id")
                source_org = record.get("target_organization_id")
                source_scope = record.get("scope")
                target_scope = target.get("scope")
                if source_org and target_org and source_org != target_org:
                    errors["relationship_organization_mismatch"] += 1
                if (
                    record_type in CHILD_RECORD_TYPES
                    and relation_type == "child_interview"
                    and not target_org
                ):
                    errors["relationship_parent_scope_invalid"] += 1
                if record_type == "interview" and relation_type == "interview_candidate":
                    if not source_org or source_org != target_org:
                        errors["relationship_organization_mismatch"] += 1
                if record_type == "interview" and relation_type == "interview_recruiter_membership":
                    if target_org != source_org or target.get("membership_role") != "recruiter":
                        errors["recruiter_membership_invalid"] += 1
                    membership_user = _single_target(
                        target, "membership_user"
                    )
                    recruiter_user = _single_target(
                        record, "interview_recruiter"
                    )
                    if membership_user is None or membership_user != recruiter_user:
                        errors["recruiter_membership_invalid"] += 1
                    relevant = _parse_timestamp(record.get("relevant_at"))
                    if relevant is not None and not _evidence_covers(
                        target.get("evidence_refs"), relevant
                    ):
                        errors["membership_not_effective"] += 1
                if record_type == "invitation" and relation_type == "invitation_recruiter_membership":
                    if target_org != source_org or target.get("membership_role") != "recruiter":
                        errors["recruiter_membership_invalid"] += 1
                    membership_user = _single_target(
                        target, "membership_user"
                    )
                    recruiter_user = _single_target(
                        record, "invitation_recruiter"
                    )
                    if membership_user is None or membership_user != recruiter_user:
                        errors["recruiter_membership_invalid"] += 1
                    relevant = _parse_timestamp(record.get("relevant_at"))
                    if relevant is not None and not _evidence_covers(
                        target.get("evidence_refs"), relevant
                    ):
                        errors["membership_not_effective"] += 1
                if record_type == "report_export" and relation_type == "report_actor_membership":
                    if target_org != source_org or target.get("membership_role") not in {
                        "recruiter",
                        "org_admin",
                    }:
                        errors["report_actor_membership_invalid"] += 1
                    membership_user = _single_target(
                        target, "membership_user"
                    )
                    actor_user = _single_target(
                        record, "report_actor"
                    )
                    if membership_user is None or membership_user != actor_user:
                        errors["report_actor_membership_invalid"] += 1
                    relevant = _parse_timestamp(record.get("relevant_at"))
                    if relevant is not None and not _evidence_covers(
                        target.get("evidence_refs"), relevant
                    ):
                        errors["membership_not_effective"] += 1
                if record_type == "report_export" and relation_type in {
                    "report_interview",
                    "report_candidate",
                }:
                    if source_org != target_org:
                        errors["relationship_organization_mismatch"] += 1
                if record_type == "report_export" and relation_type == "report_candidate":
                    interview_key = _single_target(
                        record, "report_interview"
                    )
                    interview = records.get(interview_key) if interview_key else None
                    interview_candidate = (
                        _single_target(
                            interview, "interview_candidate"
                        )
                        if interview is not None
                        else None
                    )
                    if interview_candidate != target_key:
                        errors["report_candidate_interview_conflict"] += 1
                if record_type == "audit_event" and source_scope == "organization":
                    if target_org != source_org:
                        errors["relationship_organization_mismatch"] += 1
                if record_type == "rag_chunk" or record_type == "rag_embedding":
                    if target_scope not in {"global", "organization"}:
                        errors["rag_parent_scope_invalid"] += 1
        _validate_secondary_parent_links(record, records, errors)


def _validate_secondary_parent_links(
    record: dict[str, Any],
    records: dict[tuple[str, str, str, str], dict[str, Any]],
    errors: Counter[str],
) -> None:
    if record.get("disposition") != "approved":
        return
    parent_interview = _single_target(record, "child_interview")
    if parent_interview is None:
        return

    related_interview_types = {
        "turn_question": {"interview_question"},
        "assessment_turn": {"interview_turn"},
        "evidence_turn": {"interview_turn"},
        "evidence_question": {"interview_question"},
    }
    relationships = record.get("relationships")
    if not isinstance(relationships, list):
        return
    for relationship in relationships:
        if not _has_exact_keys(relationship, {"relation_type", "target_key"}):
            continue
        relation_type = relationship["relation_type"]
        if not isinstance(relation_type, str) or relation_type not in related_interview_types:
            continue
        secondary_key = _source_key_tuple(relationship["target_key"])
        if secondary_key is None or secondary_key[2] not in related_interview_types[relation_type]:
            continue
        secondary_record = records.get(secondary_key)
        if secondary_record is None or secondary_record.get("disposition") != "approved":
            continue
        secondary_parent = _single_target(secondary_record, "child_interview")
        if secondary_parent != parent_interview:
            errors["secondary_parent_mismatch"] += 1


def _single_target(
    record: dict[str, Any],
    relation_type: str,
) -> tuple[str, str, str, str] | None:
    relationships = record.get("relationships")
    if not isinstance(relationships, list):
        return None
    targets = [
        _source_key_tuple(item.get("target_key"))
        for item in relationships
        if isinstance(item, dict) and item.get("relation_type") == relation_type
    ]
    return targets[0] if len(targets) == 1 else None


def _evidence_covers(evidence_refs: Any, timestamp: datetime) -> bool:
    if not isinstance(evidence_refs, list):
        return False
    for reference in evidence_refs:
        if not _valid_evidence_reference(reference):
            continue
        start = _parse_timestamp(reference["effective_from"])
        end = (
            None
            if reference["effective_to"] is None
            else _parse_timestamp(reference["effective_to"])
        )
        if start is not None and start <= timestamp and (end is None or timestamp < end):
            return True
    return False


def _validate_approvals(
    payload: dict[str, Any],
    errors: Counter[str],
) -> None:
    revision = payload.get("mapping_revision")
    prepared_by = payload.get("prepared_by")
    approval_record = payload.get("approval_record")
    if not _has_exact_keys(
        revision,
        {
            "revision_id",
            "sequence",
            "supersedes_revision",
            "superseded_by_revision",
            "supersession_decision",
        },
    ) or not isinstance(prepared_by, dict):
        errors["approval_invalid"] += 1
        return
    revision_id = revision.get("revision_id")
    sequence = revision.get("sequence")
    supersedes = revision.get("supersedes_revision")
    superseded_by = revision.get("superseded_by_revision")
    is_later_revision = type(sequence) is int and sequence > 1
    if (
        not _is_text(revision_id)
        or type(sequence) is not int
        or sequence < 1
        or (
            sequence == 1
            and (supersedes is not None or revision.get("supersession_decision") is not None)
        )
        or (is_later_revision and (not _is_text(supersedes) or supersedes == revision_id))
    ):
        errors["revision_invalid"] += 1
    if superseded_by is not None:
        errors["revision_superseded"] += 1
    supersession = revision.get("supersession_decision")
    if is_later_revision:
        if not _has_exact_keys(
            supersession,
            {
                "superseded_revision",
                "decision_by",
                "decision_timestamp",
                "case_reference",
                "evidence_refs",
            },
        ):
            errors["revision_supersession_invalid"] += 1
        elif (
            supersession["superseded_revision"] != supersedes
            or not _is_text(supersession["decision_by"])
            or _parse_timestamp(supersession["decision_timestamp"]) is None
            or not _is_text(supersession["case_reference"])
            or not isinstance(supersession["evidence_refs"], list)
            or not supersession["evidence_refs"]
            or not all(
                _valid_evidence_reference(reference)
                for reference in supersession["evidence_refs"]
            )
        ):
            errors["revision_supersession_invalid"] += 1
    if not _has_exact_keys(prepared_by, {"identity_id", "prepared_at"}):
        errors["approval_invalid"] += 1
        return
    if (
        not _is_text(prepared_by.get("identity_id"))
        or _parse_timestamp(prepared_by.get("prepared_at")) is None
    ):
        errors["approval_invalid"] += 1

    if not isinstance(approval_record, dict) or not _has_exact_keys(
        approval_record, {"crosswalk_sha256", "case_reference", "approvals"}
    ):
        errors["approval_missing"] += 1
        return
    if not _is_digest(approval_record.get("crosswalk_sha256")) or not _is_text(
        approval_record.get("case_reference")
    ):
        errors["approval_invalid"] += 1

    approvals = approval_record.get("approvals")
    if not isinstance(approvals, list):
        errors["approval_invalid"] += 1
        return

    by_role: Counter[str] = Counter()
    owner_approvals: Counter[str] = Counter()
    identity_roles: dict[str, set[str]] = defaultdict(set)
    approval_slots: set[tuple[str, str | None]] = set()
    prepared_identity = prepared_by.get("identity_id")
    prepared_time = _parse_timestamp(prepared_by.get("prepared_at"))
    organization_ids = {
        record["target_organization_id"]
        for record in payload.get("records", [])
        if isinstance(record, dict)
        and record.get("disposition") == "approved"
        and _is_text(record.get("target_organization_id"))
    }
    needs_security_review = any(
        isinstance(record, dict)
        and isinstance(record.get("source_key"), dict)
        and isinstance(record["source_key"].get("record_type"), str)
        and record["source_key"].get("record_type") in SECURITY_REVIEW_TYPES
        for record in payload.get("records", [])
    )
    for approval in approvals:
        common_fields = {
            "approver_id",
            "approver_role",
            "decision",
            "decision_timestamp",
            "decision_revision",
            "evidence_refs",
        }
        if not isinstance(approval, dict):
            errors["approval_invalid"] += 1
            continue
        role = approval.get("approver_role")
        expected_fields = common_fields | (
            {"organization_id"} if role == "organization_data_owner" else set()
        )
        if not _has_exact_keys(approval, expected_fields):
            errors["approval_invalid"] += 1
            continue
        approver_id = approval["approver_id"]
        decision_time = _parse_timestamp(approval["decision_timestamp"])
        evidence_refs = approval["evidence_refs"]
        if not _is_text(approver_id) or role not in APPROVAL_ROLES:
            errors["approval_invalid"] += 1
            continue
        by_role[role] += 1
        identity_roles[approver_id].add(role)
        organization_id: str | None = None
        if role == "organization_data_owner":
            organization_id = approval["organization_id"]
            if not _is_text(organization_id) or organization_id not in organization_ids:
                errors["approval_invalid"] += 1
            else:
                owner_approvals[organization_id] += 1
        slot = (role, organization_id)
        if slot in approval_slots:
            errors["approval_role_duplicate"] += 1
        approval_slots.add(slot)
        if approval["decision"] != "approved":
            errors["approval_invalid"] += 1
        if approval["decision_revision"] != revision_id:
            errors["approval_revision_invalid"] += 1
        if decision_time is None or (
            prepared_time is not None and decision_time < prepared_time
        ):
            errors["approval_invalid"] += 1
        if not isinstance(evidence_refs, list) or not evidence_refs:
            errors["approval_invalid"] += 1
        elif not all(_valid_evidence_reference(item) for item in evidence_refs):
            errors["approval_invalid"] += 1

    if by_role["source_custodian"] == 0:
        errors["approval_role_missing"] += 1
    elif by_role["source_custodian"] > 1:
        errors["approval_role_duplicate"] += 1
    for organization_id in organization_ids:
        if owner_approvals[organization_id] == 0:
            errors["approval_role_missing"] += 1
        elif owner_approvals[organization_id] > 1:
            errors["approval_role_duplicate"] += 1
    if needs_security_review:
        if by_role["security_data_steward"] == 0:
            errors["approval_role_missing"] += 1
        elif by_role["security_data_steward"] > 1:
            errors["approval_role_duplicate"] += 1
    if any(len(roles) > 1 for roles in identity_roles.values()) or (
        _is_text(prepared_identity) and prepared_identity in identity_roles
    ):
        errors["approval_identity_conflict"] += 1
    if (
        is_later_revision
        and isinstance(supersession, dict)
        and (
            not _is_text(supersession.get("decision_by"))
            or supersession["decision_by"] not in identity_roles
        )
    ):
        errors["revision_supersession_invalid"] += 1

    expected_digest = _proposal_digest(payload)
    if (
        _is_digest(approval_record.get("crosswalk_sha256"))
        and approval_record["crosswalk_sha256"] != expected_digest
    ):
        errors["proposal_digest_mismatch"] += 1


def _proposal_digest(payload: dict[str, Any]) -> str:
    core = {key: value for key, value in payload.items() if key != "approval_record"}
    canonical = json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _empty_result(errors: Counter[str], record_count: int = 0) -> ValidationResult:
    return ValidationResult(
        structurally_valid=not errors,
        record_count=record_count,
        dispositions={status: 0 for status in STATUSES},
        errors=dict(sorted(errors.items())),
    )


def validate_crosswalk_v2(data: bytes | str) -> ValidationResult:
    """Validate v2 structure and graph without authenticating people or evidence."""
    errors: Counter[str] = Counter()
    if isinstance(data, str):
        try:
            raw = data.encode("utf-8")
        except UnicodeError:
            errors["json_invalid"] += 1
            return _empty_result(errors)
    elif isinstance(data, bytes):
        raw = data
    else:
        errors["json_invalid"] += 1
        return _empty_result(errors)

    if len(raw) > MAX_CROSSWALK_BYTES:
        errors["input_too_large"] += 1
        return _empty_result(errors)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_properties,
            parse_constant=_reject_json_constant,
        )
    except _DuplicatePropertyError:
        errors["json_duplicate_property"] += 1
        return _empty_result(errors)
    except (UnicodeError, ValueError, RecursionError):
        errors["json_invalid"] += 1
        return _empty_result(errors)

    if _contains_surrogate(payload):
        errors["json_invalid"] += 1
        return _empty_result(errors)
    if not isinstance(payload, dict):
        errors["payload_schema_invalid"] += 1
        return _empty_result(errors)
    expected_top_level = {
        "crosswalk_schema_version",
        "mapping_revision",
        "prepared_by",
        "source_snapshots",
        "records",
        "approval_record",
    }
    if set(payload) != expected_top_level:
        errors["payload_schema_invalid"] += 1
        return _empty_result(errors)
    if type(payload.get("crosswalk_schema_version")) is not int or payload[
        "crosswalk_schema_version"
    ] != 2:
        errors["unsupported_schema_version"] += 1
        return _empty_result(errors)

    snapshot_descriptors = payload.get("source_snapshots")
    if not isinstance(snapshot_descriptors, list) or not snapshot_descriptors:
        errors["source_snapshots_invalid"] += 1
        return _empty_result(errors)
    snapshots: set[tuple[str, str]] = set()
    for descriptor in snapshot_descriptors:
        if not _has_exact_keys(
            descriptor, {"source_system", "snapshot_id", "snapshot_sha256"}
        ) or not all(
            _is_text(descriptor.get(field)) for field in ("source_system", "snapshot_id")
        ) or not _is_digest(descriptor.get("snapshot_sha256")):
            errors["source_snapshots_invalid"] += 1
            continue
        snapshot_key = (descriptor["source_system"], descriptor["snapshot_id"])
        if snapshot_key in snapshots:
            errors["source_snapshots_invalid"] += 1
        snapshots.add(snapshot_key)
    if errors["source_snapshots_invalid"]:
        return _empty_result(errors)
    records_value = payload.get("records")
    if not isinstance(records_value, list):
        errors["payload_schema_invalid"] += 1
        return _empty_result(errors)
    if not records_value:
        errors["records_empty"] += 1

    dispositions = Counter({status: 0 for status in STATUSES})
    records: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in records_value:
        if isinstance(item, dict) and item.get("disposition") in STATUSES:
            dispositions[item["disposition"]] += 1
        parsed = _record_shape_valid(
            item, snapshots, errors
        )
        if parsed is None:
            continue
        source_key, record = parsed
        if source_key in records:
            errors["duplicate_record_key"] += 1
        else:
            records[source_key] = record

    _validate_relationship_graph(records, errors)
    _validate_approvals(payload, errors)
    return ValidationResult(
        structurally_valid=not errors,
        record_count=len(records_value),
        dispositions={status: dispositions[status] for status in STATUSES},
        errors=dict(sorted(errors.items())),
    )


def validate_crosswalk_v2_file(path: str | Path) -> ValidationResult:
    """Read one explicitly supplied JSON file and return aggregate-safe diagnostics."""
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        return _empty_result(Counter({"input_unavailable": 1}))
    try:
        if source.stat().st_size > MAX_CROSSWALK_BYTES:
            return _empty_result(Counter({"input_too_large": 1}))
        return validate_crosswalk_v2(source.read_bytes())
    except OSError:
        return _empty_result(Counter({"input_unavailable": 1}))
