"""Add durable retry scheduling and worker controls."""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "g14_recruitment_campaign_dispatcher"
down_revision: str | Sequence[str] | None = "g13_recruitment_campaign_orchestration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recruitment_campaign_executions",
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_recruitment_campaign_executions_dispatch",
        "recruitment_campaign_executions",
        ["status", "available_at", "lease_expires_at"],
    )
    op.execute(
        sa.text(
            "UPDATE recruitment_campaign_executions "
            "SET available_at = CURRENT_TIMESTAMP "
            "WHERE status = 'retryable_failure' AND available_at IS NULL"
        )
    )
    op.create_table(
        "recruitment_campaign_worker_control",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        sa.table(
            "recruitment_campaign_worker_control",
            sa.column("id", sa.String()),
            sa.column("paused", sa.Boolean()),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        [
            {
                "id": "recruitment_campaigns",
                "paused": False,
                "updated_at": datetime.now(UTC),
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("recruitment_campaign_worker_control")
    op.drop_index(
        "ix_recruitment_campaign_executions_dispatch",
        table_name="recruitment_campaign_executions",
    )
    op.drop_column("recruitment_campaign_executions", "available_at")
