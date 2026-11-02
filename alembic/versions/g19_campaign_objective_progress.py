"""Add versioned campaign objectives, approval history, and explicit outcomes."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g19_campaign_objective_progress"
down_revision: str | Sequence[str] | None = "g18_internal_interview_scheduling"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("recruitment_campaigns") as batch_op:
        batch_op.add_column(
            sa.Column("objective_json", sa.Text(), server_default="{}", nullable=False)
        )
        batch_op.add_column(
            sa.Column("objective_version", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(
            sa.Column("plan_version", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(
            sa.Column("approved_objective_version", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("approved_plan_version", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("objective_update_idempotency_key", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(
            sa.Column("objective_update_request_hash", sa.String(length=64), nullable=True)
        )

    op.create_table(
        "recruitment_campaign_approvals",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("objective_version", sa.Integer(), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decided_by_user_id", sa.String(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_recruitment_campaign_approvals_decision",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["recruitment_campaigns.organization_id", "recruitment_campaigns.id"],
            name="fk_recruitment_campaign_approvals_campaign",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_recruitment_campaign_approvals_org_id"),
    )
    op.create_index(
        "ix_recruitment_campaign_approvals_campaign_version",
        "recruitment_campaign_approvals",
        ["organization_id", "campaign_id", "objective_version", "plan_version"],
    )

    op.create_table(
        "recruitment_campaign_hiring_outcomes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("application_id", sa.String(), nullable=False),
        sa.Column("objective_version", sa.Integer(), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("criteria_satisfied", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_by_user_id", sa.String(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('hired', 'rejected', 'withdrawn')",
            name="ck_recruitment_campaign_hiring_outcomes_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_id", "application_id"],
            [
                "recruitment_applications.organization_id",
                "recruitment_applications.campaign_id",
                "recruitment_applications.id",
            ],
            name="fk_recruitment_campaign_hiring_outcomes_application",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "campaign_id", "application_id",
            name="uq_recruitment_campaign_hiring_outcome_application",
        ),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key",
            name="uq_recruitment_campaign_hiring_outcome_idempotency",
        ),
    )
    op.create_index(
        "ix_recruitment_campaign_hiring_outcomes_campaign",
        "recruitment_campaign_hiring_outcomes",
        ["organization_id", "campaign_id", "outcome", "decided_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recruitment_campaign_hiring_outcomes_campaign",
        table_name="recruitment_campaign_hiring_outcomes",
    )
    op.drop_table("recruitment_campaign_hiring_outcomes")
    op.drop_index(
        "ix_recruitment_campaign_approvals_campaign_version",
        table_name="recruitment_campaign_approvals",
    )
    op.drop_table("recruitment_campaign_approvals")
    with op.batch_alter_table("recruitment_campaigns") as batch_op:
        batch_op.drop_column("objective_update_request_hash")
        batch_op.drop_column("objective_update_idempotency_key")
        batch_op.drop_column("approved_plan_version")
        batch_op.drop_column("approved_objective_version")
        batch_op.drop_column("plan_version")
        batch_op.drop_column("objective_version")
        batch_op.drop_column("objective_json")
