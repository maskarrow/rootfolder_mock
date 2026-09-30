import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

PROCESSING = "processing"
DONE = "done"
INTERRUPTED = "interrupted"


class File(Base):
    """An uploaded PDF.

    `stored_path` is only the name inside `STORAGE_DIR` (`<uuid>.pdf`), never a full
    path: the directory differs between a laptop and a server, and a full path would
    break every row when the storage moves.
    """

    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'done', 'interrupted')", name="ck_files_status_values"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=PROCESSING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
