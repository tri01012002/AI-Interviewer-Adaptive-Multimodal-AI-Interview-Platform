"""Add persisted recruitment campaign workflow and approval records."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g12_recruitment_campaign_foundation"
down_revision: str | Sequence[str] | None = "f29_required_audit_request_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("alembic_version") as batch_op:
        batch_op.alter_column(
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=128),
            existing_nullable=False,
        )
    op.create_table(
        "recruitment_campaigns",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("director_hiring_brief", sa.Text(), nullable=False),
        sa.Column("structured_requirements_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("job_description", sa.Text(), nullable=True),
        sa.Column("competency_rubric_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("application_form_schema_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("approval_state", sa.String(), nullable=False, server_default="draft"),
        sa.Column("workflow_state", sa.String(), nullable=False, server_default="brief_created"),
        sa.Column("approval_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "approval_state IN ('draft', 'pending_review', 'approved', 'rejected')",
            name="ck_recruitment_campaigns_approval_state",
        ),
        sa.CheckConstraint(
            "workflow_state IN ('brief_created', 'requirements_defined', 'job_description_ready', 'rubric_ready', 'application_form_ready', 'pending_approval', 'approved', 'rejected')",
            name="ck_recruitment_campaigns_workflow_state",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_recruitment_campaigns_org_owner",
        "recruitment_campaigns",
        ["organization_id", "owner_user_id"],
    )
    op.create_index(
        "ix_recruitment_campaigns_workflow_state",
        "recruitment_campaigns",
        ["organization_id", "workflow_state", "approval_state"],
    )


def downgrade() -> None:
    op.drop_table("recruitment_campaigns")
