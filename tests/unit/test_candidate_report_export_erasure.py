"""Candidate erasure removes report exports (scoped and legacy) and blocks new ones."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from config import settings
from services import report_service as report_module
from services.report_service import (
    ReportExportUnavailable,
    ReportService,
    _format_text_report,
    report_export_directory,
)
from tests.unit.tenant_test_utils import tenant_headers
from tests.unit.test_campaign_lifecycle import _setup_offline_env
from tests.unit.test_candidate_data_deletion import (
    ErasableCandidate,
    _audit_rows,
    _endpoint,
    _make_candidate,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _setup_offline_env(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture
def report_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    root = tmp_path_factory.mktemp("report-exports")
    monkeypatch.setattr(settings, "STORAGE_PATH", str(root))
    return root


def _admin(data: ErasableCandidate) -> dict[str, str]:
    return tenant_headers(data.tenant.org_admin_headers, data.tenant.organization_id)


def _export(client: TestClient, data: ErasableCandidate, fmt: str = "json"):
    return client.post(
        f"/api/v1/interview/{data.interview_id}/report?format={fmt}",
        headers=tenant_headers(data.tenant.recruiter_headers, data.tenant.organization_id),
    )


def _scoped_dir(root: Path, data: ErasableCandidate) -> Path:
    return report_export_directory(root, data.tenant.organization_id, data.interview_id)


def _write_legacy(root: Path, report: dict, fmt: str) -> Path:
    path = root / f"report_{uuid4()}.{fmt}"
    content = json.dumps(report, indent=2) if fmt == "json" else _format_text_report(report)
    path.write_text(content, encoding="utf-8")
    return path


def _assert_logs_clean(caplog: pytest.LogCaptureFixture, data: ErasableCandidate) -> None:
    needles = (data.marker, data.name, data.candidate_email)
    for record in caplog.records:
        rendered = record.getMessage() + " " + " ".join(
            str(value) for value in record.__dict__.values()
        )
        for needle in needles:
            assert needle not in rendered, record.getMessage()


def test_erasure_removes_scoped_and_legacy_exports_only_for_that_candidate(
    client: TestClient,
    offline: Path,
    report_root: Path,
    caplog: pytest.LogCaptureFixture,
):
    data = _make_candidate(client)
    other = _make_candidate(client)  # separate organization

    exported = _export(client, data, "json")
    assert exported.status_code == 200, exported.text
    assert exported.json()["report_file"].startswith("report_")
    assert exported.json()["report_file"].endswith(".json")
    assert _export(client, data, "txt").status_code == 200
    assert _export(client, other, "json").status_code == 200

    scoped = _scoped_dir(report_root, data)
    other_scoped = _scoped_dir(report_root, other)
    assert sorted(path.suffix for path in scoped.iterdir()) == [".json", ".txt"]
    assert (scoped / exported.json()["report_file"]).is_file()
    assert len(list(other_scoped.iterdir())) == 1

    report = exported.json()["report"]
    legacy_json = _write_legacy(report_root, report, "json")
    legacy_txt = _write_legacy(report_root, report, "txt")
    other_report = _export(client, other, "json").json()["report"]
    other_legacy = _write_legacy(report_root, other_report, "json")
    # Evidence text quoting the erased interview id must not make another
    # candidate's export look like the erased candidate's.
    forged = dict(other_report)
    forged["summary"] = f'\n  "interview_id": "{data.interview_id}"'
    forged_legacy = _write_legacy(report_root, forged, "json")
    unrelated = report_root / "unrelated.json"
    unrelated.write_text("{}", encoding="utf-8")

    caplog.set_level(logging.DEBUG)
    response = client.delete(_endpoint(data.candidate_id), headers=_admin(data))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["pending_file_count"] == 0
    assert not scoped.exists()
    assert not legacy_json.exists() and not legacy_txt.exists()
    assert other_legacy.is_file() and forged_legacy.is_file() and unrelated.is_file()
    assert len(list(other_scoped.iterdir())) == 2
    assert sorted(row.outcome for row in _audit_rows(data.candidate_id)) == [
        "completed",
        "pending",
    ]
    assert _audit_rows(other.candidate_id) == []
    _assert_logs_clean(caplog, data)

    repeated = client.delete(_endpoint(data.candidate_id), headers=_admin(data))
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "completed"
    assert repeated.json()["already_erased"] is True
    assert len(list(other_scoped.iterdir())) == 2


def test_export_deletion_failure_is_pending_and_retry_completes(
    client: TestClient,
    offline: Path,
    report_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    data = _make_candidate(client)
    exported = _export(client, data)
    assert exported.status_code == 200, exported.text
    export_path = _scoped_dir(report_root, data) / exported.json()["report_file"]
    legacy = _write_legacy(report_root, exported.json()["report"], "txt")

    def failing_remove(path: Path) -> None:
        raise PermissionError("synthetic export storage outage")

    caplog.set_level(logging.DEBUG)
    with monkeypatch.context() as patch:
        patch.setattr(report_module, "_remove_export_file", failing_remove)
        failed = client.delete(_endpoint(data.candidate_id), headers=_admin(data))

    assert failed.status_code == 202, failed.text
    assert failed.json()["status"] == "deletion_pending"
    assert failed.json()["retryable"] is True
    assert failed.json()["pending_file_count"] == 2
    assert export_path.is_file() and legacy.is_file()
    outcomes = [row.outcome for row in _audit_rows(data.candidate_id)]
    assert "partial" in outcomes and "completed" not in outcomes
    _assert_logs_clean(caplog, data)

    retried = client.delete(_endpoint(data.candidate_id), headers=_admin(data))
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "completed"
    assert retried.json()["already_erased"] is False
    assert not export_path.exists() and not legacy.exists()
    final = [row.outcome for row in _audit_rows(data.candidate_id)]
    assert final.count("completed") == 1 and final.count("partial") == 1

    # Files already gone are treated as deleted: a further retry stays complete.
    again = client.delete(_endpoint(data.candidate_id), headers=_admin(data))
    assert again.status_code == 200
    assert again.json()["already_erased"] is True


def test_erased_interview_cannot_produce_an_export(
    client: TestClient, offline: Path, report_root: Path
):
    data = _make_candidate(client)
    built_before_erasure = _export(client, data).json()["report"]
    erased = client.delete(_endpoint(data.candidate_id), headers=_admin(data))
    assert erased.status_code == 200, erased.text

    response = _export(client, data)
    assert response.status_code == 410, response.text
    assert response.json()["detail"]["code"] == "candidate_personal_data_erased"

    # A report built before erasure but written afterwards is refused too.
    with pytest.raises(ReportExportUnavailable):
        ReportService().write_report(built_before_erasure, "json")

    leftovers = [path for path in report_root.rglob("*") if path.is_file()]
    assert leftovers == []
