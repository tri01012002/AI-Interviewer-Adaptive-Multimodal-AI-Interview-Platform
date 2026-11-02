from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from tools.legacy_tenant_crosswalk_v2 import (
    validate_crosswalk_v2,
    validate_crosswalk_v2_file,
)

SNAPSHOT_DIGEST = "a" * 64
EVIDENCE_DIGEST = "b" * 64
APPROVAL_DIGEST = "c" * 64
START = "2020-01-01T00:00:00Z"
END = "2030-01-01T00:00:00Z"
RELEVANT_AT = "2025-01-01T00:00:00Z"


def _key(
    record_type: str,
    record_id: str,
    *,
    source_system: str = "legacy-db",
    snapshot_id: str = "snapshot-1",
) -> dict[str, str]:
    return {
        "source_system": source_system,
        "source_snapshot_id": snapshot_id,
        "record_type": record_type,
        "source_record_id": record_id,
    }


def _evidence(digest: str = EVIDENCE_DIGEST) -> dict[str, Any]:
    return {
        "locator": "restricted://case/evidence",
        "sha256": digest,
        "type": "source-system-record",
        "issuer": "source-custodian",
        "effective_from": START,
        "effective_to": END,
    }


def _record(
    record_type: str,
    record_id: str,
    *,
    relationships: list[dict[str, Any]] | None = None,
    disposition: str = "approved",
    organization_id: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source_key": _key(record_type, record_id),
        "disposition": disposition,
        "relationships": relationships or [],
        "evidence_refs": [_evidence()],
    }
    if organization_id is not None:
        record["target_organization_id"] = organization_id
    if record_type == "organization_membership":
        record["membership_role"] = "recruiter"
    if record_type in {"interview", "invitation", "report_export", "audit_event"}:
        record["relevant_at"] = RELEVANT_AT
    return record


def _relationship(kind: str, target_type: str, target_id: str) -> dict[str, Any]:
    return {"relation_type": kind, "target_key": _key(target_type, target_id)}


def _proposal() -> dict[str, Any]:
    user = _record("user", "user-1")
    membership = _record(
        "organization_membership",
        "membership-1",
        relationships=[_relationship("membership_user", "user", "user-1")],
        organization_id="org-1",
    )
    candidate = _record("candidate", "candidate-1", organization_id="org-1")
    interview = _record(
        "interview",
        "interview-1",
        relationships=[
            _relationship("interview_candidate", "candidate", "candidate-1"),
            _relationship("interview_recruiter", "user", "user-1"),
            _relationship(
                "interview_recruiter_membership",
                "organization_membership",
                "membership-1",
            ),
        ],
        organization_id="org-1",
    )
    return {
        "crosswalk_schema_version": 2,
        "mapping_revision": {
            "revision_id": "review-2026-001",
            "sequence": 1,
            "supersedes_revision": None,
            "superseded_by_revision": None,
            "supersession_decision": None,
        },
        "prepared_by": {
            "identity_id": "preparer-1",
            "prepared_at": "2026-01-01T00:00:00Z",
        },
        "source_snapshots": [
            {
                "source_system": "legacy-db",
                "snapshot_id": "snapshot-1",
                "snapshot_sha256": SNAPSHOT_DIGEST,
            }
        ],
        "records": [user, membership, candidate, interview],
        "approval_record": {},
    }


def _seal_proposal(proposal: dict[str, Any]) -> bytes:
    core = {key: value for key, value in proposal.items() if key != "approval_record"}
    canonical = json.dumps(
        core, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    organization_ids = sorted({
        record["target_organization_id"]
        for record in proposal["records"]
        if record["disposition"] == "approved"
        and isinstance(record.get("target_organization_id"), str)
    })
    reviewer_specs = [
        ("custodian-1", "source_custodian", None),
        *[
            (f"owner-{index}", "organization_data_owner", organization_id)
            for index, organization_id in enumerate(organization_ids, start=1)
        ],
    ]
    if any(
        isinstance(record.get("source_key"), dict)
        and isinstance(record["source_key"].get("record_type"), str)
        and record["source_key"]["record_type"]
        in {"report_export", "audit_event", "rag_document", "rag_chunk", "rag_embedding"}
        for record in proposal["records"]
    ):
        reviewer_specs.append(("steward-1", "security_data_steward", None))
    approvals = []
    for reviewer, role, organization_id in reviewer_specs:
        approval = {
            "approver_id": reviewer,
            "approver_role": role,
            "decision": "approved",
            "decision_timestamp": "2026-01-02T00:00:00Z",
            "decision_revision": proposal["mapping_revision"]["revision_id"],
            "evidence_refs": [_evidence(APPROVAL_DIGEST)],
        }
        if organization_id is not None:
            approval["organization_id"] = organization_id
        approvals.append(approval)
    proposal["approval_record"] = {
        "crosswalk_sha256": hashlib.sha256(canonical).hexdigest(),
        "case_reference": "restricted://approval/case",
        "approvals": approvals,
    }
    return json.dumps(proposal, ensure_ascii=False).encode("utf-8")


def _validate(proposal: dict[str, Any]):
    return validate_crosswalk_v2(_seal_proposal(proposal))


def _proposal_with_two_interview_trees() -> dict[str, Any]:
    proposal = _proposal()
    proposal["records"].extend(
        [
            _record("candidate", "candidate-2", organization_id="org-1"),
            _record(
                "interview",
                "interview-2",
                relationships=[
                    _relationship("interview_candidate", "candidate", "candidate-2"),
                    _relationship("interview_recruiter", "user", "user-1"),
                    _relationship(
                        "interview_recruiter_membership",
                        "organization_membership",
                        "membership-1",
                    ),
                ],
                organization_id="org-1",
            ),
            _record(
                "interview_question",
                "question-1",
                relationships=[
                    _relationship("child_interview", "interview", "interview-1")
                ],
            ),
            _record(
                "interview_question",
                "question-2",
                relationships=[
                    _relationship("child_interview", "interview", "interview-2")
                ],
            ),
            _record(
                "interview_turn",
                "turn-1",
                relationships=[
                    _relationship("child_interview", "interview", "interview-1"),
                    _relationship("turn_question", "interview_question", "question-1"),
                ],
            ),
            _record(
                "interview_turn",
                "turn-2",
                relationships=[
                    _relationship("child_interview", "interview", "interview-2"),
                    _relationship("turn_question", "interview_question", "question-2"),
                ],
            ),
        ]
    )
    return proposal


def test_structurally_valid_proposal_is_explicitly_unauthenticated():
    result = validate_crosswalk_v2(_seal_proposal(_proposal()))

    assert result.structurally_valid is True
    assert result.record_count == 4
    assert result.dispositions["approved"] == 4
    assert result.authorization_status == "not_authenticated_by_validator"
    assert result.as_dict()["authorization_status"] == "not_authenticated_by_validator"


def test_malformed_json_is_rejected_without_echoing_input():
    result = validate_crosswalk_v2('{"private-record-id": "secret",')

    assert result.structurally_valid is False
    assert result.errors == {"json_invalid": 1}
    assert "secret" not in json.dumps(result.as_dict())
    assert "private-record-id" not in json.dumps(result.as_dict())


def test_duplicate_json_properties_are_rejected():
    result = validate_crosswalk_v2('{"crosswalk_schema_version":2,"crosswalk_schema_version":2}')

    assert result.errors == {"json_duplicate_property": 1}


def test_json_surrogate_values_are_rejected():
    result = validate_crosswalk_v2('{"value":"\\ud800"}')

    assert result.errors == {"json_invalid": 1}


def test_unsupported_schema_version_is_rejected():
    proposal = _proposal()
    proposal["crosswalk_schema_version"] = 3

    result = _validate(proposal)

    assert result.errors == {"unsupported_schema_version": 1}


def test_duplicate_stable_keys_are_rejected():
    proposal = _proposal()
    proposal["records"].append(copy.deepcopy(proposal["records"][0]))

    result = _validate(proposal)

    assert result.errors["duplicate_record_key"] == 1


def test_malformed_nested_types_are_rejected_without_echoing_values():
    proposal = _proposal()
    proposal["records"][0]["source_key"]["record_type"] = ["PRIVATE_TYPE_SENTINEL"]

    result = _validate(proposal)

    assert result.errors["record_schema_invalid"] == 1
    assert "PRIVATE_TYPE_SENTINEL" not in json.dumps(result.as_dict())


def test_invalid_snapshot_and_evidence_digests_are_rejected():
    proposal = _proposal()
    proposal["source_snapshots"][0]["snapshot_sha256"] = "not-a-digest"
    result = validate_crosswalk_v2(_seal_proposal(proposal))

    assert result.errors == {"source_snapshots_invalid": 1}

    proposal = _proposal()
    proposal["records"][0]["evidence_refs"][0]["sha256"] = "xyz"
    result = _validate(proposal)
    assert result.errors["evidence_invalid"] >= 1


def test_relationships_can_resolve_across_registered_source_snapshots():
    proposal = _proposal()
    idp_user = _record("user", "canonical-user")
    idp_user["source_key"] = _key(
        "user",
        "canonical-user",
        source_system="identity-provider",
        snapshot_id="idp-snapshot",
    )
    proposal["source_snapshots"].append(
        {
            "source_system": "identity-provider",
            "snapshot_id": "idp-snapshot",
            "snapshot_sha256": "d" * 64,
        }
    )
    proposal["records"].append(idp_user)
    external_user_key = _key(
        "user",
        "canonical-user",
        source_system="identity-provider",
        snapshot_id="idp-snapshot",
    )
    proposal["records"][1]["relationships"][0]["target_key"] = external_user_key
    proposal["records"][3]["relationships"][1]["target_key"] = external_user_key

    result = _validate(proposal)

    assert result.structurally_valid is True


def test_malformed_evidence_intervals_are_rejected():
    proposal = _proposal()
    proposal["records"][0]["evidence_refs"][0]["effective_to"] = START

    result = _validate(proposal)

    assert result.errors["evidence_invalid"] == 1


def test_missing_required_approval_role_is_rejected():
    proposal = _proposal()
    raw = _seal_proposal(proposal)
    loaded = json.loads(raw)
    loaded["approval_record"]["approvals"] = loaded["approval_record"]["approvals"][1:]
    core = {key: value for key, value in loaded.items() if key != "approval_record"}
    loaded["approval_record"]["crosswalk_sha256"] = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    result = validate_crosswalk_v2(json.dumps(loaded))

    assert result.errors["approval_role_missing"] == 1


def test_security_steward_is_required_for_reports_audit_and_rag():
    proposal = _proposal()
    rag_document = _record("rag_document", "rag-1")
    rag_document["scope"] = "global"
    proposal["records"].append(rag_document)
    loaded = json.loads(_seal_proposal(proposal))
    loaded["approval_record"]["approvals"] = [
        approval
        for approval in loaded["approval_record"]["approvals"]
        if approval["approver_role"] != "security_data_steward"
    ]

    result = validate_crosswalk_v2(json.dumps(loaded))

    assert result.errors["approval_role_missing"] == 1


def test_each_target_organization_requires_its_own_owner_approval():
    proposal = _proposal()
    proposal["records"].append(
        _record("candidate", "candidate-2", organization_id="org-2")
    )
    loaded = json.loads(_seal_proposal(proposal))
    loaded["approval_record"]["approvals"] = [
        approval
        for approval in loaded["approval_record"]["approvals"]
        if approval.get("organization_id") != "org-2"
    ]

    result = validate_crosswalk_v2(json.dumps(loaded))

    assert result.errors["approval_role_missing"] == 1


def test_same_person_cannot_fill_multiple_approval_roles():
    proposal = _proposal()
    loaded = json.loads(_seal_proposal(proposal))
    loaded["approval_record"]["approvals"][1]["approver_id"] = "custodian-1"

    result = validate_crosswalk_v2(json.dumps(loaded))

    assert result.errors["approval_identity_conflict"] == 1


def test_preparer_cannot_self_approve():
    proposal = _proposal()
    loaded = json.loads(_seal_proposal(proposal))
    loaded["approval_record"]["approvals"][0]["approver_id"] = "preparer-1"

    result = validate_crosswalk_v2(json.dumps(loaded))

    assert result.errors["approval_identity_conflict"] == 1


def test_conflicting_relationships_are_rejected():
    proposal = _proposal()
    candidate = _record("candidate", "candidate-2", organization_id="org-1")
    proposal["records"].append(candidate)
    proposal["records"][-2]["relationships"].append(
        _relationship("interview_candidate", "candidate", "candidate-2")
    )

    result = _validate(proposal)

    assert result.errors["relationship_conflict"] == 1


def test_unresolved_parent_key_is_rejected():
    proposal = _proposal()
    proposal["records"][-1]["relationships"][0]["target_key"] = _key(
        "candidate", "missing-parent"
    )

    result = _validate(proposal)

    assert result.errors["relationship_unresolved"] == 1


def test_nonapproved_record_cannot_carry_target_organization():
    proposal = _proposal()
    candidate = proposal["records"][2]
    candidate["disposition"] = "ambiguous"

    result = _validate(proposal)

    assert result.errors["target_on_nonapproved"] == 1
    assert result.errors["relationship_parent_unapproved"] >= 1


def test_interview_candidate_organization_conflict_is_rejected():
    proposal = _proposal()
    proposal["records"][2]["target_organization_id"] = "org-2"

    result = _validate(proposal)

    assert result.errors["relationship_organization_mismatch"] >= 1


def test_organization_audit_scope_must_match_its_linked_resource():
    proposal = _proposal()
    audit_event = _record(
        "audit_event",
        "audit-1",
        relationships=[
            _relationship("audit_resource", "interview", "interview-1")
        ],
        organization_id="org-2",
    )
    audit_event["scope"] = "organization"
    proposal["records"].append(audit_event)

    result = _validate(proposal)

    assert result.errors["relationship_organization_mismatch"] >= 1


def test_assessment_turn_must_belong_to_the_same_interview():
    proposal = _proposal_with_two_interview_trees()
    proposal["records"].append(
        _record(
            "interview_assessment",
            "assessment-1",
            relationships=[
                _relationship("child_interview", "interview", "interview-1"),
                _relationship("assessment_turn", "interview_turn", "turn-2"),
            ],
        )
    )

    result = _validate(proposal)

    assert result.errors["secondary_parent_mismatch"] == 1


def test_evidence_turn_and_question_must_belong_to_the_same_interview():
    proposal = _proposal_with_two_interview_trees()
    proposal["records"].append(
        _record(
            "interview_evidence",
            "evidence-1",
            relationships=[
                _relationship("child_interview", "interview", "interview-1"),
                _relationship("evidence_turn", "interview_turn", "turn-2"),
                _relationship(
                    "evidence_question", "interview_question", "question-2"
                ),
            ],
        )
    )

    result = _validate(proposal)

    assert result.errors["secondary_parent_mismatch"] == 2


def test_turn_question_must_belong_to_the_same_interview():
    proposal = _proposal_with_two_interview_trees()
    turn = next(
        record
        for record in proposal["records"]
        if record["source_key"]["record_type"] == "interview_turn"
        and record["source_key"]["source_record_id"] == "turn-1"
    )
    turn["relationships"][1] = _relationship(
        "turn_question", "interview_question", "question-2"
    )

    result = _validate(proposal)

    assert result.errors["secondary_parent_mismatch"] == 1


def test_secondary_parent_relationships_pass_when_interview_trees_match():
    proposal = _proposal_with_two_interview_trees()
    proposal["records"].append(
        _record(
            "interview_assessment",
            "assessment-1",
            relationships=[
                _relationship("child_interview", "interview", "interview-1"),
                _relationship("assessment_turn", "interview_turn", "turn-1"),
            ],
        )
    )

    result = _validate(proposal)

    assert result.structurally_valid is True


def test_superseded_revision_and_stale_approval_revision_are_rejected():
    proposal = _proposal()
    proposal["mapping_revision"]["superseded_by_revision"] = "review-2026-002"
    loaded = json.loads(_seal_proposal(proposal))
    loaded["approval_record"]["approvals"][0]["decision_revision"] = "older-revision"

    result = validate_crosswalk_v2(json.dumps(loaded))

    assert result.errors["revision_superseded"] == 1
    assert result.errors["approval_revision_invalid"] == 1


def test_new_revision_requires_an_explicit_superseding_decision():
    proposal = _proposal()
    proposal["mapping_revision"] = {
        "revision_id": "review-2026-002",
        "sequence": 2,
        "supersedes_revision": "review-2026-001",
        "superseded_by_revision": None,
        "supersession_decision": {
            "superseded_revision": "review-2026-001",
            "decision_by": "custodian-1",
            "decision_timestamp": "2026-01-01T12:00:00Z",
            "case_reference": "restricted://supersession/case",
            "evidence_refs": [_evidence(APPROVAL_DIGEST)],
        },
    }

    result = validate_crosswalk_v2(_seal_proposal(proposal))

    assert result.structurally_valid is True
    assert result.authorization_status == "not_authenticated_by_validator"


def test_invalid_revision_sequence_is_rejected():
    proposal = _proposal()
    proposal["mapping_revision"]["sequence"] = []

    result = _validate(proposal)

    assert result.errors["revision_invalid"] == 1


def test_missing_approval_record_is_rejected():
    proposal = _proposal()
    proposal.pop("approval_record")

    result = validate_crosswalk_v2(json.dumps(proposal))

    assert result.errors["payload_schema_invalid"] == 1


def test_proposal_digest_mismatch_is_rejected():
    proposal = _proposal()
    loaded = json.loads(_seal_proposal(proposal))
    loaded["records"][0]["source_key"]["source_record_id"] = "changed-after-approval"

    result = validate_crosswalk_v2(json.dumps(loaded))

    assert result.errors["proposal_digest_mismatch"] == 1


def test_file_validation_is_read_only_and_privacy_safe(tmp_path: Path):
    path = tmp_path / "crosswalk.json"
    proposal = _proposal()
    proposal["prepared_by"]["identity_id"] = "PRIVATE_ID_SENTINEL"
    raw = _seal_proposal(proposal)
    path.write_bytes(raw)

    result = validate_crosswalk_v2_file(path)

    assert result.structurally_valid is True
    assert "PRIVATE_ID_SENTINEL" not in json.dumps(result.as_dict())

    missing = validate_crosswalk_v2_file(tmp_path / "missing.json")
    assert missing.errors == {"input_unavailable": 1}
