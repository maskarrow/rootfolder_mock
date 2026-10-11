"""users.last_login_at

A database change with data: the column is added (nullable, so it is instant and
the previous release, which does not know it, keeps working on the new schema),
then filled in for the accounts that already exist from their latest `login`
entry in the audit log. An account that never logged in stays empty.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE users
        SET last_login_at = latest.at
        FROM (
            SELECT user_id, max(created_at) AS at
            FROM audit_log
            WHERE event = 'login' AND user_id IS NOT NULL
            GROUP BY user_id
        ) AS latest
        WHERE latest.user_id = users.id
        """
    )


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
