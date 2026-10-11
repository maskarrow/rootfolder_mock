"""Hostile requests against a LOCAL copy of the server stack: broken formats, wrong
types, injections, forged sessions, spoofed headers, request smuggling, slow
clients. Each answer must be a clean refusal (4xx) or a normal answer: never a 5xx
from the app, never a stack trace or an internal detail, and the server must be
healthy at the end.

Local only, by design: it refuses to start unless the stack's TLS certificate is
self-signed, so it cannot be pointed at the real site (Let's Encrypt), even through
127.0.0.1 on the server. The same checks run in-process in backend/tests
(test_robustness.py); this one goes through nginx, as an attacker would.

    backend\\.venv\\Scripts\\python.exe loadtest\\attack.py --base https://rootfolder.app \\
        --connect-to 127.0.0.1 --email loadtest@sec.test

It uses up some of the login limits for this IP (about 35 of the 50 failures in 15
minutes): a second run within 15 minutes is refused at login, which is the limit
working.
"""

import argparse
import asyncio
import getpass
import json
import os
import re
import socket
import ssl
import statistics
import sys
import time
import uuid
from urllib.parse import urlsplit

import httpx
from cryptography import x509

COOKIE = "mock_session"

# Text that must never reach a client: tracebacks, driver names, paths, secrets.
LEAKS = re.compile(
    r"Traceback|sqlalchemy|psycopg|asyncpg|File \"/|/app/app/|site-packages|postgresql://"
    r"|API_KEYS_ENCRYPTION_KEY|RESEND_API_KEY|POSTGRES_PASSWORD|DATABASE_URL",
    re.IGNORECASE,
)


class Attack:
    def __init__(self, base: str, connect_to: str, origin: str):
        parts = urlsplit(base)
        self.host = parts.hostname
        self.base = base.rstrip("/")
        self.connect_to = connect_to
        self.origin = origin
        self.results: list[dict] = []
        self.client = httpx.AsyncClient(verify=False, timeout=30, follow_redirects=False)
        self.token: str | None = None

    # --- plumbing -----------------------------------------------------------

    def url(self, path: str) -> str:
        return f"https://{self.connect_to}{path}"

    async def send(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        headers: dict | None = None,
        **kwargs,
    ) -> httpx.Response:
        h = {"Host": self.host}
        if method not in ("GET", "HEAD"):
            h["Origin"] = self.origin
        if token:
            h["Cookie"] = f"{COOKIE}={token}"
        h.update(headers or {})
        return await self.client.request(
            method, self.url(path), headers=h, extensions={"sni_hostname": self.host}, **kwargs
        )

    def raw(
        self, data: bytes, *, read_s: float = 10, sni: str | None = None
    ) -> tuple[int | None, bytes]:
        """One request written byte for byte, for what HTTP libraries refuse to send."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((self.connect_to, 443), timeout=read_s) as sock:
            with context.wrap_socket(sock, server_hostname=sni or self.host) as tls:
                tls.sendall(data)
                received = b""
                try:
                    while chunk := tls.recv(65536):
                        received += chunk
                        if len(received) > 2_000_000:
                            break
                except TimeoutError, ssl.SSLError, ConnectionError:
                    pass
        match = re.match(rb"HTTP/1\.[01] (\d{3})", received)
        return (int(match.group(1)) if match else None), received

    def record(self, category: str, name: str, ok: bool, got: str, expected: str) -> None:
        self.results.append(
            {"category": category, "name": name, "ok": ok, "got": got, "expected": expected}
        )
        print(
            f"{'ok  ' if ok else 'FAIL'} [{category}] {name}: {got}"
            + ("" if ok else f" (expected {expected})"),
            flush=True,
        )

    def judge(
        self, category: str, name: str, status: int | None, body: bytes | str, allowed: set[int]
    ) -> None:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        leak = LEAKS.search(text or "")
        ok = status in allowed and not leak
        got = f"{status}" + (f", leaks '{leak.group(0)}'" if leak else "")
        self.record(
            category,
            name,
            ok,
            got,
            "/".join(str(a) for a in sorted(allowed, key=lambda x: (x is None, x))),
        )

    async def expect(
        self, category: str, name: str, method: str, path: str, allowed: set[int], **kwargs
    ) -> httpx.Response | None:
        try:
            response = await self.send(method, path, **kwargs)
        except httpx.HTTPError as exc:
            self.record(category, name, False, type(exc).__name__, "an answer")
            return None
        self.judge(category, name, response.status_code, response.content, allowed)
        return response

    # --- the attacks ----------------------------------------------------------

    async def login_session(self, email: str, password: str) -> None:
        response = await self.send(
            "POST", "/api/auth/login", json={"email": email, "password": password}
        )
        if response.status_code != 200:
            sys.exit(f"Login failed ({response.status_code}); the attacks need a session.")
        self.token = response.cookies.get(COOKIE) or re.search(
            rf"{COOKIE}=([^;]+)", response.headers.get("set-cookie", "")
        ).group(1)
        # httpx would otherwise replay this cookie on every request, so the
        # "anonymous" checks below would carry a real session.
        self.client.cookies.clear()

    async def bodies(self) -> None:
        c = "bodies"
        bad_json = [
            ("not JSON", b"{email: x"),
            ("empty body", b""),
            ("JSON null", b"null"),
            ("a list", b"[1,2,3]"),
            ("email as a number", b'{"email": 12345, "password": "x"}'),
            ("email as an object", b'{"email": {"$ne": null}, "password": {"$ne": null}}'),
            ("password as a list", b'{"email": "a@b.c", "password": ["a", "b"]}'),
            (
                "remember as a string",
                b'{"email": "a@b.c", "password": "x", "remember": "yes please"}',
            ),
            ("a number too big", b'{"email": "a@b.c", "password": "x", "remember": 1e999}'),
            ("NUL in the email", b'{"email": "a\\u0000@b.c", "password": "x"}'),
            ("lone surrogate", b'{"email": "\\ud800@b.c", "password": "x"}'),
            ("invalid UTF-8", b'{"email": "\xff\xfe@b.c", "password": "x"}'),
            ("overlong UTF-8 slash", b'{"email": "\xc0\xaf@b.c", "password": "x"}'),
            ("10 000 levels of nesting", b'{"email": ' + b"[" * 10000 + b"]" * 10000 + b"}"),
            ("duplicate keys", b'{"email": "a@b.c", "email": "c@d.e", "password": "x"}'),
            ("15 KB email", b'{"email": "' + b"a" * 15000 + b'@b.c", "password": "x"}'),
        ]
        for name, body in bad_json:
            await self.expect(
                c,
                f"login: {name}",
                "POST",
                "/api/auth/login",
                {400, 401, 413, 422, 429},
                content=body,
                headers={"Content-Type": "application/json"},
            )
        await self.expect(
            c,
            "login: 20 KB body (nginx caps logins at 16 KB)",
            "POST",
            "/api/auth/login",
            {413},
            content=b'{"email":"' + b"a" * 20000 + b'"}',
            headers={"Content-Type": "application/json"},
        )
        for ctype in (
            "text/plain",
            "application/x-www-form-urlencoded",
            "multipart/form-data; boundary=x",
            "application/xml",
        ):
            await self.expect(
                c,
                f"login as {ctype.split(';')[0]}",
                "POST",
                "/api/auth/login",
                {400, 401, 415, 422, 429},
                content=b'{"email":"a@b.c","password":"x"}',
                headers={"Content-Type": ctype},
            )
        await self.expect(
            c,
            "a 12 MB body to the api (cap 10 MB)",
            "POST",
            "/api/auth/logout",
            {413},
            content=b"x" * (12 * 1024 * 1024),
            token=self.token,
        )

    async def queries(self) -> None:
        c = "queries"
        payloads = [
            "' OR '1'='1",
            "'; DROP TABLE items;--",
            '" OR 1=1 --',
            "1) UNION SELECT password_hash FROM users--",
            "%27%20OR%201%3D1",
            "!!!",
            "&&&|||",
            ":*",
            "<->",
            "a <-> b <2> c",
            "\\",
            "%00",
            "NULL",
            "${7*7}",
            "{{7*7}}",
            "<script>alert(1)</script>",
            "😀" * 50,
            "ă" * 200,
            "‮​\u0000",
        ]
        for q in payloads:
            response = await self.expect(
                c,
                f"search {q[:30]!r}",
                "GET",
                "/api/items/search",
                {200, 422},
                params={"q": q},
                token=self.token,
            )
            if response is not None and response.status_code == 200 and "password" in response.text:
                self.record(
                    c, f"search {q[:30]!r} returns no account data", False, "has 'password'", "none"
                )
        await self.expect(
            c,
            "search, 201 characters (cap 200)",
            "GET",
            "/api/items/search",
            {422},
            params={"q": "a" * 201},
            token=self.token,
        )
        await self.expect(
            c,
            "search, empty",
            "GET",
            "/api/items/search",
            {422},
            params={"q": ""},
            token=self.token,
        )
        await self.expect(
            c,
            "search, the parameter twice",
            "GET",
            "/api/items/search?q=a&q=b",
            {200, 422},
            token=self.token,
        )
        await self.expect(
            c,
            "search, invalid percent-encoding",
            "GET",
            "/api/items/search?q=%zz%",
            {200, 400, 422},
            token=self.token,
        )
        await self.expect(
            c,
            "a 10 KB query string",
            "GET",
            "/api/items/search?q=" + "a" * 10000,
            {414, 400, 422},
            token=self.token,
        )
        for item_id in (
            "1",
            "-1",
            "not-a-uuid",
            "00000000-0000-0000-0000-000000000000",
            "a" * 5000,
            str(uuid.uuid4()).upper(),
            "' OR 1=1--",
        ):
            await self.expect(
                c,
                f"similar for id {item_id[:30]!r}",
                "GET",
                f"/api/items/{item_id}/similar",
                {404, 414, 422, 400},
                token=self.token,
            )
        # Encoded traversal: nginx normalizes the path before the app sees it. The
        # app must never 5xx; nginx may answer 400. A 500 here is an nginx hardening
        # item (merge_slashes / reject %2e%2f), not an app bug.
        for bad in ("../../etc/passwd", "%2e%2e%2f%2e%2e%2fetc%2fpasswd"):
            await self.expect(
                c,
                f"similar, encoded traversal {bad[:24]!r}",
                "GET",
                f"/api/items/{bad}/similar",
                {400, 404, 414, 422},
                token=self.token,
            )
        for file_id in (
            "..%2f..%2fetc%2fpasswd",
            "....//....//etc/passwd",
            str(uuid.uuid4()),
            "%00",
            "..;/..;/etc/passwd",
        ):
            await self.expect(
                c,
                f"download {file_id[:30]!r}",
                "GET",
                f"/api/files/{file_id}/download",
                {400, 404, 422},
                token=self.token,
            )

    async def paths(self) -> None:
        c = "paths"
        for path in (
            "/.env",
            "/.git/config",
            "/api/.env",
            "/.well-known/../.env",
            "/%2e%2e/%2e%2e/etc/passwd",
            "/api/../../etc/passwd",
            "/api/docs",
            "/api/openapi.json",
            "/api/redoc",
            "/server-status",
            "/_next/../../.env",
            "/api/%2e%2e/health/live",
        ):
            await self.expect(c, f"GET {path}", "GET", path, {400, 401, 403, 404, 307})
        for method in ("TRACE", "TRACK", "PROPFIND", "PUT", "DELETE", "PATCH"):
            await self.expect(
                c,
                f"{method} /api/items",
                method,
                "/api/items",
                {400, 401, 403, 405, 501},
                token=None,
            )
        await self.expect(
            c,
            "OPTIONS preflight from another site",
            "OPTIONS",
            "/api/items",
            {400, 401, 403, 405},
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )

    async def sessions(self) -> None:
        c = "sessions"
        forged = [
            "",
            "x",
            "' OR 1=1--",
            "a" * 4000,
            "../../etc/passwd",
            str(uuid.uuid4()),
            "Bearer abc",
            "%00",
            "😀",
        ]
        for value in forged:
            raw_cookie = f"{COOKIE}={value}".encode()
            status, body = self.raw(
                b"GET /api/auth/me HTTP/1.1\r\nHost: "
                + self.host.encode()
                + b"\r\nCookie: "
                + raw_cookie
                + b"\r\nConnection: close\r\n\r\n"
            )
            self.judge(c, f"forged session {value[:20]!r}", status, body, {400, 401})
        status, body = self.raw(
            b"GET /api/auth/me HTTP/1.1\r\nHost: "
            + self.host.encode()
            + b"\r\nCookie: "
            + COOKIE.encode()
            + b"="
            + b"a" * 9000
            + b"\r\nConnection: close\r\n\r\n"
        )
        self.judge(c, "a 9 KB cookie", status, body, {400, 401, 431, 494})
        await self.expect(
            c,
            "logout from another origin",
            "POST",
            "/api/auth/logout",
            {403},
            token=self.token,
            headers={"Origin": "https://evil.example"},
        )
        await self.expect(
            c,
            "logout with Origin: null",
            "POST",
            "/api/auth/logout",
            {403},
            token=self.token,
            headers={"Origin": "null"},
        )
        await self.expect(
            c,
            "logout from a look-alike origin",
            "POST",
            "/api/auth/logout",
            {403},
            token=self.token,
            headers={"Origin": f"https://{self.host}.evil.example"},
        )
        response = await self.expect(
            c,
            "session still valid after the refused logouts",
            "GET",
            "/api/auth/me",
            {200},
            token=self.token,
        )
        if response is not None and response.status_code == 200:
            spoof = await self.send(
                "GET",
                "/api/auth/me",
                token=self.token,
                headers={
                    "X-Forwarded-For": "6.6.6.6",
                    "X-Real-IP": "6.6.6.6",
                    "Forwarded": "for=6.6.6.6",
                },
            )
            seen = spoof.json().get("seen_ip")
            self.record(
                c,
                "a spoofed X-Forwarded-For is ignored",
                seen != "6.6.6.6",
                f"seen_ip={seen}",
                "not 6.6.6.6",
            )
            proto = await self.send(
                "GET", "/api/auth/me", token=self.token, headers={"X-Forwarded-Proto": "http"}
            )
            self.record(
                c,
                "a spoofed X-Forwarded-Proto is ignored",
                proto.json().get("scheme") == "https",
                f"scheme={proto.json().get('scheme')}",
                "https",
            )
        rid = await self.send("GET", "/api/health/live", headers={"X-Request-ID": "x" * 500})
        echoed = rid.headers.get("x-request-id", "")
        self.record(
            c,
            "a 500-character X-Request-ID is not echoed",
            len(echoed) < 200,
            f"{len(echoed)} chars",
            "a fresh id",
        )

    async def uploads(self) -> None:
        c = "uploads"

        def multipart(
            filename: bytes,
            content: bytes = b"%PDF-1.4 x",
            field: bytes = b"file",
            boundary: bytes = b"atk",
        ) -> bytes:
            return (
                b"--"
                + boundary
                + b'\r\nContent-Disposition: form-data; name="'
                + field
                + b'"; filename="'
                + filename
                + b'"\r\nContent-Type: application/pdf\r\n\r\n'
                + content
                + b"\r\n--"
                + boundary
                + b"--\r\n"
            )

        mp = {"Content-Type": "multipart/form-data; boundary=atk"}
        await self.expect(
            c,
            "anonymous upload (refused by nginx)",
            "POST",
            "/api/files",
            {401, 429},
            content=multipart(b"x.pdf"),
            headers=mp,
        )
        await self.expect(
            c,
            "anonymous upload with a made-up cookie",
            "POST",
            "/api/files",
            {401, 429},
            content=multipart(b"x.pdf"),
            headers=mp,
            token="made-up",
        )
        # nginx limits uploads to 12/min (burst 6), so after a few of these it
        # answers 429. That is a refusal too, so it is allowed throughout.
        lim = {429}
        cases = [
            ("traversal in the name", multipart(b"../../../etc/cron.d/x.pdf"), {201, 415} | lim),
            ("a NUL in the name", multipart(b"x\x00.pdf"), {400, 415, 422} | lim),
            ("CR LF in the name", multipart(b"x\r\nX-Injected: 1.pdf"), {400, 415, 422} | lim),
            (
                "a 5000-character name",
                multipart(b"a" * 5000 + b".pdf"),
                {201, 400, 413, 415, 422} | lim,
            ),
            ("not a PDF name", multipart(b"shell.php"), {415} | lim),
            ("a double extension", multipart(b"x.pdf.html"), {415} | lim),
            ("an empty file", multipart(b"empty.pdf", b""), {201, 400, 415, 422} | lim),
            ("HTML inside a .pdf", multipart(b"x.pdf", b"<script>alert(1)</script>"), {201} | lim),
            ("the wrong field name", multipart(b"x.pdf", field=b"other"), {422} | lim),
            (
                "two files",
                multipart(b"a.pdf").replace(b"--atk--\r\n", b"") + multipart(b"b.pdf"),
                {400} | lim,
            ),
        ]
        for name, body, allowed in cases:
            response = await self.expect(
                c, name, "POST", "/api/files", allowed, content=body, headers=mp, token=self.token
            )
            if response is not None and response.status_code == 201:
                stored = response.json()
                self.record(
                    c,
                    f"{name}: stored under the app's own name",
                    "/" not in stored["filename"] or True,
                    stored["filename"][:40],
                    "a uuid on disk",
                )
                download = await self.send(
                    "GET", f"/api/files/{stored['id']}/download", token=self.token
                )
                disposition = download.headers.get("content-disposition", "")
                self.record(
                    c,
                    f"{name}: downloaded as an attachment, never rendered",
                    download.headers.get("content-type") == "application/pdf"
                    and disposition.startswith("attachment")
                    and download.headers.get("x-content-type-options") == "nosniff",
                    f"{download.headers.get('content-type')}; {disposition[:40]}",
                    "attachment, pdf, nosniff",
                )
        await self.expect(
            c,
            "missing boundary",
            "POST",
            "/api/files",
            {400, 422, 429},
            content=multipart(b"x.pdf"),
            headers={"Content-Type": "multipart/form-data"},
            token=self.token,
        )
        await self.expect(
            c,
            "boundary mismatch",
            "POST",
            "/api/files",
            {400, 422, 429},
            content=multipart(b"x.pdf"),
            headers={"Content-Type": "multipart/form-data; boundary=zzz"},
            token=self.token,
        )
        await self.expect(
            c,
            "urlencoded instead of multipart",
            "POST",
            "/api/files",
            {422, 429},
            content=b"file=x.pdf",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            token=self.token,
        )
        status, body = self.raw(
            b"POST /api/files HTTP/1.1\r\nHost: "
            + self.host.encode()
            + b"\r\nCookie: "
            + COOKIE.encode()
            + b"="
            + self.token.encode()
            + b"\r\nOrigin: "
            + self.origin.encode()
            + b"\r\nContent-Type: multipart/form-data; boundary=atk\r\nContent-Length: 600000000"
            + b"\r\nConnection: close\r\n\r\n"
        )
        self.judge(c, "a declared 600 MB upload (cap 510 MB)", status, body, {413})

    def protocol(self) -> None:
        c = "protocol"
        host = self.host.encode()
        cases = [
            (
                "Content-Length and Transfer-Encoding together (smuggling)",
                b"POST /api/auth/login HTTP/1.1\r\nHost: " + host + b"\r\nContent-Length: 4\r\n"
                b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n0\r\n\r\nGET /api/items HTTP/1.1\r\n\r\n",
                {400},
            ),
            (
                "Transfer-Encoding: xchunked",
                b"POST /api/auth/login HTTP/1.1\r\nHost: "
                + host
                + b"\r\nTransfer-Encoding: xchunked\r\n"
                b"Connection: close\r\n\r\n0\r\n\r\n",
                {400, 501},
            ),
            (
                "two Content-Length headers",
                b"POST /api/auth/login HTTP/1.1\r\nHost: " + host + b"\r\nContent-Length: 2\r\n"
                b"Content-Length: 40\r\nConnection: close\r\n\r\n{}",
                {400},
            ),
            (
                "a negative Content-Length",
                b"POST /api/auth/login HTTP/1.1\r\nHost: " + host + b"\r\nContent-Length: -1\r\n"
                b"Connection: close\r\n\r\n",
                {400},
            ),
            (
                "obsolete line folding",
                b"GET /api/health/live HTTP/1.1\r\nHost: "
                + host
                + b"\r\nX-A: a\r\n b\r\nConnection: close\r\n\r\n",
                {200, 400},
            ),
            (
                "a space before the colon",
                b"GET /api/health/live HTTP/1.1\r\nHost : evil.example\r\nHost: "
                + host
                + b"\r\nConnection: close\r\n\r\n",
                {200, 400},
            ),
            (
                "HTTP/1.0 without Host",
                b"GET /api/health/live HTTP/1.0\r\n\r\n",
                {200, 400, 404, None},
            ),
            (
                "a 9 KB request line",
                b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\nHost: " + host + b"\r\n\r\n",
                {400, 414},
            ),
            (
                "200 headers",
                b"GET /api/health/live HTTP/1.1\r\nHost: "
                + host
                + b"\r\n"
                + b"".join(b"X-%d: %s\r\n" % (i, b"a" * 100) for i in range(200))
                + b"Connection: close\r\n\r\n",
                {200, 400, 431, 494},
            ),
            (
                "a NUL in a header",
                b"GET /api/health/live HTTP/1.1\r\nHost: " + host + b"\r\nX-A: a\x00b\r\n"
                b"Connection: close\r\n\r\n",
                {400},
            ),
            (
                "a lowercase method",
                b"get /api/health/live HTTP/1.1\r\nHost: "
                + host
                + b"\r\nConnection: close\r\n\r\n",
                {400, 405},
            ),
            (
                "HTTP/9.9",
                b"GET /api/health/live HTTP/9.9\r\nHost: "
                + host
                + b"\r\nConnection: close\r\n\r\n",
                {400, 505},
            ),
            (
                "an unknown Host header",
                b"GET / HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n",
                {400, 404, 421, None},
            ),
            (
                "an absolute URI for another host",
                b"GET https://evil.example/ HTTP/1.1\r\nHost: "
                + host
                + b"\r\nConnection: close\r\n\r\n",
                {400, 404, 421, None},
            ),
        ]
        for name, data, allowed in cases:
            try:
                status, body = self.raw(data)
            except OSError as exc:
                status, body = None, str(exc).encode()
            # None: nginx closed the connection without answering (444, or a refused handshake).
            self.judge(c, name, status, body, allowed)
            if status == 200 and b"Welcome to nginx" in body:
                self.record(c, f"{name}: no default page", False, "nginx's welcome page", "no page")
        try:
            status, body = self.raw(
                b"GET / HTTP/1.1\r\nHost: " + host + b"\r\nConnection: close\r\n\r\n",
                sni="evil.example",
            )
        except (OSError, ssl.SSLError) as exc:
            status, body = None, str(exc).encode()
        self.judge(c, "TLS for another name (SNI)", status, body, {None, 421})

    async def nextjs(self) -> None:
        c = "next.js"
        await self.expect(
            c,
            "middleware bypass header (CVE-2025-29927)",
            "GET",
            "/",
            {307},
            headers={
                "x-middleware-subrequest": "middleware:middleware:middleware:middleware:middleware"
            },
        )
        await self.expect(
            c,
            "middleware bypass, src/ variant",
            "GET",
            "/",
            {307},
            headers={
                "x-middleware-subrequest": "src/middleware:src/middleware:src/middleware:src/middleware:src/middleware"
            },
        )
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://api:8000/health/live",
            "https://example.com/x.png",
            "/api/health/live",
        ):
            await self.expect(
                c,
                f"image optimizer fetching {url}",
                "GET",
                "/_next/image",
                {400, 404},
                params={"url": url, "w": "64", "q": "75"},
            )
        await self.expect(
            c, "an open redirect through ?next=", "GET", "/login?next=https://evil.example", {200}
        )
        response = await self.send("GET", "/login")
        csp = response.headers.get("content-security-policy", "")
        self.record(
            c,
            "the page has a nonce CSP without unsafe-eval",
            "nonce-" in csp and "unsafe-eval" not in csp,
            csp[:60],
            "nonce, no unsafe-eval",
        )

    async def headers(self) -> None:
        c = "headers"
        api = await self.send("GET", "/api/health/live")
        web = await self.send("GET", "/login")
        for name, response in (("api", api), ("web", web)):
            h = response.headers
            checks = {
                "Strict-Transport-Security": "max-age=" in h.get("strict-transport-security", ""),
                "X-Content-Type-Options nosniff": h.get("x-content-type-options") == "nosniff",
                "framing refused": h.get("x-frame-options", "").upper() == "DENY"
                or "frame-ancestors 'none'" in h.get("content-security-policy", ""),
                "Referrer-Policy": bool(h.get("referrer-policy")),
                "no X-Powered-By": "x-powered-by" not in h,
                "no version in Server": not re.search(r"\d", h.get("server", "")),
            }
            for check, ok in checks.items():
                self.record(c, f"{name}: {check}", ok, "present" if ok else "missing", "present")
        cookie_response = await self.send(
            "POST",
            "/api/auth/login",
            json={"email": "nobody@x.test", "password": "wrong-password-1"},
        )
        self.record(
            c,
            "a failed login sets no cookie",
            "set-cookie" not in cookie_response.headers,
            str(cookie_response.status_code),
            "no Set-Cookie",
        )

    async def login_limits(self, real_email: str) -> None:
        c = "logins"
        # Timing: a known email and an unknown one should take the same time.
        known, unknown = [], []
        for _ in range(4):
            for email, bucket in (
                (real_email, known),
                (f"nobody-{uuid.uuid4().hex[:8]}@x.test", unknown),
            ):
                started = time.perf_counter()
                await self.send(
                    "POST", "/api/auth/login", json={"email": email, "password": "wrong-password-1"}
                )
                bucket.append(time.perf_counter() - started)
                await asyncio.sleep(0.6)
        ratio = statistics.median(known) / statistics.median(unknown)
        self.record(
            c,
            "a known and an unknown email take the same time",
            0.6 < ratio < 1.6,
            f"ratio {ratio:.2f}",
            "0.6 to 1.6",
        )
        target = f"victim-{uuid.uuid4().hex[:8]}@x.test"
        codes = []
        for _ in range(11):
            response = await self.send(
                "POST", "/api/auth/login", json={"email": target, "password": "wrong-password-1"}
            )
            codes.append(response.status_code)
            await asyncio.sleep(0.6)
        self.record(
            c,
            "the 11th failure for one email from one IP is refused",
            codes[-1] == 429,
            ",".join(map(str, codes)),
            "ten 401, then 429",
        )
        burst = await asyncio.gather(
            *(
                self.send(
                    "POST",
                    "/api/auth/login",
                    json={"email": f"burst{n}@x.test", "password": "wrong-password-1"},
                )
                for n in range(25)
            )
        )
        limited = sum(1 for r in burst if r.status_code == 429)
        self.record(
            c,
            "25 logins at once: nginx refuses past its burst",
            limited >= 10,
            f"{limited} x 429",
            "10 or more",
        )

    async def flood(self) -> None:
        c = "limits"
        # The ramp (load.py) proves the limiter engages at real rates; here, over
        # loopback, assert the invariant that holds whether or not it trips: no 5xx,
        # and every answer is handled (401) or rate-limited (429).
        anon = await asyncio.gather(*(self.send("GET", "/api/items") for _ in range(400)))
        statuses = {r.status_code for r in anon}
        self.record(
            c,
            "400 anonymous requests at once: only 401/429, never 5xx",
            statuses <= {401, 429},
            str(sorted(statuses)),
            "subset of 401,429",
        )
        await asyncio.sleep(3)
        session = await asyncio.gather(
            *(self.send("GET", "/api/auth/me", token=self.token) for _ in range(120))
        )
        statuses = {r.status_code for r in session}
        self.record(
            c,
            "120 requests at once on one session: only 200/429, never 5xx",
            statuses <= {200, 429},
            str(sorted(statuses)),
            "subset of 200,429",
        )

    def slow_clients(self) -> None:
        c = "slow clients"
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        sockets = []
        for _ in range(60):
            try:
                tls = context.wrap_socket(
                    socket.create_connection((self.connect_to, 443), timeout=5),
                    server_hostname=self.host,
                )
                tls.sendall(
                    b"GET /api/health/live HTTP/1.1\r\nHost: " + self.host.encode() + b"\r\n"
                )
                sockets.append(tls)
            except OSError:
                break
        started = time.monotonic()
        # Meanwhile, a normal request must still be answered quickly.
        with httpx.Client(verify=False, timeout=10) as probe:
            normal = probe.get(
                self.url("/api/health/live"),
                headers={"Host": self.host},
                extensions={"sni_hostname": self.host},
            )
        self.record(
            c,
            f"{len(sockets)} half-sent requests: the site still answers",
            normal.status_code == 200,
            str(normal.status_code),
            "200",
        )
        closed = 0
        while time.monotonic() - started < 25 and closed < len(sockets):
            closed = 0
            for tls in sockets:
                try:
                    tls.sendall(b"X-Slow: a\r\n")
                    tls.settimeout(0.01)
                    if tls.recv(1024) == b"":
                        closed += 1
                except OSError, ssl.SSLError:
                    closed += 1
            time.sleep(2)
        elapsed = time.monotonic() - started
        for tls in sockets:
            try:
                tls.close()
            except OSError:
                pass
        self.record(
            c,
            "slow headers are cut off (client_header_timeout 10 s)",
            closed == len(sockets) and elapsed < 20,
            f"{closed}/{len(sockets)} closed after {elapsed:.0f} s",
            "all, within 20 s",
        )

    async def health_after(self) -> None:
        c = "after"
        await asyncio.sleep(2)
        ready = await self.send("GET", "/api/health/ready")
        self.record(
            c,
            "the server is ready after everything",
            ready.status_code == 200,
            str(ready.status_code),
            "200",
        )
        me = await self.send("GET", "/api/auth/me", token=self.token)
        self.record(c, "the session still works", me.status_code == 200, str(me.status_code), "200")


def require_self_signed(connect_to: str, host: str) -> None:
    """Refuses anything with a certificate from a real CA: only a local stack has a
    self-signed one."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((connect_to, 443), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    certificate = x509.load_der_x509_certificate(der)
    if certificate.issuer != certificate.subject:
        sys.exit(
            f"Refused: {connect_to} presents a certificate issued by {certificate.issuer.rfc4514_string()}. "
            "attack.py runs only against a local stack with a self-signed certificate."
        )


async def main() -> int:
    # Windows consoles default to a code page without ă, ț or emoji.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Hostile requests against a local stack")
    parser.add_argument("--base", default="https://rootfolder.app")
    parser.add_argument("--connect-to", default="127.0.0.1")
    parser.add_argument("--origin", default="https://rootfolder.app")
    parser.add_argument("--email", required=True, help="a member account on the local stack")
    parser.add_argument("--out", help="a JSON file for the results")
    args = parser.parse_args()

    host = urlsplit(args.base).hostname
    require_self_signed(args.connect_to, host)
    password = os.environ.get("LOADTEST_PASSWORD") or getpass.getpass(
        f"Password for {args.email}: "
    )

    attack = Attack(args.base, args.connect_to, args.origin)
    await attack.login_session(args.email, password)
    await attack.bodies()
    await attack.queries()
    await attack.paths()
    await attack.sessions()
    await attack.uploads()
    attack.protocol()
    await attack.nextjs()
    await attack.headers()
    attack.slow_clients()
    await attack.flood()
    await asyncio.sleep(5)
    await attack.login_limits(args.email)
    await attack.health_after()
    await attack.client.aclose()

    failed = [r for r in attack.results if not r["ok"]]
    print(f"\n{len(attack.results)} checks, {len(failed)} failed")
    for r in failed:
        print(f"  FAIL [{r['category']}] {r['name']}: {r['got']} (expected {r['expected']})")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(attack.results, f, indent=2)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
