"""Add durable internal interview scheduling and reservations."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g18_internal_interview_scheduling"
down_revision: str | Sequence[str] | None = "g17_candidate_availability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "interview_schedules",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("application_id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("availability_request_id", sa.String(), nullable=False),
        sa.Column("availability_response_version", sa.Integer(), nullable=False),
        sa.Column("availability_window_index", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("interviewer_user_id", sa.String(), nullable=False),
        sa.Column("exclusive_resources_json", sa.Text(), nullable=False),
        sa.Column("rescheduled_from_id", sa.String(), nullable=True),
        sa.Column("reservation_token", sa.String(length=36), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancellation_reason", sa.String(length=256), nullable=True),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("confirmed_by_user_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'pending_confirmation', 'confirmed_internal', "
            "'reschedule_pending', 'cancelled', 'expired', 'needs_attention')",
            name="ck_interview_schedules_status",
        ),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 15 AND 240",
            name="ck_interview_schedules_duration",
        ),
        sa.CheckConstraint("end_at > start_at", name="ck_interview_schedules_interval"),
        sa.CheckConstraint("version > 0", name="ck_interview_schedules_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_id", "application_id", "candidate_id"],
            [
                "recruitment_applications.organization_id",
                "recruitment_applications.campaign_id",
                "recruitment_applications.id",
                "recruitment_applications.candidate_id",
            ],
            name="fk_interview_schedules_application",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "availability_request_id", "application_id", "candidate_id"],
            [
                "candidate_availability_requests.organization_id",
                "candidate_availability_requests.id",
                "candidate_availability_requests.application_id",
                "candidate_availability_requests.candidate_id",
            ],
            name="fk_interview_schedules_availability",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "interviewer_user_id"],
            ["organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_interview_schedules_interviewer_membership",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "rescheduled_from_id"],
            ["interview_schedules.organization_id", "interview_schedules.id"],
            name="fk_interview_schedules_rescheduled_from",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_interview_schedules_creator",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_user_id"],
            ["users.id"],
            name="fk_interview_schedules_confirmer",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_interview_schedules_org_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_interview_schedules_idempotency",
        ),
    )
    op.create_index(
        "uq_interview_schedules_confirmed_application",
        "interview_schedules",
        ["organization_id", "application_id"],
        unique=True,
        sqlite_where=sa.text("status = 'confirmed_internal'"),
        postgresql_where=sa.text("status = 'confirmed_internal'"),
    )
    op.create_index(
        "uq_interview_schedules_pending_replacement",
        "interview_schedules",
        ["organization_id", "rescheduled_from_id"],
        unique=True,
        sqlite_where=sa.text("status = 'reschedule_pending'"),
        postgresql_where=sa.text("status = 'reschedule_pending'"),
    )
    op.create_index(
        "ix_interview_schedules_expiration",
        "interview_schedules",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_interview_schedules_campaign",
        "interview_schedules",
        ["organization_id", "campaign_id", "created_at"],
    )

    op.create_table(
        "interview_schedule_reservations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("schedule_id", sa.String(), nullable=False),
        sa.Column("reservation_token", sa.String(length=36), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_key", sa.String(length=128), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "resource_type IN ('candidate', 'interviewer', 'exclusive')",
            name="ck_interview_schedule_reservations_resource_type",
        ),
        sa.CheckConstraint(
            "end_at > start_at",
            name="ck_interview_schedule_reservations_interval",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["interview_schedules.organization_id", "interview_schedules.id"],
            name="fk_interview_schedule_reservations_schedule",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "schedule_id",
            "reservation_token",
            "resource_type",
            "resource_key",
            name="uq_interview_schedule_reservations_resource",
        ),
    )
    op.create_index(
        "ix_interview_schedule_reservations_overlap",
        "interview_schedule_reservations",
        ["organization_id", "resource_type", "resource_key", "start_at", "end_at"],
    )
    op.create_index(
        "ix_interview_schedule_reservations_schedule",
        "interview_schedule_reservations",
        ["organization_id", "schedule_id", "reservation_token"],
    )

    op.create_table(
        "interview_schedule_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("schedule_id", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("details_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["interview_schedules.organization_id", "interview_schedules.id"],
            name="fk_interview_schedule_events_schedule",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_interview_schedule_events_actor",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_interview_schedule_events_schedule",
        "interview_schedule_events",
        ["organization_id", "schedule_id", "created_at"],
    )

    op.create_table(
        "interview_schedule_mutex",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        sa.table(
            "interview_schedule_mutex",
            sa.column("id", sa.String()),
            sa.column("generation", sa.Integer()),
        ),
        [{"id": "global", "generation": 0}],
    )


def downgrade() -> None:
    op.drop_table("interview_schedule_mutex")
    op.drop_index(
        "ix_interview_schedule_events_schedule",
        table_name="interview_schedule_events",
    )
    op.drop_table("interview_schedule_events")
    op.drop_index(
        "ix_interview_schedule_reservations_schedule",
        table_name="interview_schedule_reservations",
    )
    op.drop_index(
        "ix_interview_schedule_reservations_overlap",
        table_name="interview_schedule_reservations",
    )
    op.drop_table("interview_schedule_reservations")
    op.drop_index(
        "ix_interview_schedules_campaign",
        table_name="interview_schedules",
    )
    op.drop_index(
        "ix_interview_schedules_expiration",
        table_name="interview_schedules",
    )
    op.drop_index(
        "uq_interview_schedules_pending_replacement",
        table_name="interview_schedules",
    )
    op.drop_index(
        "uq_interview_schedules_confirmed_application",
        table_name="interview_schedules",
    )
    op.drop_table("interview_schedules")
