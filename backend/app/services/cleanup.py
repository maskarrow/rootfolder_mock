"""The periodic cleanup: what does not clean itself up.

1. Dead sessions: `read_session` only deletes an expired session when someone
   shows up with it.
2. Audit entries older than a year.

Idempotent: a second run finds nothing, so two concurrent runs are harmless.
"""

import logging
from datetime import timedelta

from sqlalchemy import delete, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit import AuditEntry
from app.models.user_session import UserSession
from app.services import auth

logger = logging.getLogger(__name__)

AUDIT_RETENTION = timedelta(days=365)


def run(db: Session) -> dict:
    """One pass; returns how many rows of each kind were deleted. Commits."""
    moment = auth.now()
    sessions = db.execute(
        delete(UserSession).where(
            or_(
                UserSession.expires_at <= moment,
                UserSession.last_seen_at <= moment - timedelta(hours=settings.session_idle_hours),
            )
        )
    ).rowcount
    old_audit = db.execute(
        delete(AuditEntry).where(AuditEntry.created_at < moment - AUDIT_RETENTION)
    ).rowcount
    db.commit()

    report = {"sessions": sessions, "audit": old_audit}
    if sessions or old_audit:
        logger.info("Cleanup deleted %s sessions and %s audit entries", sessions, old_audit)
    return report
