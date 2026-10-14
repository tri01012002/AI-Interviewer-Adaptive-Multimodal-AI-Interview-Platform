"""Add recruiter-owned jobs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8d1_jobs"
down_revision: str | Sequence[str] | None = "b6c9_candidate_invitations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requirements_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_owner_user_id", "jobs", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_owner_user_id", table_name="jobs")
    op.drop_table("jobs")