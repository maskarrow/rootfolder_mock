import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuditEntry(Base):
    """An access event (login, failed login, logout, account created).

    `user_id` and `org_id` have no foreign keys on purpose: the log must outlive the
    account and org it describes, which is also why `email` is copied in. Failed
    logins are counted from here for the rate limits, so the limit survives restarts
    and is shared by every process.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_email_created_at", "email", "created_at"),
        Index("ix_audit_log_ip_created_at", "ip", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID | None]
    user_id: Mapped[uuid.UUID | None]
    event: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
