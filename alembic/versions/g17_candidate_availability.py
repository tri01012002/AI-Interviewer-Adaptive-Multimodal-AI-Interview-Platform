"""Add durable candidate availability collection."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g17_candidate_availability"
down_revision: str | Sequence[str] | None = "g16_candidate_communications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("recruitment_applications") as batch_op:
        batch_op.create_unique_constraint(
            "uq_recruitment_applications_org_campaign_id_candidate",
            ["organization_id", "campaign_id", "id", "candidate_id"],
        )

    op.create_table(
        "candidate_availability_requests",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("application_id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interview_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("proposed_windows_json", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="pending_response", nullable=False
        ),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("reviewed_by_user_id", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_response', 'responded', 'reviewed', 'expired', 'cancelled')",
            name="ck_candidate_availability_requests_status",
        ),
        sa.CheckConstraint(
            "interview_duration_minutes BETWEEN 15 AND 240",
            name="ck_candidate_availability_requests_duration",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_id", "application_id", "candidate_id"],
            [
                "recruitment_applications.organization_id",
                "recruitment_applications.campaign_id",
                "recruitment_applications.id",
                "recruitment_applications.candidate_id",
            ],
            name="fk_candidate_availability_requests_application",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_candidate_availability_requests_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_candidate_availability_requests_creator",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
            name="fk_candidate_availability_requests_reviewer",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_candidate_availability_requests_org_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "application_id",
            name="uq_candidate_availability_requests_application",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_candidate_availability_requests_idempotency",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            "application_id",
            "candidate_id",
            name="uq_candidate_availability_requests_response_parent",
        ),
    )
    op.create_index(
        "ix_candidate_availability_requests_expiration",
        "candidate_availability_requests",
        ["status", "response_deadline"],
    )
    op.create_index(
        "ix_candidate_availability_requests_campaign",
        "candidate_availability_requests",
        ["organization_id", "campaign_id", "created_at"],
    )
    op.create_table(
        "candidate_availability_responses",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("application_id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("response_hash", sa.String(length=64), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("windows_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("review_status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("reviewed_window_indices_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("reviewed_by_user_id", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "review_status IN ('pending', 'reviewed')",
            name="ck_candidate_availability_responses_review_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_candidate_availability_responses_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "request_id", "application_id", "candidate_id"],
            [
                "candidate_availability_requests.organization_id",
                "candidate_availability_requests.id",
                "candidate_availability_requests.application_id",
                "candidate_availability_requests.candidate_id",
            ],
            name="fk_candidate_availability_responses_request",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
            name="fk_candidate_availability_responses_reviewer",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "request_id",
            name="uq_candidate_availability_responses_request",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "request_id",
            "idempotency_key",
            name="uq_candidate_availability_responses_idempotency",
        ),
    )
    op.create_index(
        "ix_candidate_availability_responses_application",
        "candidate_availability_responses",
        ["organization_id", "application_id"],
    )
    op.create_table(
        "candidate_availability_submissions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("response_hash", sa.String(length=64), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "request_id"],
            [
                "candidate_availability_requests.organization_id",
                "candidate_availability_requests.id",
            ],
            name="fk_candidate_availability_submissions_request",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "request_id",
            "idempotency_key",
            name="uq_candidate_availability_submissions_idempotency",
        ),
    )
    op.create_index(
        "ix_candidate_availability_submissions_request",
        "candidate_availability_submissions",
        ["organization_id", "request_id", "submitted_at"],
    )
    op.create_table(
        "candidate_availability_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("details_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "request_id"],
            [
                "candidate_availability_requests.organization_id",
                "candidate_availability_requests.id",
            ],
            name="fk_candidate_availability_events_request",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_candidate_availability_events_actor",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_candidate_availability_events_request",
        "candidate_availability_events",
        ["organization_id", "request_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_candidate_availability_events_request",
        table_name="candidate_availability_events",
    )
    op.drop_table("candidate_availability_events")
    op.drop_index(
        "ix_candidate_availability_submissions_request",
        table_name="candidate_availability_submissions",
    )
    op.drop_table("candidate_availability_submissions")
    op.drop_index(
        "ix_candidate_availability_responses_application",
        table_name="candidate_availability_responses",
    )
    op.drop_table("candidate_availability_responses")
    op.drop_index(
        "ix_candidate_availability_requests_campaign",
        table_name="candidate_availability_requests",
    )
    op.drop_index(
        "ix_candidate_availability_requests_expiration",
        table_name="candidate_availability_requests",
    )
    op.drop_table("candidate_availability_requests")
    with op.batch_alter_table("recruitment_applications") as batch_op:
        batch_op.drop_constraint(
            "uq_recruitment_applications_org_campaign_id_candidate",
            type_="unique",
        )
