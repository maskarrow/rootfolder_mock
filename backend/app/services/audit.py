"""The access log: the only writer of `audit_log`.

Events are constants here, not strings typed in routes, so a typo cannot silently
create an event nobody counts.

Failed logins are counted from here for rate limiting, so the limit lives in the
database: it survives restarts and is shared by every process.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditEntry
from app.models.user import User

LOGIN = "login"
LOGIN_FAILED = "login_failed"
LOGOUT = "logout"
ACCOUNT_CREATED = "account_created"


def write(
    db: Session,
    event: str,
    *,
    user: User | None = None,
    email: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Writes an entry and commits. The email comes from `user` unless given, for
    when the user does not exist (an unknown email at login)."""
    db.add(
        AuditEntry(
            event=event,
            org_id=user.org_id if user else None,
            user_id=user.id if user else None,
            email=email or (user.email if user else None),
            ip=ip,
            user_agent=user_agent,
        )
    )
    db.commit()


def recent_failures(
    db: Session, *, since: datetime, email: str | None = None, ip: str | None = None
) -> int:
    query = select(func.count()).where(
        AuditEntry.event == LOGIN_FAILED, AuditEntry.created_at >= since
    )
    if email is not None:
        query = query.where(AuditEntry.email == email)
    if ip is not None:
        query = query.where(AuditEntry.ip == ip)
    return db.scalar(query) or 0
