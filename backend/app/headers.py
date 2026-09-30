"""Security headers on API responses.

In production `/api` goes from the proxy straight to the backend, bypassing Next,
so the headers in `next.config.ts` do not apply here.

SSE responses also get `Cache-Control: no-cache, no-transform` and
`X-Accel-Buffering: no`. Without `no-transform`, compression on the way (Next's
`/api` rewrite, since browsers always ask for gzip) buffers the whole stream and
delivers it at the end; `X-Accel-Buffering` tells nginx not to buffer it either.

A raw ASGI middleware rather than `BaseHTTPMiddleware`, which wraps the response
and can interfere with streaming.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_ALWAYS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-origin"),
)

_STREAM = (
    (b"cache-control", b"no-cache, no-transform"),
    (b"x-accel-buffering", b"no"),
)


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                content_type = next(
                    (value for name, value in headers if name.lower() == b"content-type"), b""
                )
                if content_type.startswith(b"text/event-stream"):
                    # Replaced, not appended: FastAPI already sets both, and two
                    # values would be merged into one.
                    replaced = {name for name, _ in _STREAM}
                    headers = [(n, v) for n, v in headers if n.lower() not in replaced]
                    headers.extend(_STREAM)
                existing = {name.lower() for name, _ in headers}
                headers.extend(_ALWAYS)
                if b"cache-control" not in existing:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
