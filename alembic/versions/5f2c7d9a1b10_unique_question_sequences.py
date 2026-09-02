"""Enforce unique question sequence numbers per interview."""

from typing import Sequence, Union

from alembic import op


revision: str = "5f2c7d9a1b10"
down_revision: Union[str, Sequence[str], None] = "304be4757bda"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        "ix_interview_questions_interview_id_sequence",
        table_name="interview_questions",
    )
    op.create_index(
        "ix_interview_questions_interview_id_sequence",
        "interview_questions",
        ["interview_id", "sequence_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_interview_questions_interview_id_sequence",
        table_name="interview_questions",
    )
    op.create_index(
        "ix_interview_questions_interview_id_sequence",
        "interview_questions",
        ["interview_id", "sequence_number"],
    )