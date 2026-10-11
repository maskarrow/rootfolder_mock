"""Migration 0003 on data that already exists, and deploy/scripts/migrate.py.

These tests move the test database's schema down and back up, so they put it back
in a `finally`: a failure must not leave later tests on an old schema. Alembic's
`fileConfig` also replaces the root logger's handlers, which the log tests need.
"""

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import engine

BACKEND = Path(__file__).resolve().parents[1]
MIGRATE_SCRIPT = BACKEND.parent / "deploy" / "scripts" / "migrate.py"
HEAD = "0003"


@pytest.fixture
def alembic_config():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield Config(str(BACKEND / "alembic.ini"))
    root.handlers[:] = handlers
    root.setLevel(level)


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


def test_last_login_is_filled_in_for_accounts_that_already_exist(alembic_config):
    org_id = "00000000-0000-4000-8000-0000000000a1"
    logged_in = "00000000-0000-4000-8000-0000000000b1"
    never_logged_in = "00000000-0000-4000-8000-0000000000b2"

    command.downgrade(alembic_config, "0002")
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO orgs (id, name) VALUES (:id, 'Old Org')"), {"id": org_id}
            )
            for user_id, email in ((logged_in, "a@old.test"), (never_logged_in, "b@old.test")):
                conn.execute(
                    text(
                        "INSERT INTO users (id, org_id, email, name, password_hash, role) "
                        "VALUES (:id, :org, :email, 'Old', 'x', 'member')"
                    ),
                    {"id": user_id, "org": org_id, "email": email},
                )
            events = [
                ("login", logged_in, _at(2)),
                ("login", logged_in, _at(9)),  # the latest one is the answer
                ("login_failed", logged_in, _at(10)),  # failures do not count
                ("login_failed", never_logged_in, _at(5)),
            ]
            for event, user_id, when in events:
                conn.execute(
                    text(
                        "INSERT INTO audit_log (id, user_id, event, created_at) "
                        "VALUES (gen_random_uuid(), :user, :event, :at)"
                    ),
                    {"user": user_id, "event": event, "at": when},
                )

        command.upgrade(alembic_config, "head")

        with engine.connect() as conn:
            rows = dict(conn.execute(text("SELECT id::text, last_login_at FROM users")).all())
        assert rows[logged_in] == _at(9)
        assert rows[never_logged_in] is None
    finally:
        command.upgrade(alembic_config, "head")


def test_login_records_the_time_and_me_returns_it(anon_client, admin):
    assert (
        anon_client.post(
            "/auth/login", json={"email": admin.email, "password": "irrelevant-wrong-password"}
        ).status_code
        == 401
    )

    from conftest import PASSWORD

    response = anon_client.post("/auth/login", json={"email": admin.email, "password": PASSWORD})

    assert response.status_code == 200
    assert response.json()["last_login_at"] is not None
    assert anon_client.get("/auth/me").json()["last_login_at"] == response.json()["last_login_at"]


def _run_migrate_script(**env: str) -> subprocess.CompletedProcess:
    """As deploy.sh does in the api container: the script on standard input, from the
    folder that holds alembic.ini."""
    return subprocess.run(
        [sys.executable, "-"],
        input=MIGRATE_SCRIPT.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        cwd=BACKEND,
        timeout=120,
        env={**os.environ, **env},
    )


def test_migrate_script_brings_the_database_to_head():
    result = _run_migrate_script()

    assert result.returncode == 0, result.stderr
    assert "up to date" in result.stdout
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == HEAD


def test_migrate_script_skips_when_the_database_is_ahead_of_the_image():
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '0099'"))
    try:
        result = _run_migrate_script()

        assert result.returncode == 0, result.stderr
        assert "newer than this image knows" in result.stdout
        with engine.connect() as conn:
            # Untouched: the script must not "fix" a database that is ahead.
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == "0099"
    finally:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": HEAD})


def test_a_migration_kept_waiting_for_a_table_gives_up_and_changes_nothing(alembic_config):
    command.downgrade(alembic_config, "0002")
    try:
        with engine.connect() as busy:
            # An open transaction that has read `users`, which 0003 alters: the
            # migration has to wait for it to end.
            busy.execute(text("SELECT 1 FROM users LIMIT 1"))
            result = _run_migrate_script(
                MIGRATION_LOCK_TIMEOUT="300ms", MIGRATION_LOCK_ATTEMPTS="1"
            )
            busy.rollback()

        assert result.returncode == 1
        assert "nothing was changed" in result.stderr
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            columns = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = 'last_login_at'"
                )
            ).all()
        assert version == "0002"
        assert columns == []
    finally:
        command.upgrade(alembic_config, "head")
