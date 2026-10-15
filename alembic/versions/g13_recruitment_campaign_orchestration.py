"""Persist recruitment campaign approval decisions and orchestration runs."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g13_recruitment_campaign_orchestration"
down_revision: str | Sequence[str] | None = "g12_recruitment_campaign_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("recruitment_campaigns") as batch_op:
        batch_op.add_column(
            sa.Column("approval_decided_by_user_id", sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("approval_decided_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_recruitment_campaigns_approval_decider",
            "users",
            ["approval_decided_by_user_id"],
            ["id"],
        )
    op.create_table(
        "recruitment_campaign_executions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("requested_by_user_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'waiting_for_approval', 'completed', "
            "'retryable_failure', 'needs_attention')",
            name="ck_recruitment_campaign_executions_status",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["recruitment_campaigns.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", name="uq_recruitment_campaign_execution_campaign"),
    )
    op.create_index(
        "ix_recruitment_campaign_executions_org_status",
        "recruitment_campaign_executions",
        ["organization_id", "status", "updated_at"],
    )
    op.create_table(
        "recruitment_campaign_execution_steps",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("execution_id", sa.String(), nullable=False),
        sa.Column("step_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=True),
        sa.Column("output_source", sa.String(), nullable=True),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'retryable_failure', "
            "'needs_attention')",
            name="ck_recruitment_campaign_execution_steps_status",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["recruitment_campaign_executions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "step_key",
            name="uq_recruitment_campaign_execution_step",
        ),
    )


def downgrade() -> None:
    op.drop_table("recruitment_campaign_execution_steps")
    op.drop_index(
        "ix_recruitment_campaign_executions_org_status",
        table_name="recruitment_campaign_executions",
    )
    op.drop_table("recruitment_campaign_executions")
    with op.batch_alter_table("recruitment_campaigns") as batch_op:
        batch_op.drop_constraint(
            "fk_recruitment_campaigns_approval_decider",
            type_="foreignkey",
        )
        batch_op.drop_column("approval_decided_at")
        batch_op.drop_column("approval_decided_by_user_id")
