from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

import services.organization_service as organization_service
from services.database import (
    AccessAuditRecord,
    Base,
    OrganizationRecord,
    UserRecord,
)


def test_organization_mutation_rolls_back_when_audit_insert_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit-rollback.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add_all(
            [
                UserRecord(
                    id="synthetic-platform-admin",
                    email="admin@example.test",
                    password_hash="synthetic-hash",
                    role="admin",
                ),
                UserRecord(
                    id="synthetic-org-admin",
                    email="org-admin@example.test",
                    password_hash="synthetic-hash",
                    role="candidate",
                    email_verified_at=datetime.now(UTC),
                ),
            ]
        )
    monkeypatch.setattr(organization_service, "SessionLocal", factory)

    def fail_audit_insert(*event_args):
        statement = event_args[2]
        if statement.lstrip().lower().startswith("insert") and "access_audit" in statement.lower():
            raise RuntimeError("synthetic audit persistence failure")

    event.listen(engine, "before_cursor_execute", fail_audit_insert)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit persistence failure"):
            organization_service.create_organization(
                "Synthetic Organization",
                "org-admin@example.test",
                "synthetic-platform-admin",
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_audit_insert)

    with factory() as session:
        organization_count = session.scalar(select(func.count()).select_from(OrganizationRecord))
        audit_count = session.scalar(select(func.count()).select_from(AccessAuditRecord))
    assert organization_count == 0
    assert audit_count == 0
    engine.dispose()
