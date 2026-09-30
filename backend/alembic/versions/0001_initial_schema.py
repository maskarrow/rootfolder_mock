"""initial schema

1. The `vector` extension. A deploy that forgets pgvector fails here, loudly, and
   not at the first similarity query.
2. `orgs`, `users` (unique on `lower(email)`), `user_sessions` (only the token's
   SHA-256), `audit_log` (no FKs: it outlives the rows it describes; indexed for
   the per-email and per-IP login limits and for the retention cleanup).
3. `items` with `embedding vector(8)` under an HNSW cosine index, and `search`, a
   `tsvector` generated with the `romanian` configuration under a GIN index.
   `to_tsvector` is IMMUTABLE only with the configuration named explicitly, which
   generated columns require; a Postgres without `romanian` fails here.
4. `files` (`stored_path` relative to `STORAGE_DIR`) and `api_keys` (encrypted).

Revision ID: 0001
Revises:
Create Date: 2026-09-30 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "orgs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        _created_at(),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _created_at(),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_users_role_values"),
    )
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)
    op.create_index("ix_users_org_id", "users", ["org_id"])

    op.create_table(
        "user_sessions",
        sa.Column("token_hash", sa.LargeBinary(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        _created_at(),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        _created_at(),
    )
    op.create_index("ix_audit_log_email_created_at", "audit_log", ["email", "created_at"])
    op.create_index("ix_audit_log_ip_created_at", "audit_log", ["ip", "created_at"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    op.create_table(
        "items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(8), nullable=False),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('romanian', title || ' ' || body)", persisted=True),
        ),
        _created_at(),
    )
    op.create_index("ix_items_org_id", "items", ["org_id"])
    # GIN rather than GiST: reads far outnumber writes.
    op.create_index("ix_items_search", "items", ["search"], postgresql_using="gin")
    op.create_index(
        "ix_items_embedding_hnsw",
        "items",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column(
            "uploaded_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        _created_at(),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('processing', 'done', 'interrupted')", name="ck_files_status_values"
        ),
    )
    op.create_index("ix_files_org_id", "files", ["org_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("org_id", "provider", name="uq_api_keys_org_provider"),
    )


def downgrade() -> None:
    """Leaves the `vector` extension: other databases' objects may use it."""
    op.drop_table("api_keys")
    op.drop_table("files")
    op.drop_table("items")
    op.drop_table("audit_log")
    op.drop_table("user_sessions")
    op.drop_table("users")
    op.drop_table("orgs")
