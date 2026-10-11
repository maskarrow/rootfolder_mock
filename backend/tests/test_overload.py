"""When the server is full or the database is away: a 503 with Retry-After, never
a 500, and a liveness probe that does not wait behind database work."""

import asyncio
import inspect

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app import main
from app.admission import Admission
from app.config import settings
from app.db import SessionLocal, engine
from app.services import auth

BUSY = {"detail": "The service is busy. Try again in a moment."}


@pytest.fixture
def sessions_on():
    """Points the app's sessions at another engine for one test."""
    yield lambda other: SessionLocal.configure(bind=other)
    SessionLocal.configure(bind=engine)


def test_a_full_connection_pool_is_a_503_with_retry_after(client, sessions_on):
    tiny = create_engine(settings.database_url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    taken = tiny.connect()
    try:
        sessions_on(tiny)
        response = client.get("/items")
    finally:
        taken.close()
        tiny.dispose()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == BUSY


def test_database_down_is_a_503_not_a_500(client, sessions_on):
    down = create_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none", connect_args={"connect_timeout": 2}
    )
    sessions_on(down)

    response = client.get("/items")

    assert response.status_code == 503
    assert response.json() == BUSY


def test_a_query_past_the_statement_timeout_is_what_the_503_catches(db):
    db.execute(text("SET LOCAL statement_timeout = '20ms'"))

    with pytest.raises(OperationalError, match="statement timeout"):
        db.execute(text("SELECT pg_sleep(1)"))
    db.rollback()


def test_liveness_is_answered_on_the_event_loop():
    # A plain `def` waits for a thread-pool thread, and a burst of slow database
    # requests can hold all of them: the health check would time out and the
    # watchdog restart a busy, healthy API. See main.health_live.
    assert inspect.iscoroutinefunction(main.health_live)


async def _call(app, path: str, method: str = "GET") -> tuple[int, dict]:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    started: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            started["status"] = message["status"]
            started["headers"] = {k.decode(): v.decode() for k, v in message["headers"]}

    await app(scope, receive, send)
    return started["status"], started["headers"]


def _held_app(release: asyncio.Event, body_gate: asyncio.Event | None = None):
    """`/slow` waits for `release` before answering; `/download` answers at once but
    sends its body only after `body_gate`."""

    async def app(scope, receive, send):
        if scope["path"] == "/slow":
            await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if scope["path"] == "/download" and body_gate is not None:
            await body_gate.wait()
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def test_a_full_app_turns_away_what_waited_too_long_but_not_health_checks():
    async def scenario():
        release = asyncio.Event()
        guarded = Admission(_held_app(release), limit=1, wait_s=0.2)
        inside = asyncio.create_task(_call(guarded, "/slow"))
        await asyncio.sleep(0.05)
        turned_away = await _call(guarded, "/items")
        live = await _call(guarded, "/health/live")
        upload = await _call(guarded, "/files", method="POST")
        release.set()
        return await inside, turned_away, live, upload, await _call(guarded, "/items")

    inside, turned_away, live, upload, after = asyncio.run(scenario())

    assert inside[0] == 200
    assert turned_away[0] == 503
    assert turned_away[1]["retry-after"] == "5"
    assert live[0] == 200
    assert upload[0] == 200
    assert after[0] == 200  # the place came back


def test_a_request_leaves_its_place_once_its_response_starts():
    async def scenario():
        body_gate = asyncio.Event()
        guarded = Admission(_held_app(asyncio.Event(), body_gate), limit=1, wait_s=0.2)
        downloading = asyncio.create_task(_call(guarded, "/download"))
        await asyncio.sleep(0.05)
        meanwhile = await _call(guarded, "/items")
        body_gate.set()
        await downloading
        return meanwhile

    assert asyncio.run(scenario())[0] == 200


def test_reading_the_session_leaves_no_transaction_open(db, admin):
    """The cause of the deadlock the load test found: a connection held from the
    session check until the route ran, in another thread-pool turn."""
    token = auth.open_session(db, admin)

    assert auth.read_session(db, token) is not None
    assert not db.in_transaction()
    assert auth.read_session(db, "no-such-token") is None
    assert not db.in_transaction()
