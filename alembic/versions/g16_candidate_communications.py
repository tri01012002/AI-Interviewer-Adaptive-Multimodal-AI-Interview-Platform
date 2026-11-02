"""Add durable tenant-scoped candidate communications."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g16_candidate_communications"
down_revision: str | Sequence[str] | None = "g15_candidate_intake_screening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("recruitment_applications") as batch_op:
        batch_op.create_unique_constraint(
            "uq_recruitment_applications_org_campaign_id",
            ["organization_id", "campaign_id", "id"],
        )

    op.create_table(
        "candidate_communications",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("application_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("template_type", sa.String(length=64), nullable=False),
        sa.Column("template_version", sa.String(length=32), nullable=False),
        sa.Column("template_snapshot_json", sa.Text(), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="draft", nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("approved_by_user_id", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_name", sa.String(length=64), nullable=True),
        sa.Column("delivery_status", sa.String(length=32), nullable=True),
        sa.Column("provider_reference", sa.String(length=128), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "template_type IN ('interview_invitation', 'additional_information_request', "
            "'status_update', 'rejection_notification')",
            name="ck_candidate_communications_template_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'awaiting_approval', 'approved', 'sending', 'sent', "
            "'retryable_failure', 'needs_attention', 'cancelled', 'simulated')",
            name="ck_candidate_communications_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name="ck_candidate_communications_attempts",
        ),
        sa.CheckConstraint(
            "delivery_status IS NULL OR delivery_status IN ('simulated', 'sent', 'unknown')",
            name="ck_candidate_communications_delivery_status",
        ),
        sa.CheckConstraint(
            "length(subject) <= 255 AND length(body) <= 20000",
            name="ck_candidate_communications_content_length",
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'sending', 'sent', 'simulated', "
            "'retryable_failure', 'needs_attention') OR "
            "(approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_candidate_communications_approval_required",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["recruitment_campaigns.organization_id", "recruitment_campaigns.id"],
            name="fk_candidate_communications_org_campaign",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_id", "application_id"],
            [
                "recruitment_applications.organization_id",
                "recruitment_applications.campaign_id",
                "recruitment_applications.id",
            ],
            name="fk_candidate_communications_org_campaign_application",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_candidate_communications_creator",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name="fk_candidate_communications_approver",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_candidate_communications_org_id"
        ),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key",
            name="uq_candidate_communications_idempotency",
        ),
    )
    op.create_index(
        "ix_candidate_communications_dispatch",
        "candidate_communications",
        ["status", "available_at", "lease_expires_at"],
    )
    op.create_index(
        "ix_candidate_communications_org_status",
        "candidate_communications",
        ["organization_id", "status", "updated_at"],
    )

    op.create_table(
        "candidate_communication_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("communication_id", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=True),
        sa.Column("provider_name", sa.String(length=64), nullable=True),
        sa.Column("details_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "communication_id"],
            [
                "candidate_communications.organization_id",
                "candidate_communications.id",
            ],
            name="fk_candidate_communication_events_org_communication",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_candidate_communication_events_actor",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_candidate_communication_events_communication",
        "candidate_communication_events",
        ["organization_id", "communication_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_candidate_communication_events_communication",
        table_name="candidate_communication_events",
    )
    op.drop_table("candidate_communication_events")
    op.drop_index(
        "ix_candidate_communications_org_status",
        table_name="candidate_communications",
    )
    op.drop_index(
        "ix_candidate_communications_dispatch",
        table_name="candidate_communications",
    )
    op.drop_table("candidate_communications")
    with op.batch_alter_table("recruitment_applications") as batch_op:
        batch_op.drop_constraint(
            "uq_recruitment_applications_org_campaign_id", type_="unique"
        )
