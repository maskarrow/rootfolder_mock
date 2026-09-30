"""Rejects state-changing requests coming from another site.

The session cookie is `SameSite=Lax`, which already blocks cross-site POSTs. This
is the second net for what Lax misses: a subdomain is "same-site" for the cookie
but a different origin for us.

A non-GET/HEAD/OPTIONS request with an `Origin` header must come from one of
`CORS_ORIGINS`. No header passes: browsers always send it on POST, so its absence
means a non-browser client, which has no one else's cookie to abuse.

In a deploy this is also the first thing to break: a public origin missing from
`CORS_ORIGINS` turns every POST, login included, into a 403.

Raw ASGI, not `BaseHTTPMiddleware`, so the SSE stream passes through untouched.
"""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class CheckOrigin:
    def __init__(self, app: ASGIApp, allowed: list[str]) -> None:
        self.app = app
        self.allowed = {origin.encode() for origin in allowed}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in _SAFE_METHODS:
            origin = dict(scope["headers"]).get(b"origin")
            if origin is not None and origin not in self.allowed:
                response = JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
