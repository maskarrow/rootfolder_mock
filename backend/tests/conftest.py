"""The test environment and its guarantees.

pytest reads this before any test file, and the order matters: environment
variables are written before the first `import app`, because `app.config.settings`
is a singleton built at import time.

1. No writes to the real database: tests run on `<db>_test`, recreated from the
   migrations every session and refused unless its name ends in `_test`.
2. No network: Anthropic and Resend are faked, and a ban on `socket.connect`
   raises on any host but the database.
3. No files in the repo: `STORAGE_DIR` is a temporary folder.
4. No waiting: the job and the stream's silence last 0 seconds.

Why `DATABASE_URL` rather than `dependency_overrides`: jobs and startup open
`SessionLocal()` directly, so an override would leave them on the real database.
Why `TRUNCATE` after each test rather than a rolled-back transaction: those same
places open their own sessions and would not see a fixture's uncommitted rows.
"""

import logging
import os
import secrets
import shutil
import socket
import tempfile
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def _test_url():
    """Same Postgres server, another database: `DATABASE_URL` with `_test` appended."""
    raw = os.environ.get("DATABASE_URL") or dotenv_values(ROOT / ".env").get("DATABASE_URL")
    if not raw:
        raise RuntimeError(
            "DATABASE_URL is missing from both the environment and .env; tests need "
            "Postgres running (`just up`)"
        )
    url = make_url(raw)
    return url.set(database=f"{url.database}_test")


_TEST_URL = _test_url()
_TEST_STORAGE = tempfile.mkdtemp(prefix="delegate-mock-tests-")

os.environ["DATABASE_URL"] = _TEST_URL.render_as_string(hide_password=False)
os.environ["STORAGE_DIR"] = _TEST_STORAGE
# The test client talks to `http://testserver`, and httpx does not send back a
# `Secure` cookie over HTTP.
os.environ["SESSION_COOKIE_SECURE"] = "false"
# Local whatever the machine's `.env` says, so startup does not refuse; tests that
# want a public address set it themselves.
os.environ["APP_URL"] = "http://localhost:3010"
os.environ["CORS_ORIGINS"] = "http://localhost:3010"
os.environ["RESEND_API_KEY"] = ""
# No cleanup on a parallel task mid-test; its tests call it directly.
os.environ["CLEANUP_INTERVAL_HOURS"] = "0"
os.environ["JOB_SECONDS"] = "0"
os.environ["STREAM_SILENCE_S"] = "0"
# Generated per session rather than written as a literal: a fixed key in the repo
# could end up copied into a real environment.
os.environ["API_KEYS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app.config import settings  # noqa: E402  (after the environment, on purpose)
from app.db import SessionLocal, engine  # noqa: E402
from app.models.item import Item  # noqa: E402
from app.models.org import Org  # noqa: E402
from app.models.user import ADMIN, User  # noqa: E402
from app.services import auth  # noqa: E402

# Generated for the same reason as the master key. Every test user has it.
PASSWORD = secrets.token_urlsafe(16)


def _check_database() -> None:
    """The last net before a test writes to the real database."""
    if make_url(settings.database_url).database.endswith("_test"):
        return
    raise RuntimeError(
        f"Tests refuse to start: the database name does not end in `_test` "
        f"({make_url(settings.database_url).database})"
    )


_check_database()

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost", str(_TEST_URL.host or "localhost")}


class NetworkCallForbidden(RuntimeError):
    """A test tried to reach the network: something was not faked."""


@pytest.fixture(autouse=True, scope="session")
def _no_network():
    """Blocks every connection except the database, on `socket.socket.connect`,
    below every library at once. The test client opens no socket."""
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if str(host) not in _ALLOWED_HOSTS:
            raise NetworkCallForbidden(f"Test reaching the network at {host!r}")
        return real_connect(self, address, *args, **kwargs)

    socket.socket.connect = guarded_connect
    try:
        yield
    finally:
        socket.socket.connect = real_connect


def _drop_test_database(admin) -> None:
    with admin.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": _TEST_URL.database},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{_TEST_URL.database}"'))


@pytest.fixture(scope="session", autouse=True)
def _test_database(_no_network):
    """Recreates the test database and migrates it with Alembic, so every run also
    checks that the migration chain climbs from zero."""
    admin = create_engine(_TEST_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")
    _drop_test_database(admin)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{_TEST_URL.database}"'))

    from alembic import command
    from alembic.config import Config

    # `fileConfig` in `alembic/env.py` replaces the root logger's handlers and level;
    # put them back, or the app's INFO lines would never reach the log tests.
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")
    root.handlers[:] = handlers
    root.setLevel(level)

    yield

    engine.dispose()
    _drop_test_database(admin)
    admin.dispose()
    shutil.rmtree(_TEST_STORAGE, ignore_errors=True)


_TABLES = ("api_keys", "files", "items", "audit_log", "user_sessions", "users", "orgs")


@pytest.fixture(autouse=True)
def _clean_database(_test_database):
    """Empties the tables after each test (after, so a failing test's rows can still
    be inspected while the session runs)."""
    yield
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} CASCADE"))


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


class FakeResend:
    """`emails._send_resend`, replaced: records what would have been sent."""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.error: Exception | None = None

    def __call__(self, to: str, subject: str, text: str) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append((to, subject, text))


@pytest.fixture(autouse=True)
def resend(monkeypatch) -> FakeResend:
    fake = FakeResend()
    monkeypatch.setattr("app.emails._send_resend", fake)
    return fake


class FakeAnthropic:
    """`checks._list_models`, replaced: answers `response`, or raises `error`, and
    records the keys it was given."""

    def __init__(self):
        self.keys: list[str] = []
        self.response = httpx.Response(200, json={"data": [{"id": "claude-test"}]})
        self.error: Exception | None = None

    def __call__(self, key: str) -> httpx.Response:
        self.keys.append(key)
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def anthropic(monkeypatch) -> FakeAnthropic:
    fake = FakeAnthropic()
    monkeypatch.setattr("app.routers.checks._list_models", fake)
    return fake


@pytest.fixture
def make_org(db):
    def _make(name: str = "Test Org") -> Org:
        org = Org(name=name)
        db.add(org)
        db.commit()
        return org

    return _make


@pytest.fixture
def make_user(db):
    """A user whose password is `PASSWORD`. Emails are unique per call."""
    counter = iter(range(1, 1000))

    def _make(org: Org, *, role: str = ADMIN, email: str | None = None, active=True) -> User:
        user = User(
            org_id=org.id,
            email=email or f"user{next(counter)}@{org.name.lower().replace(' ', '')}.test",
            name="Test User",
            role=role,
            is_active=active,
            password_hash=auth.hash_password(PASSWORD),
        )
        db.add(user)
        db.commit()
        return user

    return _make


@pytest.fixture
def make_item(db):
    def _make(org: Org, title: str, body: str = "", *, embedding=None, pinned=False) -> Item:
        item = Item(
            org_id=org.id,
            title=title,
            body=body,
            embedding=embedding or [1.0] + [0.0] * 7,
            pinned=pinned,
        )
        db.add(item)
        db.commit()
        return item

    return _make


@pytest.fixture
def anon_client():
    """The whole app with `lifespan` running and nobody logged in."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_for(db):
    """A logged-in client per user, each with its own cookie jar. The session is
    opened through the service rather than `/auth/login`, which would cost an argon2
    hash per test."""
    from fastapi.testclient import TestClient

    from app.main import app

    with ExitStack() as stack:

        def _client(user: User):
            c = stack.enter_context(TestClient(app))
            c.cookies.set(auth.COOKIE, auth.open_session(db, user))
            return c

        yield _client


@pytest.fixture
def org(make_org) -> Org:
    return make_org("Delegate")


@pytest.fixture
def other_org(make_org) -> Org:
    return make_org("Demo Client")


@pytest.fixture
def admin(make_user, org) -> User:
    return make_user(org, role=ADMIN)


@pytest.fixture
def client(client_for, admin):
    """The org admin of `org`, logged in."""
    return client_for(admin)
