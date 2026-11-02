"""Add tenant-scoped candidate applications, CV processing, and screening evidence."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g15_candidate_intake_screening"
down_revision: str | Sequence[str] | None = "g14_recruitment_campaign_dispatcher"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("recruitment_campaigns") as batch_op:
        batch_op.create_unique_constraint(
            "uq_recruitment_campaigns_org_id", ["organization_id", "id"]
        )

    op.create_table(
        "recruitment_applications",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("review_status", sa.String(), server_default="pending", nullable=False),
        sa.Column("reviewed_by_user_id", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retryable_failure', "
            "'needs_review', 'needs_attention', 'failed')",
            name="ck_recruitment_applications_status",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'reviewed')",
            name="ck_recruitment_applications_review_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["recruitment_campaigns.organization_id", "recruitment_campaigns.id"],
            name="fk_recruitment_applications_org_campaign",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_recruitment_applications_org_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["users.id"],
            name="fk_recruitment_applications_reviewer",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_recruitment_applications_org_id"
        ),
        sa.UniqueConstraint(
            "organization_id", "campaign_id", "candidate_id",
            name="uq_recruitment_applications_campaign_candidate",
        ),
    )
    op.create_index(
        "ix_recruitment_applications_organization_id",
        "recruitment_applications",
        ["organization_id"],
    )
    op.create_index(
        "ix_recruitment_applications_dispatch",
        "recruitment_applications",
        ["status", "available_at", "lease_expires_at"],
    )

    op.create_table(
        "candidate_cv_documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("application_id", sa.String(), nullable=False),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("file_extension", sa.String(), nullable=False),
        sa.Column("media_type", sa.String(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("parse_status", sa.String(), server_default="pending", nullable=False),
        sa.Column("parser_version", sa.String(), nullable=True),
        sa.Column("parsed_output_json", sa.Text(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("parse_error_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "parse_status IN ('pending', 'parsed', 'failed')",
            name="ck_candidate_cv_documents_parse_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "application_id"],
            ["recruitment_applications.organization_id", "recruitment_applications.id"],
            name="fk_candidate_cv_documents_org_application",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_candidate_cv_documents_storage_key"),
        sa.UniqueConstraint(
            "organization_id", "application_id",
            name="uq_candidate_cv_documents_org_application",
        ),
    )

    op.create_table(
        "candidate_screenings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("application_id", sa.String(), nullable=False),
        sa.Column("rubric_version", sa.String(length=64), nullable=False),
        sa.Column("rubric_snapshot_json", sa.Text(), nullable=False),
        sa.Column("result_source", sa.String(), server_default="deterministic", nullable=False),
        sa.Column("review_required", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "application_id"],
            ["recruitment_applications.organization_id", "recruitment_applications.id"],
            name="fk_candidate_screenings_org_application",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "application_id",
            name="uq_candidate_screenings_org_application",
        ),
    )

    op.create_table(
        "candidate_criterion_assessments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("screening_id", sa.String(), nullable=False),
        sa.Column("criterion_id", sa.String(), nullable=False),
        sa.Column("criterion_title", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("evidence_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("uncertainty", sa.Text(), nullable=False),
        sa.Column("review_required", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("review_status", sa.String(), server_default="pending", nullable=False),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "review_status IN ('pending', 'confirmed', 'needs_correction')",
            name="ck_candidate_criterion_assessments_review_status",
        ),
        sa.ForeignKeyConstraint(
            ["screening_id"], ["candidate_screenings.id"],
            name="fk_candidate_criterion_assessments_screening",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["users.id"],
            name="fk_candidate_criterion_assessments_reviewer",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "screening_id", "criterion_id",
            name="uq_candidate_criterion_assessments_screening_criterion",
        ),
    )


def downgrade() -> None:
    op.drop_table("candidate_criterion_assessments")
    op.drop_table("candidate_screenings")
    op.drop_table("candidate_cv_documents")
    op.drop_index(
        "ix_recruitment_applications_dispatch",
        table_name="recruitment_applications",
    )
    op.drop_index(
        "ix_recruitment_applications_organization_id",
        table_name="recruitment_applications",
    )
    op.drop_table("recruitment_applications")
    with op.batch_alter_table("recruitment_campaigns") as batch_op:
        batch_op.drop_constraint(
            "uq_recruitment_campaigns_org_id", type_="unique"
        )
