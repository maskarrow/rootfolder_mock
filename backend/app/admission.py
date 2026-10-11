"""Admission control: at most ADMISSION_LIMIT requests inside the app at once; the
rest wait in line, on the event loop, for up to ADMISSION_WAIT_S, then get a 503
with Retry-After.

Why: every request here needs a thread from anyio's pool (40) and, for its
queries, a database connection (DB_POOL_SIZE + DB_MAX_OVERFLOW, also 40). Let a
burst far past capacity in at once and each request only gets slower, until nginx
and the clients give up while the app keeps working for them. With the limit equal
to the thread pool, a request that is in never waits for a thread or a connection,
so the app runs at full speed however long the line; one in line waits cheaply,
and past the wait gets a quick, honest 503 instead of a minute of nothing.

A request leaves as soon as its response starts: sending a body (a download, the
stream) takes no thread, so it does not hold a place. Not in line at all: the
health checks, which must answer while the app is full (Docker's check restarts
an API whose /health/live times out), and uploads, which spend minutes receiving
their body and are limited by nginx instead (4 at a time per IP).
"""

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

BUSY = {"detail": "The service is busy. Try again in a moment."}


class Admission:
    def __init__(self, app: ASGIApp, limit: int, wait_s: float) -> None:
        self.app = app
        self.wait_s = wait_s
        self.places = asyncio.Semaphore(limit)

    @staticmethod
    def _exempt(scope: Scope) -> bool:
        path = scope["path"]
        return path.startswith("/health/") or (path == "/files" and scope["method"] == "POST")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._exempt(scope):
            await self.app(scope, receive, send)
            return

        try:
            await asyncio.wait_for(self.places.acquire(), timeout=self.wait_s)
        except TimeoutError:
            response = JSONResponse(BUSY, status_code=503, headers={"Retry-After": "5"})
            await response(scope, receive, send)
            return

        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self.places.release()

        async def send_and_leave(message: Message) -> None:
            if message["type"] == "http.response.start":
                release()
            await send(message)

        try:
            await self.app(scope, receive, send_and_leave)
        finally:
            release()
