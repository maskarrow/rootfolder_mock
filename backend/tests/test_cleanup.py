"""The periodic cleanup: dead sessions and year-old audit entries go, the rest stays."""

from datetime import timedelta

from sqlalchemy import select

from app.models.audit import AuditEntry
from app.models.user_session import UserSession
from app.services import auth, cleanup


def test_cleanup_deletes_only_what_is_dead(db, admin):
    now = auth.now()
    live = auth.hash_token("live")
    db.add_all(
        [
            UserSession(
                token_hash=live,
                user_id=admin.id,
                last_seen_at=now,
                expires_at=now + timedelta(days=1),
            ),
            UserSession(
                token_hash=auth.hash_token("idle"),
                user_id=admin.id,
                last_seen_at=now - timedelta(hours=9),
                expires_at=now + timedelta(days=1),
            ),
            UserSession(
                token_hash=auth.hash_token("expired"),
                user_id=admin.id,
                last_seen_at=now,
                expires_at=now - timedelta(seconds=1),
            ),
            AuditEntry(event="login", created_at=now - timedelta(days=366)),
            AuditEntry(event="login", created_at=now - timedelta(days=364)),
        ]
    )
    db.commit()

    report = cleanup.run(db)

    assert report == {"sessions": 2, "audit": 1}
    assert db.scalars(select(UserSession.token_hash)).all() == [live]
    assert len(db.scalars(select(AuditEntry)).all()) == 1
    # Idempotent: a second pass finds nothing.
    assert cleanup.run(db) == {"sessions": 0, "audit": 0}
