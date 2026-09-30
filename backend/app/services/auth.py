"""Passwords and sessions.

Passwords use argon2id with `argon2-cffi` defaults; the hash carries its own
parameters, so they can be raised later without invalidating old passwords.

Sessions live in the database (`UserSession`) rather than in a signed token, so
they can be revoked immediately. The cookie holds 32 random bytes; the database
holds only their SHA-256. Plain SHA-256 is enough because the token has 256 bits
of entropy, and it is computed on every request.

Time comes from `now()` so tests can move past the idle timeout.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import User
from app.models.user_session import UserSession

logger = logging.getLogger(__name__)

COOKIE = "mock_session"

# Every argon2 verification costs CPU and memory; this stops megabyte "passwords".
PASSWORD_MAX = 1024

# `last_seen_at` is written at most once a minute per session, not on every request.
_ACTIVITY_THRESHOLD = timedelta(minutes=1)

_hasher = PasswordHasher()
_dummy_hash: str | None = None


def now() -> datetime:
    return datetime.now(UTC)


def password_problem(password: str) -> str | None:
    """Why a new password is rejected, or `None`."""
    if len(password) < settings.password_min_length:
        return f"The password must be at least {settings.password_min_length} characters."
    if len(password) > PASSWORD_MAX:
        return f"The password can be at most {PASSWORD_MAX} characters."
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """`password_hash` is `None` for an unknown email.

    Then a dummy hash is verified so the response takes as long as a real one;
    otherwise timing would reveal which emails have accounts. Over-long passwords
    go through the dummy too, for the same reason.
    """
    if password_hash is None or len(password) > PASSWORD_MAX:
        _verify_dummy(password)
        return False
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except VerificationError, InvalidHashError:
        logger.error("Unreadable password hash in the database; the account cannot log in")
        return False


def _verify_dummy(password: str) -> None:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _hasher.hash(secrets.token_urlsafe(16))
    try:
        _hasher.verify(_dummy_hash, password[:PASSWORD_MAX])
    except VerificationError:
        pass


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True)
class AuthContext:
    session: UserSession
    user: User


def open_session(db: Session, user: User) -> str:
    """Creates the session and returns the plaintext token for the cookie. Commits.

    The absolute limit is the same with or without "remember me"; that flag only
    decides whether the browser keeps the cookie after closing (`routers/auth.py`).
    """
    token = secrets.token_urlsafe(32)
    moment = now()
    db.add(
        UserSession(
            token_hash=hash_token(token),
            user_id=user.id,
            last_seen_at=moment,
            expires_at=moment + timedelta(days=settings.session_max_days),
        )
    )
    db.commit()
    return token


def read_session(db: Session, token: str | None) -> AuthContext | None:
    """The valid session behind the token, or `None`.

    Invalid sessions are deleted, not just refused, so reactivating a user does not
    revive old sessions.
    """
    if not token:
        return None

    row = db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == hash_token(token))
    ).first()
    if row is None:
        return None
    session, user = row

    moment = now()
    valid = (
        session.expires_at > moment
        and session.last_seen_at > moment - timedelta(hours=settings.session_idle_hours)
        and user.is_active
    )
    if not valid:
        db.delete(session)
        db.commit()
        return None

    if moment - session.last_seen_at >= _ACTIVITY_THRESHOLD:
        session.last_seen_at = moment
        db.commit()
    return AuthContext(session=session, user=user)


def close_session(db: Session, token: str | None) -> None:
    if not token:
        return
    db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(token)))
    db.commit()
