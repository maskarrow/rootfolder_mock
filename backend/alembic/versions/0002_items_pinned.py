"""items.pinned

An additive change, so every deploy has more than one migration to run in order,
and the expand-only rule can be practised: the column has a server default, so the
previous version of the app, which does not know it, keeps inserting rows while
both versions run.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30 12:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("items", "pinned")
