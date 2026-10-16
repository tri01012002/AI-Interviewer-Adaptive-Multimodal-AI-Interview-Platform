"""Link campaign interviews to reviewed applications and internal schedules."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g20_campaign_interview_linkage"
down_revision: str | Sequence[str] | None = "g19_campaign_objective_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("recruitment_campaign_executions") as batch_op:
        batch_op.drop_constraint(
            "ck_recruitment_campaign_executions_status", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_recruitment_campaign_executions_status",
            "status IN ('pending', 'running', 'waiting_for_approval', 'completed', "
            "'awaiting_progress', 'retryable_failure', 'needs_attention')",
        )

    with op.batch_alter_table("interviews") as batch_op:
        batch_op.add_column(sa.Column("campaign_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("application_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("schedule_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("objective_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("plan_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("rubric_snapshot_json", sa.Text(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_interviews_campaign_application",
            ["organization_id", "campaign_id", "application_id"],
        )
        batch_op.create_check_constraint(
            "ck_interviews_campaign_link_complete",
            "campaign_id IS NULL OR (application_id IS NOT NULL AND schedule_id IS NOT NULL "
            "AND objective_version IS NOT NULL AND plan_version IS NOT NULL "
            "AND objective_version > 0 AND plan_version > 0 "
            "AND rubric_snapshot_json IS NOT NULL)",
        )
        batch_op.create_foreign_key(
            "fk_interviews_campaign_application",
            "recruitment_applications",
            ["organization_id", "campaign_id", "application_id", "candidate_id"],
            ["organization_id", "campaign_id", "id", "candidate_id"],
        )
        batch_op.create_foreign_key(
            "fk_interviews_schedule",
            "interview_schedules",
            ["organization_id", "schedule_id"],
            ["organization_id", "id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("interviews") as batch_op:
        batch_op.drop_constraint("fk_interviews_schedule", type_="foreignkey")
        batch_op.drop_constraint(
            "fk_interviews_campaign_application", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "ck_interviews_campaign_link_complete", type_="check"
        )
        batch_op.drop_constraint(
            "uq_interviews_campaign_application", type_="unique"
        )
        batch_op.drop_column("rubric_snapshot_json")
        batch_op.drop_column("plan_version")
        batch_op.drop_column("objective_version")
        batch_op.drop_column("schedule_id")
        batch_op.drop_column("application_id")
        batch_op.drop_column("campaign_id")

    with op.batch_alter_table("recruitment_campaign_executions") as batch_op:
        batch_op.drop_constraint(
            "ck_recruitment_campaign_executions_status", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_recruitment_campaign_executions_status",
            "status IN ('pending', 'running', 'waiting_for_approval', 'completed', "
            "'retryable_failure', 'needs_attention')",
        )
