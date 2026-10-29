"""Alembic migrations from an empty disposable PostgreSQL database to head."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from services.database import Base, engine

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="migration verification needs a disposable PostgreSQL DATABASE_URL",
)

EXPECTED_HEAD = "g20_campaign_interview_linkage"
REPOSITORY = Path(__file__).resolve().parents[2]
# Tables owned by the recruitment campaign workflow: no schema drift allowed.
CAMPAIGN_TABLE_PREFIXES = (
    "recruitment_",
    "candidate_",
    "interview_schedule",
)
# Known drift, reported rather than fixed (no schema changes in this task):
# the models declare index=True on these columns but no migration creates them.
# They are plain lookup indexes; no uniqueness guarantee depends on them.
KNOWN_MISSING_CAMPAIGN_INDEXES = {
    "ix_recruitment_campaign_executions_organization_id",
    "ix_recruitment_campaigns_organization_id",
    "ix_recruitment_campaigns_owner_user_id",
}


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPOSITORY,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.fixture
def empty_database_url():
    url = make_url(os.environ["DATABASE_URL"])
    name = f"{url.database}_migration"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_migration_history_has_single_expected_head():
    scripts = ScriptDirectory.from_config(Config(str(REPOSITORY / "alembic.ini")))
    assert scripts.get_heads() == [EXPECTED_HEAD]
    revisions = list(scripts.walk_revisions())
    assert len(revisions) == 19
    assert all(not isinstance(item.down_revision, tuple) for item in revisions)


def test_empty_postgres_database_upgrades_to_head_matching_models(empty_database_url):
    upgraded = _alembic(empty_database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr[-4000:]
    current = _alembic(empty_database_url, "current")
    assert current.returncode == 0, current.stderr[-4000:]
    assert f"{EXPECTED_HEAD} (head)" in current.stdout

    migrated = create_engine(empty_database_url)
    try:
        with migrated.connect() as connection:
            differences = compare_metadata(
                MigrationContext.configure(connection, opts={"compare_type": True}),
                Base.metadata,
            )
    finally:
        migrated.dispose()

    def table_of(difference) -> str:
        item = difference[0] if isinstance(difference, list) else difference
        target = item[1] if not isinstance(difference, list) else item
        table = getattr(target, "table", None)
        if table is not None:
            return table.name
        return str(item[2]) if isinstance(difference, list) else str(target)

    structural = [
        item
        for item in differences
        if not isinstance(item, list)
        and item[0] in {"add_table", "remove_table", "add_column", "remove_column"}
    ]
    campaign_drift = [
        item for item in differences if table_of(item).startswith(CAMPAIGN_TABLE_PREFIXES)
    ]
    assert structural == []
    assert all(
        not isinstance(item, list)
        and item[0] == "add_index"
        and not item[1].unique
        and item[1].name in KNOWN_MISSING_CAMPAIGN_INDEXES
        for item in campaign_drift
    ), campaign_drift
    assert {item[1].name for item in campaign_drift} == KNOWN_MISSING_CAMPAIGN_INDEXES
