from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from alembic import command
from alembic.config import Config


def test_candidate_invitation_migration_preserves_legacy_profiles(monkeypatch):
    project_root = Path(__file__).resolve().parents[2]
    storage = project_root / "storage"
    storage.mkdir(exist_ok=True)
    database_path = storage / f"test_candidate_migration_{uuid4().hex}.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(project_root / "alembic.ini"))
    engine = None

    try:
        command.upgrade(config, "a4d2_resource_ownership")
        engine = create_engine(database_url)
        now = datetime.now(UTC).isoformat()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email, password_hash, role, created_at, updated_at) "
                    "VALUES (:id, :email, :password_hash, :role, :created_at, :updated_at)"
                ),
                {
                    "id": "legacy-user",
                    "email": "legacy@example.com",
                    "password_hash": "not-a-real-password-hash",
                    "role": "candidate",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO candidates "
                    "(id, name, email, owner_user_id, user_id, phone, resume_url, "
                    "created_at, updated_at) "
                    "VALUES (:id, :name, :email, NULL, NULL, NULL, NULL, :created_at, :updated_at)"
                ),
                {
                    "id": "legacy-profile",
                    "name": "Legacy Candidate",
                    "email": "legacy-profile@example.com",
                    "created_at": now,
                    "updated_at": now,
                },
            )

        command.upgrade(config, "head")
        command.downgrade(config, "a4d2_resource_ownership")
        command.upgrade(config, "head")

        with engine.connect() as connection:
            candidate = connection.execute(
                text(
                    "SELECT id, name, email, owner_user_id, user_id, organization_id "
                    "FROM candidates WHERE id = 'legacy-profile'"
                )
            ).one()
            verified_at = connection.execute(
                text("SELECT email_verified_at FROM users WHERE id = 'legacy-user'")
            ).scalar_one()
            invitation_count = connection.execute(
                text("SELECT count(*) FROM candidate_invitations")
            ).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()

        assert candidate == (
            "legacy-profile",
            "Legacy Candidate",
            "legacy-profile@example.com",
            None,
            None,
            None,
        )
        assert verified_at is None
        assert invitation_count == 0
        assert revision == "d3f7_tenant_isolation"
    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)


def test_tenant_downgrade_refuses_to_discard_tenant_and_support_audit_data(monkeypatch):
    project_root = Path(__file__).resolve().parents[2]
    storage = project_root / "storage"
    storage.mkdir(exist_ok=True)
    database_path = storage / f"test_tenant_downgrade_{uuid4().hex}.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(project_root / "alembic.ini"))
    engine = None

    try:
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        now = datetime.now(UTC).isoformat()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email, password_hash, role, created_at, updated_at) "
                    "VALUES (:id, :email, :password_hash, :role, :created_at, :updated_at)"
                ),
                {
                    "id": "tenant-admin",
                    "email": "tenant-admin@example.com",
                    "password_hash": "not-a-real-password-hash",
                    "role": "recruiter",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO organizations "
                    "(id, name, status, created_by_user_id, created_at, updated_at) "
                    "VALUES (:id, :name, :status, :created_by_user_id, :created_at, :updated_at)"
                ),
                {
                    "id": "tenant-a",
                    "name": "Tenant A",
                    "status": "active",
                    "created_by_user_id": "tenant-admin",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO organization_memberships "
                    "(id, organization_id, user_id, role, status, granted_by_user_id, "
                    "created_at, revoked_at) "
                    "VALUES (:id, :organization_id, :user_id, :role, :status, "
                    ":granted_by_user_id, :created_at, NULL)"
                ),
                {
                    "id": "tenant-a-admin",
                    "organization_id": "tenant-a",
                    "user_id": "tenant-admin",
                    "role": "org_admin",
                    "status": "active",
                    "granted_by_user_id": "tenant-admin",
                    "created_at": now,
                },
            )

        with pytest.raises(RuntimeError, match="Cannot downgrade tenant data safely"):
            command.downgrade(config, "b6c9_candidate_invitations")

        with engine.connect() as connection:
            organization_count = connection.execute(
                text("SELECT count(*) FROM organizations")
            ).scalar_one()
            membership_count = connection.execute(
                text("SELECT count(*) FROM organization_memberships")
            ).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()

        assert organization_count == 1
        assert membership_count == 1
        assert revision == "d3f7_tenant_isolation"

        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM organization_memberships "
                    "WHERE id = 'tenant-a-admin'"
                )
            )
            connection.execute(
                text("DELETE FROM organizations WHERE id = 'tenant-a'")
            )
            connection.execute(
                text(
                    "INSERT INTO access_audit "
                    "(id, actor_user_id, action, resource_type, resource_id, created_at, "
                    "organization_id, requested_organization_id, outcome, support_reason) "
                    "VALUES ('audit-unmapped-support', NULL, 'support_access', 'organization', "
                    "'unmapped-organization', :created_at, NULL, "
                    "'unmapped-organization', 'denied', 'regression test')"
                ),
                {"created_at": now},
            )

        with pytest.raises(RuntimeError, match="Cannot downgrade tenant data safely"):
            command.downgrade(config, "b6c9_candidate_invitations")

        with engine.connect() as connection:
            audit_metadata_count = connection.execute(
                text(
                    "SELECT count(*) FROM access_audit "
                    "WHERE requested_organization_id = 'unmapped-organization'"
                )
            ).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()

        assert audit_metadata_count == 1
        assert revision == "d3f7_tenant_isolation"
    finally:
        if engine is not None:
            engine.dispose()
        database_path.unlink(missing_ok=True)
