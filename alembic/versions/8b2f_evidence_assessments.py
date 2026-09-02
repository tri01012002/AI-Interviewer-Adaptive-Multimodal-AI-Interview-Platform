"""Add durable evidence metadata and per-turn competency assessments."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8b2f_evidence_assessments"
down_revision: Union[str, Sequence[str], None] = "7a1f_owner_and_turn_states"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("interview_evidence") as batch_op:
        batch_op.add_column(sa.Column("question_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("evidence_type", sa.String(), nullable=False, server_default="candidate_statement"))
        batch_op.add_column(sa.Column("relevance", sa.Float(), nullable=False, server_default="0.0"))
        batch_op.add_column(sa.Column("evaluator_type", sa.String(), nullable=False, server_default="deterministic"))
        batch_op.add_column(sa.Column("evaluator_version", sa.String(), nullable=False, server_default="phase-6-v1"))
        batch_op.create_foreign_key("fk_evidence_question_id", "interview_questions", ["question_id"], ["id"])

    op.create_table(
        "interview_assessments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("interview_id", sa.String(), nullable=False),
        sa.Column("turn_id", sa.String(), nullable=False),
        sa.Column("competency", sa.String(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="not_assessed"),
        sa.Column("evidence_strength", sa.String(), nullable=False, server_default="weak"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("strengths_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("gaps_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("evaluator_type", sa.String(), nullable=False, server_default="deterministic"),
        sa.Column("evaluator_version", sa.String(), nullable=False, server_default="phase-6-v1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["interview_id"], ["interviews.id"]),
        sa.ForeignKeyConstraint(["turn_id"], ["interview_turns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assessment_interview_id", "interview_assessments", ["interview_id"])
    op.create_index("ix_assessment_turn_id", "interview_assessments", ["turn_id"])
    op.create_index("ix_assessment_unique_turn_competency", "interview_assessments", ["interview_id", "turn_id", "competency"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_assessment_unique_turn_competency", table_name="interview_assessments")
    op.drop_index("ix_assessment_turn_id", table_name="interview_assessments")
    op.drop_index("ix_assessment_interview_id", table_name="interview_assessments")
    op.drop_table("interview_assessments")
    with op.batch_alter_table("interview_evidence") as batch_op:
        batch_op.drop_constraint("fk_evidence_question_id", type_="foreignkey")
        batch_op.drop_column("evaluator_version")
        batch_op.drop_column("evaluator_type")
        batch_op.drop_column("relevance")
        batch_op.drop_column("evidence_type")
        batch_op.drop_column("question_id")