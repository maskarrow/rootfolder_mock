"""JSON logs on stdout and the one log line per request.

Every line is one JSON object: `ts`, `level`, `logger`, `msg`, `version`, `env`,
plus `request_id` when written inside a request. The server's log collector reads
stdout, so there is no file and no rotation here.

The request ID lives in a `ContextVar`. anyio copies the context into the thread
pool, so sync routes and `BackgroundTasks` jobs log with the ID of the request that
started them.

Nothing here logs bodies, query strings, cookies or headers: passwords, tokens,
keys and file contents must never reach the logs.
"""

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Set on request lines through `extra`, next to the common fields.
_REQUEST_FIELDS = ("method", "path", "status", "duration_ms", "client_ip")

# A proxy's `X-Request-ID` is reused so both logs share the ID; anything else is
# replaced, since the value is echoed into a header and every log line.
_VALID_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")

_logger = logging.getLogger("app.request")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        current = request_id.get()
        if current is not None:
            line["request_id"] = current
        line["version"] = settings.app_version
        line["env"] = settings.app_env
        for field in _REQUEST_FIELDS:
            if hasattr(record, field):
                line[field] = getattr(record, field)
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False, default=str)


def configure() -> None:
    """Called once, when `app.main` is imported.

    uvicorn sets up its own text handlers before importing the app; its loggers are
    handed to the root here so its lines come out as JSON too. Its access log is
    silenced: `RequestLog` writes the request lines, with the request ID.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False


class RequestLog:
    """Assigns the request ID, returns it as `X-Request-ID`, and writes one line when
    the response is finished (for a stream, when the stream ends).

    Raw ASGI rather than `BaseHTTPMiddleware`, so SSE passes through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        current = incoming if _VALID_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id.set(current)
        # Stays 500 if the app raises before starting a response.
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", current.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            client = scope.get("client")
            _logger.info(
                "%s %s %s",
                scope["method"],
                scope["path"],
                status,
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "client_ip": client[0] if client else None,
                },
            )
            request_id.reset(token)
