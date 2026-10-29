from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from tools.legacy_tenant_mapping_dry_run import (
    DryRunError,
    _open_read_only,
    main,
    run_dry_run,
)

PII_SENTINEL = "SYNTHETIC_PRIVATE_SENTINEL"


@pytest.fixture
def legacy_database(tmp_path: Path) -> Path:
    database = tmp_path / "synthetic_legacy.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE users (id TEXT PRIMARY KEY);
            CREATE TABLE candidates (
                id TEXT PRIMARY KEY,
                owner_user_id TEXT,
                user_id TEXT,
                organization_id TEXT
            );
            CREATE TABLE interviews (
                id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                owner_user_id TEXT REFERENCES users(id),
                organization_id TEXT
            );
            CREATE TABLE interview_questions (
                id TEXT PRIMARY KEY,
                interview_id TEXT NOT NULL
            );
            CREATE TABLE interview_turns (
                id TEXT PRIMARY KEY,
                interview_id TEXT NOT NULL,
                question_id TEXT
            );
            CREATE TABLE interview_assessments (
                id TEXT PRIMARY KEY,
                interview_id TEXT NOT NULL,
                turn_id TEXT NOT NULL
            );
            CREATE TABLE interview_evidence (
                id TEXT PRIMARY KEY,
                interview_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                question_id TEXT
            );
            CREATE TABLE interview_competency_state (
                id TEXT PRIMARY KEY,
                interview_id TEXT NOT NULL
            );
            CREATE TABLE access_audit (
                id TEXT PRIMARY KEY,
                actor_user_id TEXT,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                organization_id TEXT
            );
            CREATE TABLE rag_documents (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                organization_id TEXT,
                embedding_json TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO users (id) VALUES (?)",
            (("user-recruiter",), ("user-candidate",), ("user-unmapped",)),
        )
        connection.executemany(
            "INSERT INTO candidates (id, owner_user_id, user_id) VALUES (?, ?, ?)",
            (
                ("candidate-good", "user-recruiter", "user-candidate"),
                ("candidate-unmapped", None, None),
                ("candidate-other-org", None, None),
                ("candidate-owner-broken", "user-missing", None),
            ),
        )
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executemany(
            """
            INSERT INTO interviews (id, candidate_id, owner_user_id)
            VALUES (?, ?, ?)
            """,
            (
                ("interview-good", "candidate-good", "user-recruiter"),
                ("interview-unmapped", "candidate-unmapped", "user-recruiter"),
                ("interview-candidate-missing", "candidate-ghost", "user-recruiter"),
                ("interview-owner-missing", "candidate-good", "user-ghost"),
                ("interview-owner-null", "candidate-good", None),
                ("interview-conflicting", "candidate-other-org", "user-recruiter"),
            ),
        )
        connection.executemany(
            "INSERT INTO interview_questions (id, interview_id) VALUES (?, ?)",
            (
                ("question-good", "interview-good"),
                ("question-orphan", "interview-ghost"),
            ),
        )
        connection.executemany(
            """
            INSERT INTO interview_turns (id, interview_id, question_id)
            VALUES (?, ?, ?)
            """,
            (
                ("turn-good", "interview-good", "question-good"),
                ("turn-orphan", "interview-good", "question-ghost"),
                ("turn-no-question", "interview-good", None),
            ),
        )
        connection.executemany(
            "INSERT INTO interview_assessments (id, interview_id, turn_id) VALUES (?, ?, ?)",
            (
                ("assessment-good", "interview-good", "turn-good"),
                ("assessment-orphan", "interview-good", "turn-ghost"),
            ),
        )
        connection.executemany(
            """
            INSERT INTO interview_evidence (id, interview_id, turn_id, question_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                ("evidence-good", "interview-good", "turn-good", "question-good"),
                ("evidence-orphan", "interview-good", "turn-ghost", None),
            ),
        )
        connection.executemany(
            "INSERT INTO interview_competency_state (id, interview_id) VALUES (?, ?)",
            (
                ("competency-good", "interview-good"),
                ("competency-orphan", "interview-ghost"),
            ),
        )
        connection.execute(
            "INSERT INTO access_audit VALUES (?, ?, ?, ?, ?)",
            ("audit-good", "user-recruiter", "read", "interview", None),
        )
        connection.execute(
            "INSERT INTO access_audit VALUES (?, ?, ?, ?, ?)",
            ("audit-orphan", "user-ghost", "read", "interview", None),
        )
        connection.executemany(
            "INSERT INTO rag_documents VALUES (?, ?, ?, ?, ?)",
            (
                ("rag-one", "doc-one", "chunk-one", None, None),
                ("rag-two", "doc-two", "chunk-two", None, "[]"),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return database


def _approved_mapping(
    path: Path,
    *,
    extra_records: list[dict] | None = None,
) -> str:
    records = [
        {
            "category": "organization_memberships",
            "record_id": "user-recruiter",
            "organization_id": "org-a",
            "role": "recruiter",
            "evidence_reference": "roster:recruiter-a",
        },
        {
            "category": "organization_memberships",
            "record_id": "user-candidate",
            "organization_id": "org-a",
            "role": "candidate",
            "evidence_reference": "roster:candidate-a",
        },
        {
            "category": "candidates",
            "record_id": "candidate-good",
            "organization_id": "org-a",
            "evidence_reference": "ats:candidate-a",
        },
        {
            "category": "candidates",
            "record_id": "candidate-other-org",
            "organization_id": "org-b",
            "evidence_reference": "ats:candidate-b",
        },
        {
            "category": "candidates",
            "record_id": "candidate-owner-broken",
            "organization_id": "org-a",
            "evidence_reference": "ats:candidate-c",
        },
        {
            "category": "interviews",
            "record_id": "interview-good",
            "organization_id": "org-a",
            "evidence_reference": "ats:interview-a",
        },
        {
            "category": "interviews",
            "record_id": "interview-unmapped",
            "organization_id": "org-a",
            "evidence_reference": "ats:interview-unmapped",
        },
        {
            "category": "interviews",
            "record_id": "interview-conflicting",
            "organization_id": "org-a",
            "evidence_reference": "ats:interview-conflict",
        },
        {
            "category": "rag_documents",
            "record_id": "rag-one",
            "scope": "global",
            "evidence_reference": "catalog:synthetic-global",
        },
    ]
    if extra_records:
        records.extend(extra_records)
    payload = {
        "format_version": 1,
        "approval": {
            "status": "approved",
            "authority_source": "synthetic-test-source",
            "approval_reference": "test-approval-001",
            "approver": "synthetic-data-owner",
            "approved_at": "2026-10-13T00:00:00Z",
        },
        "organizations": [
            {"organization_id": "org-a", "evidence_reference": "registry:org-a"},
            {"organization_id": "org-b", "evidence_reference": "registry:org-b"},
        ],
        "records": records,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _create_empty_valid_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE users (id TEXT PRIMARY KEY);
            CREATE TABLE candidates (id TEXT PRIMARY KEY);
            CREATE TABLE interviews (
                id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                owner_user_id TEXT
            );
            CREATE TABLE interview_questions (
                id TEXT PRIMARY KEY,
                interview_id TEXT NOT NULL
            );
            CREATE TABLE interview_turns (
                id TEXT PRIMARY KEY,
                interview_id TEXT NOT NULL,
                question_id TEXT
            );
            CREATE TABLE interview_assessments (
                id TEXT PRIMARY KEY,
                interview_id TEXT NOT NULL,
                turn_id TEXT NOT NULL
            );
            CREATE TABLE interview_evidence (
                id TEXT PRIMARY KEY,
                interview_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                question_id TEXT
            );
            CREATE TABLE interview_competency_state (
                id TEXT PRIMARY KEY,
                interview_id TEXT NOT NULL
            );
            CREATE TABLE access_audit (
                id TEXT PRIMARY KEY,
                actor_user_id TEXT,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL
            );
            CREATE TABLE rag_documents (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_missing_explicit_source_target_is_rejected(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2
    assert "source-db" in capsys.readouterr().err


def test_sqlite_source_is_read_only_and_byte_for_byte_unchanged(
    legacy_database: Path,
):
    before = hashlib.sha256(legacy_database.read_bytes()).digest()

    connection = _open_read_only(legacy_database)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO users (id) VALUES ('should-not-write')")
    finally:
        connection.close()

    report, _ = run_dry_run(legacy_database)
    after = hashlib.sha256(legacy_database.read_bytes()).digest()
    assert before == after
    assert report["source"]["schema_valid"] is True


def test_aggregate_counts_and_broken_relationships_match_fixture(
    legacy_database: Path,
):
    report, exit_code = run_dry_run(legacy_database)
    resources = report["resources"]

    assert exit_code == 2
    assert resources["users"]["total"] == 3
    assert resources["candidates"]["total"] == 4
    assert resources["interviews"]["total"] == 6
    assert resources["interview_questions"]["total"] == 2
    assert resources["interview_turns"]["total"] == 3
    assert resources["interview_assessments"]["total"] == 2
    assert resources["interview_evidence"]["total"] == 2
    assert resources["interview_competency_state"]["total"] == 2
    assert resources["interviews"]["structurally_inconsistent"] == 3
    assert report["relationships"]["interview_candidate"][
        "candidate_profile_missing"
    ] == 1
    assert report["relationships"]["interview_candidate"]["owner_user_missing"] == 1
    assert report["relationships"]["interview_candidate"]["owner_null"] == 1
    assert report["relationships"]["candidate_owner_and_user"][
        "owner_user_missing_user"
    ] == 1
    assert report["integrity"]["foreign_key_violations"]["total"] == 1
    assert report["integrity"]["broken_child_links"] == {
        "interview_questions": 1,
        "interview_turns": 1,
        "interview_assessments": 1,
        "interview_evidence": 1,
        "interview_competency_state": 1,
    }


def test_child_link_counts_exclude_inherited_parent_mapping_failures(
    legacy_database: Path,
):
    connection = sqlite3.connect(legacy_database)
    try:
        connection.execute(
            "INSERT INTO interview_questions (id, interview_id) VALUES (?, ?)",
            ("question-parent-unmapped", "interview-candidate-missing"),
        )
        connection.commit()
    finally:
        connection.close()

    report, _ = run_dry_run(legacy_database)
    assert report["resources"]["interview_questions"]["structurally_inconsistent"] == 2
    assert report["integrity"]["broken_child_links"]["interview_questions"] == 1


def test_approved_mapping_requires_exact_digest_and_keeps_children_scoped(
    legacy_database: Path,
    tmp_path: Path,
):
    mapping = tmp_path / "mapping.json"
    digest = _approved_mapping(mapping)

    unapproved_report, _ = run_dry_run(
        legacy_database,
        mapping_file=mapping,
    )
    assert unapproved_report["mapping"]["state"] == "proposed_review_required"
    assert unapproved_report["resources"]["interviews"]["mappable"] == 0

    report, exit_code = run_dry_run(
        legacy_database,
        mapping_file=mapping,
        approved_mapping_sha256=digest,
    )
    assert exit_code == 2
    assert report["mapping"]["state"] == "approved_digest_matched"
    assert report["resources"]["interviews"]["mappable"] == 1
    assert report["resources"]["interviews"]["unmapped"] == 1
    assert report["resources"]["interviews"]["structurally_inconsistent"] == 3
    assert report["resources"]["interview_questions"]["mappable"] == 1
    assert report["resources"]["interview_questions"]["unmapped"] == 0
    assert report["resources"]["interview_turns"]["structurally_inconsistent"] == 1
    assert report["resources"]["interview_assessments"][
        "structurally_inconsistent"
    ] == 1
    assert report["resources"]["interview_evidence"][
        "structurally_inconsistent"
    ] == 1
    assert report["resources"]["interview_competency_state"][
        "structurally_inconsistent"
    ] == 1
    assert report["resources"]["rag_documents"]["mappable"] == 1
    assert report["rag_provenance"]["status"] == "unknown"
    assert report["rag_provenance"]["scopes"]["verified_global"] == 1
    assert report["rag_provenance"]["scopes"]["unknown"] == 1


def test_ambiguous_mapping_entries_are_never_mappable(
    legacy_database: Path,
    tmp_path: Path,
):
    mapping = tmp_path / "ambiguous.json"
    digest = _approved_mapping(
        mapping,
        extra_records=[
            {
                "category": "candidates",
                "record_id": "candidate-good",
                "organization_id": "org-b",
                "evidence_reference": "conflicting:source",
            }
        ],
    )
    report, _ = run_dry_run(
        legacy_database,
        mapping_file=mapping,
        approved_mapping_sha256=digest,
    )
    assert report["resources"]["candidates"]["ambiguous"] == 1
    assert report["resources"]["candidates"]["mappable"] == 1
    assert report["resources"]["interviews"]["ambiguous"] == 2
    assert report["resources"]["interviews"]["mappable"] == 0
    assert report["integrity"]["passed"] is False


def test_report_exports_are_reconciled_without_emitting_payloads(
    legacy_database: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    reports_dir = tmp_path / "exports"
    reports_dir.mkdir()
    (reports_dir / "report_good.json").write_text(
        json.dumps(
            {
                "interview_id": "interview-good",
                "candidate_id": "candidate-good",
                "private_note": PII_SENTINEL,
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "report_missing.json").write_text(
        json.dumps(
            {
                "interview_id": "not-in-database",
                "candidate_id": "candidate-good",
                "private_note": PII_SENTINEL,
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "report_mismatch.json").write_text(
        json.dumps(
            {
                "interview_id": "interview-good",
                "candidate_id": "candidate-other",
                "private_note": PII_SENTINEL,
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--source-db",
            str(legacy_database),
            "--reports-dir",
            str(reports_dir),
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    rendered = captured.out
    assert exit_code == 2
    assert report["reports_scanned"] is True
    assert report["resources"]["report_exports"]["total"] == 3
    assert report["relationships"]["report_exports"]["source_interview_absent"] == 1
    assert report["relationships"]["report_exports"]["interview_candidate_match"] == 1
    assert report["resources"]["report_exports"]["unmapped"] == 1
    assert report["resources"]["report_exports"]["structurally_inconsistent"] == 2
    assert PII_SENTINEL not in rendered
    assert "interview-good" not in rendered
    assert "candidate-good" not in rendered
    assert PII_SENTINEL not in captured.err


def test_unknown_rag_provenance_is_not_assumed_global(
    legacy_database: Path,
):
    report, _ = run_dry_run(legacy_database)
    assert report["resources"]["rag_documents"]["unmapped"] == 2
    assert report["rag_provenance"]["status"] == "unknown"
    assert report["rag_provenance"]["scopes"]["unknown"] == 2


@pytest.mark.parametrize(
    "contents",
    [
        b"{not-json}",
        b'{"format_version": 1, "format_version": 1}',
        b'{"format_version": 1}',
    ],
)
def test_malformed_mapping_input_is_rejected_without_echoing_content(
    tmp_path: Path,
    contents: bytes,
    capsys: pytest.CaptureFixture[str],
):
    database = tmp_path / "minimal.db"
    _create_empty_valid_database(database)
    mapping = tmp_path / "bad.json"
    mapping.write_bytes(contents)

    with pytest.raises(DryRunError) as error:
        run_dry_run(database, mapping_file=mapping)
    assert error.value.code == "mapping_schema_invalid"
    assert contents.decode("utf-8", errors="ignore") not in capsys.readouterr().err


def test_csv_mapping_is_not_treated_as_authoritative(
    legacy_database: Path,
    tmp_path: Path,
):
    mapping = tmp_path / "unreviewed.csv"
    mapping.write_text("record_id,organization_id\nprivate-id,org-a\n", encoding="utf-8")

    with pytest.raises(DryRunError) as error:
        run_dry_run(legacy_database, mapping_file=mapping)
    assert error.value.code == "mapping_schema_invalid"


@pytest.mark.parametrize("invalid_value", ["boolean_version", "list_category", "list_role"])
def test_malformed_mapping_scalar_types_fail_closed(
    legacy_database: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    invalid_value: str,
):
    mapping = tmp_path / "mapping.json"
    _approved_mapping(mapping)
    payload = json.loads(mapping.read_text(encoding="utf-8"))
    if invalid_value == "boolean_version":
        payload["format_version"] = True
    elif invalid_value == "list_category":
        payload["records"][0]["category"] = []
    else:
        payload["records"][0]["role"] = []
    mapping.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["--source-db", str(legacy_database), "--mapping", str(mapping)]) == 2
    captured = capsys.readouterr()
    assert '"error": "mapping_schema_invalid"' in captured.err
    assert "user-recruiter" not in captured.err
    assert "user-recruiter" not in captured.out


def test_schema_errors_return_nonzero_and_do_not_expose_source_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    database = tmp_path / "invalid.db"
    connection = sqlite3.connect(database)
    connection.execute(f"CREATE TABLE private_table (value TEXT)")
    connection.execute("INSERT INTO private_table VALUES (?)", (PII_SENTINEL,))
    connection.commit()
    connection.close()

    assert main(["--source-db", str(database)]) == 2
    captured = capsys.readouterr()
    assert '"error": "source_schema_invalid"' in captured.err
    assert PII_SENTINEL not in captured.err
    assert PII_SENTINEL not in captured.out


def test_unapproved_or_unresolvable_mapping_never_becomes_mappable(
    legacy_database: Path,
    tmp_path: Path,
):
    mapping = tmp_path / "mapping.json"
    digest = _approved_mapping(
        mapping,
        extra_records=[
            {
                "category": "candidates",
                "record_id": "missing-candidate-id",
                "organization_id": "org-a",
                "evidence_reference": "ats:missing",
            }
        ],
    )
    report, exit_code = run_dry_run(
        legacy_database,
        mapping_file=mapping,
        approved_mapping_sha256=digest,
    )
    assert exit_code == 2
    assert report["mapping"]["invalid_source_references"] == 1
    assert report["integrity"]["passed"] is False


def test_mapping_digest_mismatch_is_rejected(
    legacy_database: Path,
    tmp_path: Path,
):
    mapping = tmp_path / "mapping.json"
    digest = _approved_mapping(mapping)

    with pytest.raises(DryRunError) as error:
        run_dry_run(
            legacy_database,
            mapping_file=mapping,
            approved_mapping_sha256="0" * 64 if digest != "0" * 64 else "1" * 64,
        )
    assert error.value.code == "approved_digest_mismatch"


def test_unapproved_manifest_provenance_is_rejected(
    legacy_database: Path,
    tmp_path: Path,
):
    mapping = tmp_path / "mapping.json"
    _approved_mapping(mapping)
    payload = json.loads(mapping.read_text(encoding="utf-8"))
    payload["approval"]["status"] = "proposed"
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    mapping.write_bytes(raw)

    with pytest.raises(DryRunError) as error:
        run_dry_run(
            legacy_database,
            mapping_file=mapping,
            approved_mapping_sha256=hashlib.sha256(raw).hexdigest(),
        )
    assert error.value.code == "mapping_provenance_incomplete"
