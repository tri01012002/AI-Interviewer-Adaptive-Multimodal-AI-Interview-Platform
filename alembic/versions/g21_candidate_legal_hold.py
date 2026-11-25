"""Add a legal-hold flag to candidates (blocks erasure and retention selection)."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g21_candidate_legal_hold"
down_revision: str | Sequence[str] | None = "g20_campaign_interview_linkage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("candidates") as batch_op:
        batch_op.add_column(
            sa.Column(
                "legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(sa.Column("legal_hold_reason", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("legal_hold_set_by_user_id", sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("legal_hold_set_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_candidates_legal_hold_set_by_user",
            "users",
            ["legal_hold_set_by_user_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("candidates") as batch_op:
        batch_op.drop_constraint("fk_candidates_legal_hold_set_by_user", type_="foreignkey")
        batch_op.drop_column("legal_hold_set_at")
        batch_op.drop_column("legal_hold_set_by_user_id")
        batch_op.drop_column("legal_hold_reason")
        batch_op.drop_column("legal_hold")
