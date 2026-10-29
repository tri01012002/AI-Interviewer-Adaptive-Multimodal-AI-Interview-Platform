"""merge havy and tri heads

Revision ID: d44c73fdf433
Revises: c8d1_interview_start_idempotency, g20_campaign_interview_linkage
Create Date: 2026-10-29 14:26:08.325494

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd44c73fdf433'
down_revision: Union[str, Sequence[str], None] = ('c8d1_interview_start_idempotency', 'g20_campaign_interview_linkage')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
