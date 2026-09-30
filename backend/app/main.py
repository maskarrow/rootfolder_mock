"""The app: middleware, startup checks, background loops, routers and health.

Startup assumes a single server process, as Delegate's deploy does: files left in
`processing` by the previous process are marked `interrupted` before the first
request. Migrations never run here; the deploy runs `alembic upgrade head` once,
before starting the new containers.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from anyio import to_thread
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import emails, logs
from app.config import settings
from app.db import SessionLocal, engine
from app.deps import get_auth
from app.headers import SecurityHeaders
from app.origin import CheckOrigin
from app.routers import auth, files, items, stream
from app.services import cleanup, jobs

logs.configure()
logger = logging.getLogger(__name__)


def _cleanup_once() -> None:
    db = SessionLocal()
    try:
        cleanup.run(db)
    finally:
        db.close()


async def _periodic_cleanup() -> None:
    """At startup, then every `CLEANUP_INTERVAL_HOURS`. No exception may escape: a
    silently stopped loop would never clean again. Failures retry on the next pass."""
    while True:
        try:
            await to_thread.run_sync(_cleanup_once)
        except Exception:
            logger.exception("Cleanup failed; retrying on the next pass")
        await asyncio.sleep(settings.cleanup_interval_hours * 3600)


def _check_emails() -> None:
    """A public install does not start without an email key, as in Delegate, where
    invites and password resets depend on it. Public is recognized by `APP_URL`."""
    if settings.resend_api_key or emails.is_local_address():
        return
    raise RuntimeError(
        f"APP_URL={settings.app_url} is a public address but RESEND_API_KEY is missing. "
        "Put the Resend key in the environment."
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await to_thread.run_sync(_check_emails)
    # Before the first request, so nobody sees a file "processing" that never ends.
    await to_thread.run_sync(jobs.fail_interrupted)
    # `CLEANUP_INTERVAL_HOURS=0` turns it off (tests must not see rows deleted by a
    # parallel task mid-test).
    cleaner = (
        asyncio.create_task(_periodic_cleanup()) if settings.cleanup_interval_hours > 0 else None
    )
    logger.info("Started: version=%s env=%s", settings.app_version, settings.app_env)
    try:
        yield
    finally:
        if cleaner is not None:
            cleaner.cancel()


def _docs_config() -> dict:
    """`/docs`, `/redoc` and `/openapi.json` only locally; publicly they would hand out
    the full route map."""
    if emails.is_local_address():
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


app = FastAPI(title="delegate-mock API", lifespan=lifespan, **_docs_config())

_ORIGINS = [origin.strip() for origin in settings.cors_origins.split(",")]

# Added first, so it sits inside CORS: the OPTIONS preflight must be answered by CORS.
app.add_middleware(CheckOrigin, allowed=_ORIGINS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Outside CORS, so preflights and `CheckOrigin` refusals get a log line too.
app.add_middleware(logs.RequestLog)
# Outermost, so every response above also gets the headers.
app.add_middleware(SecurityHeaders)

# The only router without a mandatory session: login is the way to get one.
app.include_router(auth.router)

# Attached per router rather than per route, so a new route is protected without
# anyone remembering to.
_AUTHENTICATED = [Depends(get_auth)]

app.include_router(items.router, dependencies=_AUTHENTICATED)
app.include_router(files.router, dependencies=_AUTHENTICATED)
app.include_router(stream.router, dependencies=_AUTHENTICATED)


def _health() -> dict:
    return {"status": "ok", "version": settings.app_version, "env": settings.app_env}


@app.get("/health/live")
def health_live():
    """The process answers. Never touches the database, so a database outage does
    not make the orchestrator restart a healthy API."""
    return _health()


@app.get("/health/ready")
def health_ready():
    """Ready to serve: the database answers. 503 otherwise, so the deploy script's
    wait and the proxy can tell."""
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:
        # One line, not a traceback: probes repeat every few seconds during an outage.
        # The first line says why (refused, wrong password, unknown host) without the
        # password, which psycopg never includes.
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        logger.warning("Readiness check: the database does not answer: %s", reason)
        return JSONResponse({**_health(), "status": "unavailable", "db": "down"}, status_code=503)
    return {**_health(), "db": "ok"}
