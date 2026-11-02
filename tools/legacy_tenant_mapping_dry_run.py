"""Aggregate-only, read-only audit of legacy tenant ownership mappings."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

REPORT_GLOB = "report_*.json"
MAX_MAPPING_BYTES = 10 * 1024 * 1024
MAX_REPORT_BYTES = 5 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STATUSES = (
    "mappable",
    "unmapped",
    "ambiguous",
    "structurally_inconsistent",
)
MAPPING_CATEGORIES = {
    "organization_memberships",
    "candidates",
    "interviews",
    "access_audit",
    "candidate_invitations",
    "rag_documents",
}
REQUIRED_COLUMNS: dict[str, set[str]] = {
    "users": {"id"},
    "candidates": {"id"},
    "interviews": {"id", "candidate_id", "owner_user_id"},
    "interview_questions": {"id", "interview_id"},
    "interview_turns": {"id", "interview_id", "question_id"},
    "interview_assessments": {"id", "interview_id", "turn_id"},
    "interview_evidence": {
        "id",
        "interview_id",
        "turn_id",
        "question_id",
    },
    "interview_competency_state": {"id", "interview_id"},
    "access_audit": {"id", "actor_user_id", "resource_type", "resource_id"},
    "rag_documents": {"id", "document_id", "chunk_id"},
}
OPTIONAL_TABLES = {"candidate_invitations", "organization_memberships", "organizations"}


class DryRunError(Exception):
    """An expected failure whose public message contains no source data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MappingEntry:
    category: str
    record_id: str
    organization_id: str | None
    evidence_reference: str
    role: str | None = None
    scope: str | None = None


class Counts:
    def __init__(self) -> None:
        self.total = 0
        self.values = Counter({status: 0 for status in STATUSES})

    def add(self, status: str) -> None:
        self.total += 1
        self.values[status] += 1

    def report(self) -> dict[str, int]:
        return {"total": self.total, **{key: self.values[key] for key in STATUSES}}


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read_mapping(path: Path, approved_digest: str | None) -> tuple[
    bool,
    str | None,
    dict[str, str],
    dict[tuple[str, str], list[MappingEntry]],
    dict[tuple[str, str, str], list[MappingEntry]],
    int,
]:
    if path.is_symlink() or not path.is_file():
        raise DryRunError("mapping_file_unavailable")
    if path.suffix.lower() != ".json":
        raise DryRunError("mapping_schema_invalid")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DryRunError("mapping_file_unavailable") from exc
    if len(raw) > MAX_MAPPING_BYTES:
        raise DryRunError("mapping_file_too_large")

    digest = hashlib.sha256(raw).hexdigest()
    if approved_digest is not None:
        if not SHA256_PATTERN.fullmatch(approved_digest):
            raise DryRunError("approved_digest_invalid")
        if digest != approved_digest:
            raise DryRunError("approved_digest_mismatch")

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise DryRunError("mapping_schema_invalid") from exc

    if not isinstance(payload, dict) or set(payload) != {
        "format_version",
        "approval",
        "organizations",
        "records",
    }:
        raise DryRunError("mapping_schema_invalid")
    if type(payload["format_version"]) is not int or payload["format_version"] != 1:
        raise DryRunError("mapping_schema_invalid")

    approval = payload["approval"]
    if not isinstance(approval, dict) or set(approval) != {
        "status",
        "authority_source",
        "approval_reference",
        "approver",
        "approved_at",
    }:
        raise DryRunError("mapping_schema_invalid")
    if (
        approval["status"] != "approved"
        or not all(
            _nonempty_string(approval[field])
            for field in ("authority_source", "approval_reference", "approver")
        )
        or not _valid_timestamp(approval["approved_at"])
    ):
        raise DryRunError("mapping_provenance_incomplete")

    organizations = payload["organizations"]
    if not isinstance(organizations, list):
        raise DryRunError("mapping_schema_invalid")
    organization_references: dict[str, str] = {}
    for organization in organizations:
        if (
            not isinstance(organization, dict)
            or set(organization) != {"organization_id", "evidence_reference"}
            or not _nonempty_string(organization["organization_id"])
            or not _nonempty_string(organization["evidence_reference"])
        ):
            raise DryRunError("mapping_schema_invalid")
        organization_id = organization["organization_id"]
        if organization_id in organization_references:
            raise DryRunError("mapping_schema_invalid")
        organization_references[organization_id] = organization["evidence_reference"]

    records = payload["records"]
    if not isinstance(records, list):
        raise DryRunError("mapping_schema_invalid")
    by_record: dict[tuple[str, str], list[MappingEntry]] = defaultdict(list)
    memberships: dict[tuple[str, str, str], list[MappingEntry]] = defaultdict(list)
    for record in records:
        if not isinstance(record, dict):
            raise DryRunError("mapping_schema_invalid")
        category = record.get("category")
        if not isinstance(category, str) or category not in MAPPING_CATEGORIES:
            raise DryRunError("mapping_schema_invalid")
        expected = {"category", "record_id", "evidence_reference"}
        if category == "organization_memberships":
            expected |= {"organization_id", "role"}
        elif category == "rag_documents":
            expected |= {"scope"}
            if record.get("scope") == "organization":
                expected.add("organization_id")
            elif record.get("scope") == "global":
                if "organization_id" in record:
                    raise DryRunError("mapping_schema_invalid")
            else:
                raise DryRunError("mapping_schema_invalid")
        else:
            expected.add("organization_id")
        if set(record) != expected:
            raise DryRunError("mapping_schema_invalid")
        if not _nonempty_string(record["record_id"]) or not _nonempty_string(
            record["evidence_reference"]
        ):
            raise DryRunError("mapping_schema_invalid")

        organization_id = record.get("organization_id")
        if category != "rag_documents" or record["scope"] == "organization":
            if (
                not _nonempty_string(organization_id)
                or organization_id not in organization_references
            ):
                raise DryRunError("mapping_provenance_incomplete")
        if category == "organization_memberships" and (
            not isinstance(record["role"], str)
            or record["role"] not in {"org_admin", "recruiter", "candidate"}
        ):
            raise DryRunError("mapping_schema_invalid")

        entry = MappingEntry(
            category=category,
            record_id=record["record_id"],
            organization_id=organization_id,
            evidence_reference=record["evidence_reference"],
            role=record.get("role"),
            scope=record.get("scope"),
        )
        if category == "organization_memberships":
            memberships[(entry.record_id, organization_id, entry.role or "")].append(entry)
        else:
            by_record[(category, entry.record_id)].append(entry)

    approved = approved_digest is not None
    if not approved:
        return False, digest, {}, {}, {}, len(records)
    return (
        True,
        digest,
        organization_references,
        by_record,
        memberships,
        len(records),
    )


def _valid_timestamp(value: Any) -> bool:
    if not _nonempty_string(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _open_read_only(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise DryRunError("source_target_unsafe")
    try:
        resolved = path.resolve(strict=True)
        uri = resolved.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            connection.close()
            raise DryRunError("read_only_enforcement_failed")
        database_path = connection.execute("PRAGMA database_list").fetchone()["file"]
        if Path(database_path).resolve(strict=True) != resolved:
            connection.close()
            raise DryRunError("source_target_unsafe")
        return connection
    except DryRunError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise DryRunError("source_target_unavailable") from exc


def _inspect_schema(connection: sqlite3.Connection) -> tuple[set[str], dict[str, set[str]]]:
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            table: {
                row["name"]
                for row in connection.execute(
                    f"PRAGMA table_info({_quote_identifier(table)})"
                )
            }
            for table in tables
        }
    except sqlite3.Error as exc:
        raise DryRunError("source_schema_invalid") from exc

    for table, required in REQUIRED_COLUMNS.items():
        if table not in tables or not required.issubset(columns[table]):
            raise DryRunError("source_schema_invalid")
    for table in OPTIONAL_TABLES & tables:
        required = {
            "candidate_invitations": {"id", "recruiter_user_id", "candidate_id"},
            "organization_memberships": {
                "organization_id",
                "user_id",
                "role",
                "status",
            },
            "organizations": {"id", "status"},
        }[table]
        if not required.issubset(columns[table]):
            raise DryRunError("source_schema_invalid")
    return tables, columns


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _select_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: set[str],
    requested: Sequence[str],
) -> list[dict[str, Any]]:
    selected = [
        f'"{column}"' if column in columns else f'NULL AS "{column}"'
        for column in requested
    ]
    try:
        return [
            dict(row)
            for row in connection.execute(
                f'SELECT {", ".join(selected)} FROM "{table}"'
            )
        ]
    except sqlite3.Error as exc:
        raise DryRunError("source_read_failed") from exc


def _classify_direct(
    category: str,
    record_id: str,
    entries: dict[tuple[str, str], list[MappingEntry]],
    *,
    structural: bool = False,
) -> tuple[str, MappingEntry | None]:
    if structural:
        return "structurally_inconsistent", None
    matches = entries.get((category, record_id), [])
    if len(matches) > 1:
        return "ambiguous", None
    if not matches:
        return "unmapped", None
    return "mappable", matches[0]


def _membership_status(
    user_id: str | None,
    organization_id: str | None,
    role: str,
    memberships: dict[tuple[str, str, str], list[MappingEntry]],
) -> str:
    if not user_id or not organization_id:
        return "unmapped"
    exact = memberships.get((user_id, organization_id, role), [])
    competing = [
        key
        for key in memberships
        if key[0] == user_id and key[1] == organization_id and key[2] != role
    ]
    if len(exact) > 1 or competing:
        return "ambiguous"
    return "mappable" if exact else "unmapped"


def _classify_inherited_children(
    rows: list[dict[str, Any]],
    parent_status: dict[str, tuple[str, str | None]],
    secondary_parents: dict[str, set[str]] | None = None,
    required_secondary_parents: Sequence[str] = (),
) -> tuple[dict[str, int], int]:
    counts = Counts()
    broken = 0
    for row in rows:
        parent_id = row["interview_id"]
        relationship_broken = not parent_id or parent_id not in parent_status
        if relationship_broken:
            status = "structurally_inconsistent"
        else:
            status, organization_id = parent_status[parent_id]
            secondary_parents = secondary_parents or {}
            if any(not row[field] for field in required_secondary_parents) or any(
                row[field] and row[field] not in valid_ids
                for field, valid_ids in secondary_parents.items()
            ):
                relationship_broken = True
                status = "structurally_inconsistent"
            elif status == "mappable" and not organization_id:
                status = "unmapped"
        counts.add(status)
        broken += int(relationship_broken)
    return counts.report(), broken


def _read_reports(
    reports_dir: Path | None,
    interviews: dict[str, str | None],
    interview_status: dict[str, tuple[str, str | None]],
    candidates: dict[str, tuple[str, str | None]],
) -> tuple[dict[str, int], dict[str, int], bool]:
    counts = Counts()
    relationships = Counter()
    if reports_dir is None:
        return counts.report(), dict(relationships), False
    if reports_dir.is_symlink() or not reports_dir.is_dir():
        raise DryRunError("reports_target_unsafe")

    try:
        paths = sorted(reports_dir.glob(REPORT_GLOB))
    except OSError as exc:
        raise DryRunError("reports_read_failed") from exc
    for path in paths:
        status = "structurally_inconsistent"
        if path.is_symlink() or not path.is_file():
            relationships["unreadable"] += 1
            counts.add(status)
            continue
        try:
            if path.stat().st_size > MAX_REPORT_BYTES:
                relationships["unreadable"] += 1
                counts.add(status)
                continue
            raw = path.read_bytes()
            if len(raw) > MAX_REPORT_BYTES:
                relationships["unreadable"] += 1
                counts.add(status)
                continue
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
            relationships["malformed_or_unreadable"] += 1
            counts.add(status)
            continue
        if not isinstance(payload, dict):
            relationships["missing_link_fields"] += 1
            counts.add(status)
            continue
        interview_id = payload.get("interview_id")
        candidate_id = payload.get("candidate_id")
        if not _nonempty_string(interview_id) or not _nonempty_string(candidate_id):
            relationships["missing_link_fields"] += 1
            counts.add(status)
            continue
        if interview_id not in interviews:
            relationships["source_interview_absent"] += 1
            counts.add(status)
            continue
        if interviews[interview_id] != candidate_id:
            relationships["interview_candidate_mismatch"] += 1
            counts.add(status)
            continue

        relationships["interview_candidate_match"] += 1
        interview_state, interview_organization = interview_status[interview_id]
        candidate_state, candidate_organization = candidates.get(
            candidate_id, ("structurally_inconsistent", None)
        )
        if candidate_id not in candidates:
            relationships["candidate_profile_absent"] += 1
            counts.add("structurally_inconsistent")
        elif interview_state == "ambiguous" or candidate_state == "ambiguous":
            counts.add("ambiguous")
        elif (
            interview_state == "mappable"
            and candidate_state == "mappable"
            and interview_organization == candidate_organization
        ):
            counts.add("mappable")
        elif (
            interview_state == "mappable"
            and candidate_state == "mappable"
            and interview_organization != candidate_organization
        ):
            relationships["tenant_scope_mismatch"] += 1
            counts.add("ambiguous")
        elif (
            interview_state == "structurally_inconsistent"
            or candidate_state == "structurally_inconsistent"
        ):
            counts.add("structurally_inconsistent")
        else:
            counts.add("unmapped")
    return counts.report(), dict(relationships), True


def run_dry_run(
    source_db: str | Path | None,
    *,
    mapping_file: str | Path | None = None,
    approved_mapping_sha256: str | None = None,
    reports_dir: str | Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Inspect one explicitly named SQLite source; never mutate it."""
    if source_db is None or not str(source_db).strip():
        raise DryRunError("source_target_required")
    source_path = Path(source_db)
    if source_path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise DryRunError("source_target_unsafe")
    if mapping_file is None and approved_mapping_sha256 is not None:
        raise DryRunError("mapping_file_required_for_digest")

    approved = False
    mapping_digest = None
    organization_references: dict[str, str] = {}
    record_entries: dict[tuple[str, str], list[MappingEntry]] = {}
    membership_entries: dict[tuple[str, str, str], list[MappingEntry]] = {}
    mapping_entry_count = 0
    if mapping_file is not None:
        (
            approved,
            mapping_digest,
            organization_references,
            record_entries,
            membership_entries,
            mapping_entry_count,
        ) = _read_mapping(Path(mapping_file), approved_mapping_sha256)

    reports_path = Path(reports_dir) if reports_dir is not None else None
    connection = _open_read_only(source_path)
    try:
        tables, columns = _inspect_schema(connection)

        integrity_rows = list(connection.execute("PRAGMA integrity_check"))
        sqlite_integrity_ok = len(integrity_rows) == 1 and integrity_rows[0][0] == "ok"
        foreign_key_violations = Counter()
        for row in connection.execute("PRAGMA foreign_key_check"):
            foreign_key_violations[row[0]] += 1

        user_rows = _select_rows(connection, "users", columns["users"], ("id",))
        candidate_rows = _select_rows(
            connection,
            "candidates",
            columns["candidates"],
            ("id", "owner_user_id", "user_id", "organization_id"),
        )
        interview_rows = _select_rows(
            connection,
            "interviews",
            columns["interviews"],
            ("id", "candidate_id", "owner_user_id", "organization_id"),
        )

        all_table_rows: dict[str, list[dict[str, Any]]] = {}
        for table in (
            "interview_questions",
            "interview_turns",
            "interview_assessments",
            "interview_evidence",
            "interview_competency_state",
        ):
            all_table_rows[table] = _select_rows(
                connection,
                table,
                columns[table],
                ("id", "interview_id", "turn_id", "question_id"),
            )
        audit_rows = _select_rows(
            connection,
            "access_audit",
            columns["access_audit"],
            ("id", "actor_user_id", "resource_type", "resource_id", "organization_id"),
        )
        rag_rows = _select_rows(
            connection,
            "rag_documents",
            columns["rag_documents"],
            ("id", "organization_id"),
        )
        invitation_rows = (
            _select_rows(
                connection,
                "candidate_invitations",
                columns["candidate_invitations"],
                ("id", "candidate_id", "recruiter_user_id", "organization_id"),
            )
            if "candidate_invitations" in tables
            else []
        )
        membership_rows = (
            _select_rows(
                connection,
                "organization_memberships",
                columns["organization_memberships"],
                ("organization_id", "user_id", "role", "status"),
            )
            if "organization_memberships" in tables
            else []
        )
        source_organizations = (
            {
                row["id"]: row.get("status")
                for row in _select_rows(
                    connection,
                    "organizations",
                    columns["organizations"],
                    ("id", "status"),
                )
            }
            if "organizations" in tables
            else None
        )
    except sqlite3.Error as exc:
        raise DryRunError("source_read_failed") from exc
    finally:
        connection.close()

    source_user_ids = {row["id"] for row in user_rows}
    candidate_ids = {row["id"] for row in candidate_rows}
    interview_by_id = {row["id"]: row for row in interview_rows}
    source_organization_ids = (
        set(source_organizations) if source_organizations is not None else None
    )

    invalid_mapping_references = 0
    invalid_organization_ids: set[str] = set()
    if approved:
        if source_organizations is not None:
            for organization_id in organization_references:
                source_status = source_organizations.get(organization_id)
                if organization_id not in source_organizations or source_status != "active":
                    invalid_organization_ids.add(organization_id)
                    invalid_mapping_references += 1
        for key, entries in record_entries.items():
            category, record_id = key
            if category == "candidates" and record_id not in candidate_ids:
                invalid_mapping_references += len(entries)
            elif category == "interviews" and record_id not in interview_by_id:
                invalid_mapping_references += len(entries)
            elif category == "access_audit" and record_id not in {
                row["id"] for row in audit_rows
            }:
                invalid_mapping_references += len(entries)
            elif category == "candidate_invitations" and record_id not in {
                row["id"] for row in invitation_rows
            }:
                invalid_mapping_references += len(entries)
            elif category == "rag_documents" and record_id not in {
                row["id"] for row in rag_rows
            }:
                invalid_mapping_references += len(entries)
        for (user_id, organization_id, _), entries in membership_entries.items():
            if user_id not in source_user_ids:
                invalid_mapping_references += len(entries)
            if (
                source_organization_ids is not None
                and organization_id not in source_organization_ids
            ):
                invalid_organization_ids.add(organization_id)
                invalid_mapping_references += len(entries)

    approved_record_entries = (
        {
            key: [
                entry
                for entry in entries
                if entry.organization_id is None
                or entry.organization_id not in invalid_organization_ids
            ]
            for key, entries in record_entries.items()
        }
        if approved
        else {}
    )
    approved_membership_entries = (
        {
            key: entries
            for key, entries in membership_entries.items()
            if key[0] in source_user_ids and key[1] not in invalid_organization_ids
        }
        if approved
        else {}
    )

    organization_membership_state = Counts()
    for row in membership_rows:
        user_id = row["user_id"]
        organization_id = row["organization_id"]
        role = row["role"]
        if user_id not in source_user_ids or not organization_id:
            status = "structurally_inconsistent"
        elif (
            source_organization_ids is not None
            and organization_id not in source_organization_ids
        ):
            status = "structurally_inconsistent"
        elif row["status"] != "active":
            status = "unmapped"
        else:
            status = _membership_status(
                user_id,
                organization_id,
                role,
                approved_membership_entries,
            )
        organization_membership_state.add(status)

    user_states: dict[str, str] = {}
    users_with_membership: dict[str, list[tuple[str, str]]] = defaultdict(list)
    ambiguous_users: set[str] = set()
    if approved:
        for (
            user_id,
            organization_id,
            role,
        ), entries in approved_membership_entries.items():
            if user_id not in source_user_ids:
                continue
            if len(entries) > 1:
                ambiguous_users.add(user_id)
            else:
                users_with_membership[user_id].append((organization_id, role))
        for user_id in source_user_ids:
            user_organizations: dict[str, set[str]] = defaultdict(set)
            for organization_id, role in users_with_membership.get(user_id, []):
                user_organizations[organization_id].add(role)
            if user_id in ambiguous_users or any(
                len(roles) > 1 for roles in user_organizations.values()
            ):
                user_states[user_id] = "ambiguous"
            elif users_with_membership.get(user_id):
                user_states[user_id] = "mappable"
            else:
                user_states[user_id] = "unmapped"
    else:
        user_states = {user_id: "unmapped" for user_id in source_user_ids}
    user_counts = Counts()
    for user_id in source_user_ids:
        user_counts.add(user_states[user_id])

    candidate_states: dict[str, tuple[str, str | None]] = {}
    candidate_counts = Counts()
    candidate_relationships = Counter()
    for row in candidate_rows:
        status, mapping = _classify_direct(
            "candidates", row["id"], approved_record_entries
        )
        if (
            mapping
            and row["organization_id"]
            and mapping.organization_id != row["organization_id"]
        ):
            status = "ambiguous"
        if row["owner_user_id"]:
            if row["owner_user_id"] not in source_user_ids:
                candidate_relationships["owner_user_missing_user"] += 1
                status = "structurally_inconsistent"
            elif mapping and approved:
                membership_status = _membership_status(
                    row["owner_user_id"],
                    mapping.organization_id,
                    "recruiter",
                    approved_membership_entries,
                )
                if membership_status != "mappable":
                    status = membership_status
        if row["user_id"]:
            if row["user_id"] not in source_user_ids:
                candidate_relationships["candidate_user_missing_user"] += 1
                status = "structurally_inconsistent"
            elif mapping and approved:
                membership_status = _membership_status(
                    row["user_id"],
                    mapping.organization_id,
                    "candidate",
                    approved_membership_entries,
                )
                if membership_status != "mappable":
                    status = membership_status
        candidate_states[row["id"]] = (
            status,
            mapping.organization_id if status == "mappable" and mapping else None,
        )
        candidate_counts.add(status)

    interview_status: dict[str, tuple[str, str | None]] = {}
    interview_counts = Counts()
    interview_relationships = Counter(
        {
            "candidate_profile_resolved": 0,
            "candidate_profile_missing": 0,
            "owner_user_resolved": 0,
            "owner_user_missing": 0,
            "owner_null": 0,
            "owner_membership_unverified": 0,
        }
    )
    for row in interview_rows:
        status = "unmapped"
        mapping: MappingEntry | None = None
        if not row["candidate_id"] or row["candidate_id"] not in candidate_ids:
            interview_relationships["candidate_profile_missing"] += 1
            status = "structurally_inconsistent"
        else:
            interview_relationships["candidate_profile_resolved"] += 1
        if not row["owner_user_id"]:
            interview_relationships["owner_null"] += 1
            status = "structurally_inconsistent"
        elif row["owner_user_id"] not in source_user_ids:
            interview_relationships["owner_user_missing"] += 1
            status = "structurally_inconsistent"
        else:
            interview_relationships["owner_user_resolved"] += 1

        if status != "structurally_inconsistent":
            status, mapping = _classify_direct(
                "interviews", row["id"], approved_record_entries
            )
            if (
                mapping
                and row["organization_id"]
                and mapping.organization_id != row["organization_id"]
            ):
                status = "ambiguous"
            candidate_status, candidate_organization = candidate_states[row["candidate_id"]]
            if candidate_status == "structurally_inconsistent":
                status = candidate_status
            elif candidate_status == "ambiguous":
                status = "ambiguous"
            elif status == "mappable" and approved and mapping is not None:
                if (
                    candidate_status == "mappable"
                    and candidate_organization != mapping.organization_id
                ):
                    status = "ambiguous"
                else:
                    owner_membership = _membership_status(
                        row["owner_user_id"],
                        mapping.organization_id,
                        "recruiter",
                        approved_membership_entries,
                    )
                    if owner_membership != "mappable":
                        interview_relationships["owner_membership_unverified"] += 1
                        status = owner_membership
                    elif candidate_status != "mappable":
                        status = "unmapped"
        interview_status[row["id"]] = (
            status,
            mapping.organization_id if status == "mappable" and mapping else None,
        )
        interview_counts.add(status)

    interview_ids_to_candidates = {
        row["id"]: row["candidate_id"] for row in interview_rows
    }
    question_counts, question_broken = _classify_inherited_children(
        all_table_rows["interview_questions"], interview_status
    )
    turn_counts, turn_broken = _classify_inherited_children(
        all_table_rows["interview_turns"],
        interview_status,
        secondary_parents={
            "question_id": {
                row["id"] for row in all_table_rows["interview_questions"]
            }
        },
    )
    assessment_counts, assessment_broken = _classify_inherited_children(
        all_table_rows["interview_assessments"],
        interview_status,
        secondary_parents={
            "turn_id": {row["id"] for row in all_table_rows["interview_turns"]}
        },
        required_secondary_parents=("turn_id",),
    )
    evidence_counts, evidence_broken = _classify_inherited_children(
        all_table_rows["interview_evidence"],
        interview_status,
        secondary_parents={
            "turn_id": {row["id"] for row in all_table_rows["interview_turns"]},
            "question_id": {
                row["id"] for row in all_table_rows["interview_questions"]
            },
        },
        required_secondary_parents=("turn_id",),
    )
    competency_counts, competency_broken = _classify_inherited_children(
        all_table_rows["interview_competency_state"], interview_status
    )

    audit_counts = Counts()
    audit_relationships = Counter()
    for row in audit_rows:
        structural = bool(
            row["actor_user_id"] and row["actor_user_id"] not in source_user_ids
        )
        if structural:
            audit_relationships["actor_user_missing"] += 1
        status, _ = _classify_direct(
            "access_audit",
            row["id"],
            approved_record_entries,
            structural=structural,
        )
        audit_counts.add(status)

    invitation_counts = Counts()
    invitation_relationships = Counter()
    for row in invitation_rows:
        structural = False
        if row["recruiter_user_id"] not in source_user_ids:
            structural = True
            invitation_relationships["recruiter_user_missing"] += 1
        if row["candidate_id"] and row["candidate_id"] not in candidate_ids:
            structural = True
            invitation_relationships["candidate_profile_missing"] += 1
        status, mapping = _classify_direct(
            "candidate_invitations",
            row["id"],
            approved_record_entries,
            structural=structural,
        )
        if status == "mappable" and mapping and approved:
            recruiter_status = _membership_status(
                row["recruiter_user_id"],
                mapping.organization_id,
                "recruiter",
                approved_membership_entries,
            )
            candidate_status, candidate_organization = (
                candidate_states.get(row["candidate_id"], ("unmapped", None))
                if row["candidate_id"]
                else ("mappable", mapping.organization_id)
            )
            if recruiter_status != "mappable":
                status = recruiter_status
            elif candidate_status == "structurally_inconsistent":
                status = candidate_status
            elif candidate_status == "ambiguous":
                status = "ambiguous"
            elif candidate_status == "unmapped":
                status = "unmapped"
            elif candidate_organization != mapping.organization_id:
                status = "ambiguous"
        invitation_counts.add(status)

    rag_counts = Counts()
    rag_scopes = Counter()
    for row in rag_rows:
        status, mapping = _classify_direct(
            "rag_documents", row["id"], approved_record_entries
        )
        if mapping and approved:
            if mapping.scope == "global":
                rag_scopes["verified_global"] += 1
            elif mapping.scope == "organization":
                rag_scopes["verified_organization"] += 1
            if row["organization_id"] and row["organization_id"] != mapping.organization_id:
                status = "ambiguous"
                rag_scopes["scope_conflict"] += 1
        else:
            rag_scopes["unknown"] += 1
        rag_counts.add(status)

    report_counts, report_relationships, reports_scanned = _read_reports(
        reports_path,
        interview_ids_to_candidates,
        interview_status,
        candidate_states,
    )

    all_counts = {
        "users": user_counts.report(),
        "organization_memberships": organization_membership_state.report(),
        "candidates": candidate_counts.report(),
        "interviews": interview_counts.report(),
        "interview_questions": question_counts,
        "interview_turns": turn_counts,
        "interview_assessments": assessment_counts,
        "interview_evidence": evidence_counts,
        "interview_competency_state": competency_counts,
        "candidate_invitations": invitation_counts.report(),
        "access_audit": audit_counts.report(),
        "rag_documents": rag_counts.report(),
        "report_exports": report_counts,
    }
    structural_count = sum(
        values["structurally_inconsistent"] for values in all_counts.values()
    )
    ambiguous_count = sum(values["ambiguous"] for values in all_counts.values())
    foreign_key_total = sum(foreign_key_violations.values())
    integrity_passed = (
        sqlite_integrity_ok
        and foreign_key_total == 0
        and structural_count == 0
        and ambiguous_count == 0
        and invalid_mapping_references == 0
    )

    report = {
        "report_schema_version": 1,
        "source": {"type": "sqlite", "schema_valid": True},
        "mapping": {
            "state": (
                "approved_digest_matched"
                if approved
                else "proposed_review_required"
                if mapping_file is not None
                else "not_provided"
            ),
            "sha256": mapping_digest,
            "entries_proposed": mapping_entry_count,
            "approved_organization_count": len(organization_references)
            if approved
            else 0,
            "invalid_source_references": invalid_mapping_references,
        },
        "resources": all_counts,
        "relationships": {
            "interview_candidate": dict(interview_relationships),
            "candidate_owner_and_user": dict(candidate_relationships),
            "audit_actor": dict(audit_relationships),
            "invitation": dict(invitation_relationships),
            "report_exports": report_relationships,
        },
        "rag_provenance": {
            "status": (
                "verified_by_approved_mapping"
                if approved and rag_counts.total and not rag_scopes["unknown"]
                else "unknown"
                if rag_counts.total
                else "no_documents"
            ),
            "scopes": dict(rag_scopes),
        },
        "reports_scanned": reports_scanned,
        "integrity": {
            "sqlite_integrity_check": "ok" if sqlite_integrity_ok else "failed",
            "foreign_key_violations": {
                "total": foreign_key_total,
                "by_child_table": dict(foreign_key_violations),
            },
            "broken_child_links": {
                "interview_questions": question_broken,
                "interview_turns": turn_broken,
                "interview_assessments": assessment_broken,
                "interview_evidence": evidence_broken,
                "interview_competency_state": competency_broken,
            },
            "passed": integrity_passed,
        },
    }
    return report, 0 if integrity_passed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only aggregate inventory of a legacy SQLite tenant mapping. "
            "No database URL or application default is consulted."
        )
    )
    parser.add_argument(
        "--source-db",
        required=True,
        help="Explicit SQLite source file (.db, .sqlite, or .sqlite3); opened read-only.",
    )
    parser.add_argument(
        "--reports-dir",
        help="Optional explicit directory containing report_*.json export files.",
    )
    parser.add_argument(
        "--mapping",
        help="Optional strict JSON mapping proposal. CSV files are not accepted.",
    )
    parser.add_argument(
        "--approved-mapping-sha256",
        help="Out-of-band approved SHA-256 digest of the exact mapping file bytes.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report, exit_code = run_dry_run(
            args.source_db,
            mapping_file=args.mapping,
            approved_mapping_sha256=args.approved_mapping_sha256,
            reports_dir=args.reports_dir,
        )
    except DryRunError as exc:
        print(json.dumps({"error": exc.code}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
