"""Add interview ownership for resource authorization."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a1f_owner_and_turn_states"
down_revision: Union[str, Sequence[str], None] = "5f2c7d9a1b10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("interviews") as batch_op:
        batch_op.add_column(sa.Column("owner_user_id", sa.String(), nullable=True))
        batch_op.create_index("ix_interviews_owner_user_id", ["owner_user_id"])
        batch_op.create_foreign_key(
            "fk_interviews_owner_user_id_users",
            "users",
            ["owner_user_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("interviews") as batch_op:
        batch_op.drop_constraint("fk_interviews_owner_user_id_users", type_="foreignkey")
        batch_op.drop_index("ix_interviews_owner_user_id")
        batch_op.drop_column("owner_user_id")