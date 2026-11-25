"""Policy-neutral retention rules, legal hold, dry run and guarded execution."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import app
from config import settings
from services.candidate_cv_storage import CandidateCVStorage
from services.candidate_data_deletion import ERASED_TEXT, erased_candidate_email
from services.database import (
    AccessAuditRecord,
    CandidateRecord,
    RecruitmentApplicationRecord,
    RecruitmentCampaignHiringOutcomeRecord,
    SessionLocal,
)
from services.retention_policy import (
    RetentionConfigError,
    RetentionExecutionDisabled,
    RetentionPolicyService,
    load_rules,
    main,
)
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    tenant_headers,
)
from tests.unit.test_campaign_lifecycle import _setup_offline_env
from tests.unit.test_candidate_data_deletion import ErasableCandidate, _make_candidate
from tests.unit.test_recruitment_candidate_intake import (
    create_approved_campaign,
    submit_cv,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _setup_offline_env(monkeypatch, tmp_path)
    return tmp_path


def _rules(min_age_days: int = 30, **extra) -> list:
    rule = {
        "id": "test-unsuccessful",
        "category": "unsuccessful_candidate",
        "age_basis": "hiring_outcome_decided_at",
        "min_age_days": min_age_days,
        **extra,
    }
    return load_rules(json.dumps([rule]), use_settings=False)


def _backdate(application_id: str, days: int) -> None:
    then = datetime.now(UTC) - timedelta(days=days)
    with SessionLocal.begin() as session:
        session.execute(
            update(RecruitmentCampaignHiringOutcomeRecord)
            .where(RecruitmentCampaignHiringOutcomeRecord.application_id == application_id)
            .values(decided_at=then)
        )
        session.execute(
            update(RecruitmentApplicationRecord)
            .where(RecruitmentApplicationRecord.id == application_id)
            .values(updated_at=then)
        )


def _audits(organization_id: str, prefix: str) -> list[AccessAuditRecord]:
    with SessionLocal() as session:
        return list(
            session.execute(
                select(AccessAuditRecord)
                .where(
                    AccessAuditRecord.organization_id == organization_id,
                    AccessAuditRecord.action.like(f"{prefix}%"),
                )
                .order_by(AccessAuditRecord.created_at)
            ).scalars()
        )


def _selected(report) -> list[str]:
    return sorted(cid for cid, _ in report.planned())


def _is_erased(candidate_id: str) -> bool:
    with SessionLocal() as session:
        candidate = session.get(CandidateRecord, candidate_id)
        return candidate.email == erased_candidate_email(candidate_id)


def _assert_no_pii(data: ErasableCandidate, *texts: str) -> None:
    for text in texts:
        assert data.marker not in text
        assert data.name not in text
        assert data.candidate_email not in text


def _assert_db_and_files_untouched(data: ErasableCandidate) -> None:
    assert CandidateCVStorage.exists(data.storage_key)
    with SessionLocal() as session:
        candidate = session.get(CandidateRecord, data.candidate_id)
        assert candidate.name == data.name
        assert candidate.user_id == data.candidate_user_id
    assert _audits(data.tenant.organization_id, "candidate_personal_data_erasure_") == []
    assert _audits(data.tenant.organization_id, "retention_erasure_") == []


def test_rule_configuration_is_validated_and_has_no_builtin_rules(monkeypatch):
    monkeypatch.setattr(settings, "RETENTION_RULES_JSON", None)
    assert load_rules() == []
    monkeypatch.setattr(settings, "RETENTION_RULES_JSON", "  ")
    assert load_rules() == []
    for bad in (
        "{}",
        "not json",
        json.dumps([{"id": "a", "category": "unsuccessful_candidate",
                     "age_basis": "hiring_outcome_decided_at"}]),
        json.dumps([{"id": "a", "category": "everyone",
                     "age_basis": "hiring_outcome_decided_at", "min_age_days": 1}]),
        json.dumps([{"id": "a", "category": "unsuccessful_candidate",
                     "age_basis": "campaign_closed_at", "min_age_days": 1}]),
        json.dumps([{"id": "a", "category": "unsuccessful_candidate",
                     "age_basis": "hiring_outcome_decided_at", "min_age_days": 0}]),
        json.dumps([{"id": "a b", "category": "unsuccessful_candidate",
                     "age_basis": "hiring_outcome_decided_at", "min_age_days": 1}]),
    ):
        with pytest.raises(RetentionConfigError):
            load_rules(bad, use_settings=False)


def test_no_rules_selects_nothing_and_cannot_execute(
    client: TestClient, offline: Path, monkeypatch, capsys
):
    data = _make_candidate(client)
    _backdate(data.application_id, 3650)
    org = data.tenant.organization_id
    monkeypatch.setattr(settings, "RETENTION_RULES_JSON", None)
    monkeypatch.setattr(settings, "RETENTION_EXECUTION_ENABLED", True)

    report = RetentionPolicyService.run(org)
    assert report.planned() == [] and report.rules == []

    with pytest.raises(RetentionExecutionDisabled):
        RetentionPolicyService.run(org, execute=True)
    assert main(["--organization-id", org, "--execute"]) == 3
    capsys.readouterr()
    _assert_db_and_files_untouched(data)


def test_rule_selects_only_eligible_candidates_in_its_organization(
    client: TestClient, offline: Path
):
    eligible = _make_candidate(client)
    other_org = _make_candidate(client)
    tenant = eligible.tenant
    org = tenant.organization_id
    _backdate(eligible.application_id, 400)
    _backdate(other_org.application_id, 400)

    # Same organization: a hired candidate and a candidate with no application.
    _, hired_headers, hired_profile, _ = create_candidate_through_invitation(client, tenant)
    create_candidate_through_invitation(client, tenant)
    campaign_id = create_approved_campaign(client, tenant)
    uploaded = submit_cv(client, tenant, hired_headers, campaign_id)
    assert uploaded.status_code == 202, uploaded.text
    hired_application = uploaded.json()["id"]
    with SessionLocal.begin() as session:
        session.add(
            RecruitmentCampaignHiringOutcomeRecord(
                organization_id=org,
                campaign_id=campaign_id,
                application_id=hired_application,
                objective_version=1,
                plan_version=1,
                outcome="hired",
                criteria_satisfied=True,
                idempotency_key=f"retention-hired-{hired_application}",
                request_hash="0" * 64,
                reason="synthetic",
                decided_by_user_id=tenant.org_admin["id"],
                decided_at=datetime.now(UTC) - timedelta(days=400),
            )
        )

    report = RetentionPolicyService.evaluate(org, _rules(30))
    assert _selected(report) == [eligible.candidate_id]
    assert hired_profile["candidate_id"] not in _selected(report)

    # Too recent for the configured age: nothing.
    assert _selected(RetentionPolicyService.evaluate(org, _rules(1000))) == []
    # A rule restricted to another organization does not apply here.
    scoped = _rules(30, organization_ids=[other_org.tenant.organization_id])
    assert _selected(RetentionPolicyService.evaluate(org, scoped)) == []
    assert _selected(
        RetentionPolicyService.evaluate(other_org.tenant.organization_id, scoped)
    ) == [other_org.candidate_id]
    # candidate_without_hire also never selects the hired candidate.
    without_hire = load_rules(json.dumps([{
        "id": "no-hire", "category": "candidate_without_hire",
        "age_basis": "application_last_activity", "min_age_days": 30,
    }]), use_settings=False)
    assert _selected(RetentionPolicyService.evaluate(org, without_hire)) == [
        eligible.candidate_id
    ]


def test_dry_run_changes_nothing_and_output_has_no_pii(
    client: TestClient, offline: Path, monkeypatch, capsys, caplog
):
    data = _make_candidate(client)
    _backdate(data.application_id, 400)
    org = data.tenant.organization_id
    files_before = sorted(path.name for path in offline.rglob("*"))
    monkeypatch.setattr(settings, "RETENTION_RULES_JSON", json.dumps([{
        "id": "dry", "category": "unsuccessful_candidate",
        "age_basis": "hiring_outcome_decided_at", "min_age_days": 30,
    }]))
    monkeypatch.setattr(settings, "RETENTION_EXECUTION_ENABLED", True)

    with caplog.at_level(logging.DEBUG):
        assert main(["--organization-id", org]) == 0
    output = capsys.readouterr().out
    parsed = json.loads(output)
    rule = parsed["organizations"][0]["rules"][0]
    assert parsed["organizations"][0]["mode"] == "dry_run"
    assert rule["candidate_ids"] == [data.candidate_id] and rule["selected_count"] == 1

    assert sorted(path.name for path in offline.rglob("*")) == files_before
    _assert_db_and_files_untouched(data)
    with SessionLocal() as session:
        assert data.marker in session.execute(
            select(RecruitmentCampaignHiringOutcomeRecord.reason).where(
                RecruitmentCampaignHiringOutcomeRecord.application_id
                == data.application_id
            )
        ).scalar_one()
    previews = _audits(org, "retention_preview")
    assert len(previews) == 1 and previews[0].outcome == "dry_run"
    assert previews[0].actor_user_id is None
    logs = " ".join(str(record.__dict__) for record in caplog.records)
    _assert_no_pii(data, output, logs, str(previews[0].support_reason))


def test_legal_hold_excludes_from_retention_and_blocks_manual_erasure(
    client: TestClient, offline: Path
):
    data = _make_candidate(client)
    _backdate(data.application_id, 400)
    tenant = data.tenant
    org = tenant.organization_id
    hold_url = f"/api/v1/candidates/{data.candidate_id}/legal-hold"
    admin = tenant_headers(tenant.org_admin_headers, org)

    denied = client.put(
        hold_url, json={"reason": "Synthetic matter"},
        headers=tenant_headers(tenant.recruiter_headers, org),
    )
    assert denied.status_code == 403
    held = client.put(hold_url, json={"reason": "Synthetic matter"}, headers=admin)
    assert held.status_code == 200, held.text
    assert held.json()["legal_hold"] is True

    report = RetentionPolicyService.evaluate(org, _rules(30))
    assert _selected(report) == []
    assert report.rules[0].excluded_legal_hold == 1

    erase = client.delete(f"/api/v1/candidates/{data.candidate_id}/personal-data", headers=admin)
    assert erase.status_code == 409, erase.text
    assert erase.json()["detail"] == {
        "code": "candidate_erasure_blocked", "reason": "legal_hold"
    }
    assert CandidateCVStorage.exists(data.storage_key)
    with SessionLocal() as session:
        assert session.get(CandidateRecord, data.candidate_id).name == data.name
    assert [
        (row.action, row.outcome)
        for row in _audits(org, "candidate_")
        if row.resource_id == data.candidate_id
        and ("legal_hold" in row.action or "erasure" in row.action)
    ] == [
        ("candidate_legal_hold_set", "allowed"),
        ("candidate_personal_data_erasure_blocked", "blocked"),
    ]

    released = client.delete(hold_url, headers=admin)
    assert released.status_code == 200 and released.json()["legal_hold"] is False
    with SessionLocal() as session:
        candidate = session.get(CandidateRecord, data.candidate_id)
        assert candidate.legal_hold_reason is None
    assert _selected(RetentionPolicyService.evaluate(org, _rules(30))) == [
        data.candidate_id
    ]


def test_execution_requires_enable_flag(client: TestClient, offline: Path, monkeypatch, capsys):
    data = _make_candidate(client)
    _backdate(data.application_id, 400)
    org = data.tenant.organization_id
    monkeypatch.setattr(settings, "RETENTION_EXECUTION_ENABLED", False)

    with pytest.raises(RetentionExecutionDisabled):
        RetentionPolicyService.run(org, execute=True, rules=_rules(30))
    monkeypatch.setattr(settings, "RETENTION_RULES_JSON", json.dumps([{
        "id": "guarded", "category": "unsuccessful_candidate",
        "age_basis": "hiring_outcome_decided_at", "min_age_days": 30,
    }]))
    assert main(["--organization-id", org, "--execute"]) == 3
    capsys.readouterr()
    _assert_db_and_files_untouched(data)


def test_execution_erases_audits_and_is_idempotent(
    client: TestClient, offline: Path, monkeypatch, caplog
):
    data = _make_candidate(client)
    bystander = _make_candidate(client)
    _backdate(data.application_id, 400)
    _backdate(bystander.application_id, 400)
    org = data.tenant.organization_id
    monkeypatch.setattr(settings, "RETENTION_EXECUTION_ENABLED", True)

    with caplog.at_level(logging.DEBUG):
        report = RetentionPolicyService.run(org, execute=True, rules=_rules(30))
    body = report.as_dict()
    assert body["results"] == [{
        "candidate_id": data.candidate_id, "rule_id": "test-unsuccessful",
        "status": "completed",
    }]
    assert body["complete"] is True
    assert _is_erased(data.candidate_id)
    assert not CandidateCVStorage.exists(data.storage_key)
    with SessionLocal() as session:
        assert session.get(CandidateRecord, data.candidate_id).name == ERASED_TEXT
    _assert_db_and_files_untouched(bystander)

    retention = _audits(org, "retention_erasure_")
    assert sorted((row.action, row.outcome) for row in retention) == [
        ("retention_erasure_completed", "completed"),
        ("retention_erasure_started", "pending"),
    ]
    assert all(row.support_reason == "retention_rule:test-unsuccessful" for row in retention)
    erasure = _audits(org, "candidate_personal_data_erasure_")
    assert {row.outcome for row in erasure} == {"pending", "completed"}
    assert all(row.actor_user_id is None for row in retention + erasure)
    logs = " ".join(str(record.__dict__) for record in caplog.records)
    _assert_no_pii(data, json.dumps(body), logs)

    rerun = RetentionPolicyService.run(org, execute=True, rules=_rules(30))
    assert rerun.results == []
    assert rerun.rules[0].excluded_already_erased == 1
    assert len(_audits(org, "retention_erasure_")) == 2


def test_partial_file_failure_is_pending_not_success_and_retry_completes(
    client: TestClient, offline: Path, monkeypatch
):
    data = _make_candidate(client)
    _backdate(data.application_id, 400)
    org = data.tenant.organization_id
    monkeypatch.setattr(settings, "RETENTION_EXECUTION_ENABLED", True)

    def failing_delete(storage_key: str) -> None:
        raise OSError("synthetic storage outage")

    with monkeypatch.context() as patch:
        patch.setattr(CandidateCVStorage, "delete", staticmethod(failing_delete))
        report = RetentionPolicyService.run(org, execute=True, rules=_rules(30))
    body = report.as_dict()
    assert body["complete"] is False
    assert body["results"][0]["status"] == "deletion_pending"
    assert body["results"][0]["pending_file_count"] == 1
    assert CandidateCVStorage.exists(data.storage_key)
    actions = [(row.action, row.outcome) for row in _audits(org, "retention_erasure_")]
    assert ("retention_erasure_pending", "partial") in actions
    assert all(outcome != "completed" for _, outcome in actions)

    # The redacted candidate no longer looks "recent"-aged by rule; it is retried.
    retry = RetentionPolicyService.run(org, execute=True, rules=_rules(30))
    assert retry.pending_retry == {data.candidate_id: "test-unsuccessful"}
    assert [item["status"] for item in retry.results] == ["completed"]
    assert not CandidateCVStorage.exists(data.storage_key)

    final = RetentionPolicyService.run(org, execute=True, rules=_rules(30))
    assert final.results == [] and final.pending_retry == {}
