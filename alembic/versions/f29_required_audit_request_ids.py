"""Add idempotency keys for mandatory access-audit attempts."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f29_required_audit_request_ids"
down_revision: str | Sequence[str] | None = "e84_rag_tenant_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "access_audit",
        sa.Column("request_id", sa.String(), nullable=True),
    )
    op.create_index(
        "uq_access_audit_request_id",
        "access_audit",
        ["request_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_access_audit_request_id", table_name="access_audit")
    op.drop_column("access_audit", "request_id")
