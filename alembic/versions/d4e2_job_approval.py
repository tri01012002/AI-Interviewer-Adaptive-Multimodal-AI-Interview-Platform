"""Add durable job approval metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e2_job_approval"
down_revision: str | Sequence[str] | None = "c8d1_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("approved_by", sa.String(), nullable=True))
    op.add_column(
        "jobs", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("jobs", "approved_at")
    op.drop_column("jobs", "approved_by")