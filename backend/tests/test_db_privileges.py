"""The app's database role can touch rows and nothing else.

deploy/scripts/migrate.py runs as the owner and creates the role, as on the server;
the test then connects as that role and tries what a bug that let SQL through
could try. Each refusal is a whole class of damage the app cannot do.
"""

import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db import engine

BACKEND = Path(__file__).resolve().parents[1]
MIGRATE_SCRIPT = BACKEND.parent / "deploy" / "scripts" / "migrate.py"
APP_ROLE = "rootfolder_test_app"


@pytest.fixture(scope="module")
def app_engine():
    password = secrets.token_hex(16)
    env = {**os.environ, "APP_DB_USER": APP_ROLE, "APP_DB_PASSWORD": password}
    result = subprocess.run(
        [sys.executable, "-"],
        input=MIGRATE_SCRIPT.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        cwd=BACKEND,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert f"Database role {APP_ROLE}" in result.stdout

    url = make_url(settings.database_url).set(username=APP_ROLE, password=password)
    role_engine = create_engine(url, isolation_level="AUTOCOMMIT")
    yield role_engine
    role_engine.dispose()


def _refused(role_engine, statement: str) -> bool:
    try:
        with role_engine.connect() as conn:
            conn.execute(text(statement))
    except DBAPIError as exc:
        return "permission denied" in str(exc) or "must be owner" in str(exc)
    return False


def test_the_role_reads_and_writes_rows(app_engine):
    org_id = uuid.uuid4()
    with app_engine.connect() as conn:
        conn.execute(text("INSERT INTO orgs (id, name) VALUES (:id, 'Role test')"), {"id": org_id})
        conn.execute(text("UPDATE orgs SET name = 'Renamed' WHERE id = :id"), {"id": org_id})
        assert conn.execute(text("SELECT name FROM orgs WHERE id = :id"), {"id": org_id}).scalar()
        conn.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE users",
        "TRUNCATE audit_log",
        "ALTER TABLE users ADD COLUMN is_superadmin boolean",
        "CREATE TABLE stolen (x text)",
        "SELECT version_num FROM alembic_version",
        "UPDATE alembic_version SET version_num = '0000'",
        "CREATE ROLE intruder SUPERUSER LOGIN",
        "ALTER ROLE rootfolder_test_app SUPERUSER",
        "COPY (SELECT 1) TO PROGRAM 'id'",
        "SELECT pg_read_file('/etc/passwd')",
    ],
)
def test_the_role_cannot_go_beyond_rows(app_engine, statement):
    assert _refused(app_engine, statement), statement


def test_the_role_is_not_a_superuser(app_engine):
    with app_engine.connect() as conn:
        is_super = conn.execute(
            text(
                "SELECT rolsuper OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname = :r"
            ),
            {"r": APP_ROLE},
        ).scalar()
    assert is_super is False


def test_a_table_added_later_is_granted_too(app_engine):
    """Default privileges: a migration (run as the owner) creates a table, and the
    role can use it without anyone remembering to grant it."""
    with engine.begin() as owner:
        owner.execute(text("CREATE TABLE later_table (id int)"))
    try:
        with app_engine.connect() as conn:
            conn.execute(text("INSERT INTO later_table VALUES (1)"))
            assert conn.execute(text("SELECT count(*) FROM later_table")).scalar() == 1
    finally:
        with engine.begin() as owner:
            owner.execute(text("DROP TABLE later_table"))


def test_every_query_has_a_time_limit():
    with engine.connect() as conn:
        limit = conn.execute(text("SHOW statement_timeout")).scalar()
    assert limit == f"{settings.db_statement_timeout_ms // 1000}s"
