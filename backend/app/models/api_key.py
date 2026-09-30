import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

ANTHROPIC = "anthropic"


class ApiKey(Base):
    """An org's key for a model provider, encrypted with the master key in the
    environment (`app/secrets.py`). One per org and provider; saving again replaces
    it. The key never leaves through the API."""

    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("org_id", "provider", name="uq_api_keys_org_provider"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cleared by a successful check.
    last_error: Mapped[str | None] = mapped_column(Text)
