"""Load and stress test for rootfolder: how much the server takes, where it breaks,
and whether it comes back by itself.

Only load: many ordinary requests from one throwaway account. Hostile input (broken
formats, injections, oversized headers) is loadtest/attack.py, which refuses to run
against anything but a local stack.

Modes (loadtest/README.md has the exact commands, on the server and locally):

    smoke    every feature once, each answer checked; also after every deploy
    ramp     concurrency up step by step until the failures or the latency pass a
             limit (the breaking point), then: does the server recover by itself?
    spike    calm, a sudden burst, calm again: how fast latency returns to normal
    soak     steady load for many minutes: does latency, memory or disk drift?
    streams  many streamed answers at once, each must arrive whole
    uploads  large uploads in parallel, each downloaded again and compared
    suite    smoke, ramp, spike, streams, uploads, recovery, smoke: the full run

Two places to run it, and they answer different questions:

- Through nginx (`--api https://rootfolder.app/api`): what a user gets, including
  the limits. From one IP nginx answers 429 above its rates; that is the limiter
  doing its job, reported apart from failures.
- Inside the server's compose network (`--api http://api:8000 --web http://web:3000`):
  no nginx, so the app's own capacity, Postgres and the connection pool.

While it runs, a probe asks /health/live every second, as Docker's health check
does. If the probe would have failed three times in a row, the watchdog
(scripts/heal.sh) would restart a busy API: the report says so.

The password is read from LOADTEST_PASSWORD or asked in the terminal; it never goes
on the command line. The account must not be an admin (--allow-admin overrides).
Uploaded files are named `loadtest-*.pdf`; `delete_files.py --uploaded-by <email>`
removes them afterwards.
"""

import argparse
import asyncio
import getpass
import hashlib
import ipaddress
import json
import math
import multiprocessing
import os
import random
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

COOKIE = "mock_session"

# Where this script may send load: the app's own names, the compose services, this
# machine. Anything else is refused, so a typo cannot load someone else's server.
ALLOWED_HOSTS = {"rootfolder.app", "www.rootfolder.app", "api", "web", "nginx", "localhost"}
MAX_CONCURRENCY = 2000

SEARCH_TERMS = [
    "caietul de sarcini",
    "garanția de participare",
    "criteriul de atribuire",
    "termenul de depunere",
    "ofertele întârziate",
    "clarificări",
    "licitație",
    "experiența similară",
    "subcontractarea lucrărilor",
    "drumului județean",
]

# What a busy user does, by weight. `web` only when --web is given.
MIX = {
    "items": 25,
    "search": 20,
    "similar": 15,
    "me": 15,
    "files": 10,
    "ready": 5,
    "web": 10,
}

FAILURES = {"busy", "error", "timeout", "conn", "unauthorized", "rejected"}

# Latencies go into log-scale bins 5 % wide: percentiles to within 5 %, and the
# bins of many processes and many minutes add up without keeping every sample.
_LOG_STEP = math.log(1.05)


def _bin(ms: float) -> int:
    return 0 if ms < 1 else int(math.log(ms) / _LOG_STEP)


def _bin_ms(b: int) -> float:
    return math.exp((b + 0.5) * _LOG_STEP)


def percentile(hist: dict, fraction: float) -> float:
    total = sum(hist.values())
    if not total:
        return 0.0
    wanted = fraction * total
    seen = 0
    for b in sorted(hist):
        seen += hist[b]
        if seen >= wanted:
            return _bin_ms(int(b))
    return _bin_ms(int(max(hist)))


def classify(status: int) -> str:
    if status < 400:
        return "ok"
    if status == 429:
        return "limited"
    if status == 503:
        return "busy"
    if status >= 500:
        return "error"
    if status == 401:
        return "unauthorized"
    return "rejected"


def classify_exc(exc: Exception) -> str:
    return "timeout" if isinstance(exc, httpx.TimeoutException) else "conn"


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {message}", flush=True)


# --- Target --------------------------------------------------------------------


class Target:
    """The URLs, and how to reach them. With --connect-to, requests go to another
    address (nginx's container, 127.0.0.1) while the Host header and the TLS name
    stay the public one, as curl's --resolve does."""

    def __init__(
        self,
        api: str,
        web: str | None,
        origin: str,
        connect_to: str | None,
        insecure: bool,
        timeout: float,
    ):
        self.api = api.rstrip("/")
        self.web = web.rstrip("/") if web else None
        self.origin = origin
        self.connect_to = connect_to
        self.insecure = insecure
        self.timeout = timeout
        self.public_host = urlsplit(self.api).hostname

    def as_dict(self) -> dict:
        return {
            "api": self.api,
            "web": self.web,
            "origin": self.origin,
            "connect_to": self.connect_to,
            "insecure": self.insecure,
            "timeout": self.timeout,
        }

    def route(self, url: str) -> tuple[str, dict, dict]:
        """The URL to open, extra headers, and httpx extensions."""
        parts = urlsplit(url)
        if not self.connect_to or parts.hostname != self.public_host:
            return url, {}, {}
        netloc = self.connect_to if parts.port is None else f"{self.connect_to}:{parts.port}"
        host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
        extensions = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
        return urlunsplit(parts._replace(netloc=netloc)), {"Host": host}, extensions

    def client(self, connections: int) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            verify=not self.insecure,
            limits=httpx.Limits(max_connections=connections, max_keepalive_connections=connections),
            follow_redirects=False,
        )

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        token: str | None = None,
        **kwargs,
    ) -> httpx.Response:
        real_url, headers, extensions = self.route(url)
        headers.update(kwargs.pop("headers", {}))
        if token:
            # By hand: the cookie is `Secure`, which httpx does not send over http.
            headers["Cookie"] = f"{COOKIE}={token}"
        if method != "GET":
            headers.setdefault("Origin", self.origin)
        return await client.request(
            method, real_url, headers=headers, extensions=extensions, **kwargs
        )


def check_host_allowed(url: str, extra: list[str]) -> None:
    host = urlsplit(url).hostname or ""
    if host in ALLOWED_HOSTS or host in extra:
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_private):
        return
    sys.exit(
        f"Refused: {host} is not one of this app's hosts ({', '.join(sorted(ALLOWED_HOSTS))}"
        ", a private address, or --allow-host)."
    )


# --- Sessions ------------------------------------------------------------------


async def login(target: Target, email: str, password: str, allow_admin: bool) -> tuple[str, dict]:
    async with target.client(2) as client:
        for attempt in range(10):
            response = await target.request(
                client,
                "POST",
                f"{target.api}/auth/login",
                json={"email": email, "password": password, "remember": False},
            )
            if response.status_code == 429 and attempt < 9:
                await asyncio.sleep(1.5)  # nginx allows 2 logins a second per IP
                continue
            break
    if response.status_code != 200:
        sys.exit(f"Login failed: {response.status_code} {response.text[:200]}")
    me = response.json()
    if me.get("role") == "admin" and not allow_admin:
        sys.exit(
            f"{email} is an admin. Use a throwaway member account for load "
            "(create_user.py without --admin), or pass --allow-admin."
        )
    token = response.cookies.get(COOKIE)
    if not token:
        # Over http the Secure cookie is not stored; read it from the header.
        header = response.headers.get("set-cookie", "")
        token = header.split(f"{COOKIE}=", 1)[1].split(";", 1)[0] if f"{COOKIE}=" in header else ""
    if not token:
        sys.exit("Login answered 200 without a session cookie.")
    return token, me


async def open_sessions(
    target: Target, email: str, password: str, count: int, allow_admin: bool
) -> tuple[list[str], dict]:
    tokens = []
    me: dict = {}
    for _ in range(count):
        token, me = await login(target, email, password, allow_admin)
        tokens.append(token)
    return tokens, me


async def close_sessions(target: Target, tokens: list[str]) -> None:
    async with target.client(2) as client:
        for token in tokens:
            try:
                await target.request(client, "POST", f"{target.api}/auth/logout", token=token)
            except httpx.HTTPError:
                pass


async def item_ids(target: Target, token: str) -> list[str]:
    async with target.client(1) as client:
        response = await target.request(client, "GET", f"{target.api}/items", token=token)
    if response.status_code != 200:
        return []
    return [item["id"] for item in response.json()]


# --- The load itself: worker processes ------------------------------------------


def _pick(mix: dict, rng: random.Random) -> str:
    names = list(mix)
    return rng.choices(names, weights=[mix[n] for n in names])[0]


async def _one(
    target: Target,
    client: httpx.AsyncClient,
    name: str,
    token: str,
    ids: list[str],
    rng: random.Random,
) -> int:
    api = target.api
    if name == "items":
        url = f"{api}/items"
    elif name == "search":
        url = f"{api}/items/search?q={quote(rng.choice(SEARCH_TERMS))}"
    elif name == "similar":
        url = f"{api}/items/{rng.choice(ids)}/similar"
    elif name == "me":
        url = f"{api}/auth/me"
    elif name == "files":
        url = f"{api}/files"
    elif name == "ready":
        url = f"{api}/health/ready"
    elif name == "live":
        url = f"{api}/health/live"
    elif name == "web":
        url = f"{target.web}/"
    else:
        raise ValueError(name)
    response = await target.request(client, "GET", url, token=token)
    # Read to the end, as a browser would; the timing includes the body.
    await response.aread()
    return response.status_code


async def _worker_loop(
    target,
    client,
    mix,
    tokens,
    ids,
    index,
    start,
    active_from,
    active_until,
    think_s,
    bucket_s,
    stats,
    seed,
):
    rng = random.Random(seed * 100_003 + index)
    token = tokens[index % len(tokens)]
    begin = start + active_from
    end = start + active_until
    now = time.monotonic()
    if begin > now:
        await asyncio.sleep(begin - now)
    while (now := time.monotonic()) < end:
        name = _pick(mix, rng)
        started = time.perf_counter()
        try:
            outcome = classify(await _one(target, client, name, token, ids, rng))
        except httpx.HTTPError as exc:
            outcome = classify_exc(exc)
        ms = (time.perf_counter() - started) * 1000
        key = (int((now - start) // bucket_s), name)
        entry = stats[key]
        entry["outcomes"][outcome] += 1
        if outcome == "ok":
            entry["hist"][_bin(ms)] += 1
        if think_s:
            await asyncio.sleep(rng.uniform(0, 2 * think_s))


def _process_main(spec: dict) -> dict:
    """One worker process: its share of the workers, one event loop."""
    target = Target(**spec["target"])
    stats: dict = defaultdict(lambda: {"outcomes": Counter(), "hist": Counter()})

    async def run():
        async with target.client(max(1, len(spec["workers"]))) as client:
            start = time.monotonic() + (spec["start_wall"] - time.time())
            await asyncio.gather(
                *(
                    _worker_loop(
                        target,
                        client,
                        spec["mix"],
                        spec["tokens"],
                        spec["ids"],
                        index,
                        start,
                        active_from,
                        active_until,
                        spec["think_s"],
                        spec["bucket_s"],
                        stats,
                        spec["seed"],
                    )
                    for index, active_from, active_until in spec["workers"]
                )
            )

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return {
        f"{bucket}|{name}": {"outcomes": dict(v["outcomes"]), "hist": dict(v["hist"])}
        for (bucket, name), v in stats.items()
    }


class Monitor:
    """In the main process while the load runs: the liveness probe (as Docker's
    health check sees the API) and the host's load, memory and free disk."""

    def __init__(self, target: Target):
        self.target = target
        self.stop = threading.Event()
        self.probes: list[tuple[float, float | None, str]] = []
        self.host: list[dict] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.started = time.monotonic()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=20)

    def _run(self):
        url, headers, extensions = self.target.route(f"{self.target.api}/health/live")
        with httpx.Client(timeout=15, verify=not self.target.insecure) as client:
            while not self.stop.is_set():
                t = time.monotonic() - self.started
                started = time.perf_counter()
                try:
                    response = client.get(url, headers=headers, extensions=extensions)
                    outcome = classify(response.status_code)
                    ms = (time.perf_counter() - started) * 1000
                except httpx.HTTPError as exc:
                    outcome, ms = classify_exc(exc), None
                self.probes.append((round(t, 1), None if ms is None else round(ms, 1), outcome))
                sample = host_sample()
                if sample:
                    self.host.append({"t": round(t, 1), **sample})
                self.stop.wait(max(0.0, 1.0 - (time.perf_counter() - started)))

    def summary(self) -> dict:
        latencies = [ms for _, ms, outcome in self.probes if outcome == "ok" and ms is not None]
        # Docker: unhealthy after 3 checks in a row fail or take over 10 s.
        streak = worst = 0
        for _, ms, outcome in self.probes:
            bad = outcome in FAILURES or (ms is not None and ms > 10_000)
            streak = streak + 1 if bad else 0
            worst = max(worst, streak)
        result = {
            "probes": len(self.probes),
            "probe_failures": sum(1 for _, _, o in self.probes if o in FAILURES),
            "probe_max_ms": max(latencies, default=None),
            "probe_p95_ms": sorted(latencies)[int(0.95 * (len(latencies) - 1))]
            if latencies
            else None,
            "probe_worst_failure_streak": worst,
            "watchdog_would_restart": worst >= 3,
        }
        if self.host:
            result["host_max_load1"] = max(h["load1"] for h in self.host)
            result["host_min_mem_available_mb"] = min(h["mem_available_mb"] for h in self.host)
            result["host_min_disk_free_gb"] = min(h["disk_free_gb"] for h in self.host)
        return result


def host_sample() -> dict | None:
    """Linux only. In a container /proc/loadavg and /proc/meminfo are the host's, and
    `/` is on the host's disk, so these describe the server."""
    try:
        load1 = float(Path("/proc/loadavg").read_text().split()[0])
        meminfo = Path("/proc/meminfo").read_text()
        available_kb = int(
            next(
                line.split()[1] for line in meminfo.splitlines() if line.startswith("MemAvailable:")
            )
        )
        disk = os.statvfs("/")
    except OSError, StopIteration, AttributeError:
        return None
    return {
        "load1": load1,
        "mem_available_mb": available_kb // 1024,
        "disk_free_gb": round(disk.f_bavail * disk.f_frsize / 1024**3, 1),
    }


def run_load(
    target: Target,
    tokens: list[str],
    ids: list[str],
    mix: dict,
    schedule: list[tuple[float, float]],
    procs: int,
    think_s: float,
    bucket_s: float,
    seed: int,
) -> tuple[dict, dict]:
    """Runs workers on the given schedule (each one's start and end, in seconds from
    the start) across processes. Returns the merged statistics by (bucket, endpoint),
    and the monitor's summary."""
    procs = max(1, min(procs, len(schedule)))
    start_wall = time.time() + 1.5 + 0.5 * procs
    specs = []
    for p in range(procs):
        workers = [(i, a, b) for i, (a, b) in enumerate(schedule) if i % procs == p]
        specs.append(
            {
                "target": target.as_dict(),
                "tokens": tokens,
                "ids": ids,
                "mix": mix,
                "workers": workers,
                "think_s": think_s,
                "bucket_s": bucket_s,
                "seed": seed + p,
                # One wall-clock start for all processes, after they have had time to
                # spawn, so a spike begins at the same moment in each of them.
                "start_wall": start_wall,
            }
        )
    merged: dict = defaultdict(lambda: {"outcomes": Counter(), "hist": Counter()})
    context = multiprocessing.get_context("spawn")
    with Monitor(target) as monitor, context.Pool(procs) as pool:
        try:
            results = pool.map(_process_main, specs)
        except KeyboardInterrupt:
            pool.terminate()
            raise
    for result in results:
        for key, value in result.items():
            bucket, name = key.split("|")
            merged[(int(bucket), name)]["outcomes"].update(value["outcomes"])
            merged[(int(bucket), name)]["hist"].update(
                {int(k): v for k, v in value["hist"].items()}
            )
    return merged, monitor.summary()


def summarize(stats: dict, seconds: float, buckets: set | None = None) -> dict:
    """Totals over the given buckets (all by default), overall and per endpoint."""
    total_outcomes: Counter = Counter()
    total_hist: Counter = Counter()
    per_endpoint: dict = defaultdict(lambda: {"outcomes": Counter(), "hist": Counter()})
    for (bucket, name), value in stats.items():
        if buckets is not None and bucket not in buckets:
            continue
        total_outcomes.update(value["outcomes"])
        total_hist.update(value["hist"])
        per_endpoint[name]["outcomes"].update(value["outcomes"])
        per_endpoint[name]["hist"].update(value["hist"])
    count = sum(total_outcomes.values())
    failures = sum(n for outcome, n in total_outcomes.items() if outcome in FAILURES)
    return {
        "requests": count,
        "rps": round(count / seconds, 1) if seconds else 0,
        "ok_rps": round(total_outcomes["ok"] / seconds, 1) if seconds else 0,
        "outcomes": dict(total_outcomes),
        "failure_rate": round(failures / count, 4) if count else 0,
        "p50_ms": round(percentile(total_hist, 0.50), 1),
        "p95_ms": round(percentile(total_hist, 0.95), 1),
        "p99_ms": round(percentile(total_hist, 0.99), 1),
        "max_ms": round(_bin_ms(max(total_hist)), 1) if total_hist else 0,
        "endpoints": {
            name: {
                "requests": sum(v["outcomes"].values()),
                "failures": sum(n for o, n in v["outcomes"].items() if o in FAILURES),
                "limited": v["outcomes"].get("limited", 0),
                "p95_ms": round(percentile(v["hist"], 0.95), 1),
            }
            for name, v in sorted(per_endpoint.items())
        },
    }


def timeline(stats: dict, bucket_s: float) -> list[dict]:
    rows = []
    for bucket in sorted({b for b, _ in stats}):
        s = summarize(stats, bucket_s, {bucket})
        rows.append(
            {
                "t": int(bucket * bucket_s),
                "rps": s["rps"],
                "ok_rps": s["ok_rps"],
                "failure_rate": s["failure_rate"],
                "p50_ms": s["p50_ms"],
                "p95_ms": s["p95_ms"],
                "limited": s["outcomes"].get("limited", 0),
            }
        )
    return rows


# --- Checks ----------------------------------------------------------------------


async def baseline(target: Target, token: str, samples: int = 10) -> float:
    """Median latency of /items with nothing else running, in ms."""
    times = []
    async with target.client(1) as client:
        for _ in range(samples):
            started = time.perf_counter()
            try:
                response = await target.request(client, "GET", f"{target.api}/items", token=token)
                if response.status_code == 200:
                    times.append((time.perf_counter() - started) * 1000)
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    return round(sorted(times)[len(times) // 2], 1) if times else 0.0


async def recovery(target: Target, token: str, normal_ms: float, limit_s: float = 180) -> dict:
    """After the load: seconds until /health/ready answers 200 and /items is back
    near its normal speed (median of 5 under 3x the baseline, or under 50 ms)."""
    started = time.monotonic()
    ready_after = None
    async with target.client(2) as client:
        while time.monotonic() - started < limit_s:
            try:
                ready = await target.request(client, "GET", f"{target.api}/health/ready")
                if ready.status_code == 200 and ready_after is None:
                    ready_after = round(time.monotonic() - started, 1)
                if ready_after is not None:
                    times = []
                    for _ in range(5):
                        t0 = time.perf_counter()
                        response = await target.request(
                            client, "GET", f"{target.api}/items", token=token
                        )
                        if response.status_code == 200:
                            times.append((time.perf_counter() - t0) * 1000)
                    median = sorted(times)[len(times) // 2] if len(times) == 5 else None
                    if median is not None and median < max(3 * normal_ms, 50):
                        return {
                            "recovered": True,
                            "ready_after_s": ready_after,
                            "normal_after_s": round(time.monotonic() - started, 1),
                            "items_median_ms": round(median, 1),
                        }
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    return {"recovered": False, "ready_after_s": ready_after, "limit_s": limit_s}


def _pdf_chunks(seed: int, size_mb: int):
    """A PDF-looking body of `size_mb` MB, generated as it is sent, and its SHA-256."""
    digest = hashlib.sha256()
    rng = random.Random(seed)

    def chunks():
        head = b"%PDF-1.4\n"
        digest.update(head)
        yield head
        remaining = size_mb * 1024 * 1024 - len(head)
        while remaining > 0:
            chunk = rng.randbytes(min(1024 * 1024, remaining))
            digest.update(chunk)
            remaining -= len(chunk)
            yield chunk

    return chunks, digest


async def upload_one(
    target: Target, client: httpx.AsyncClient, token: str, name: str, size_mb: int, seed: int
) -> dict:
    boundary = f"loadtest{seed}"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{name}"\r\nContent-Type: application/pdf\r\n\r\n'
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    chunks, digest = _pdf_chunks(seed, size_mb)

    async def body():
        yield head
        for chunk in chunks():
            yield chunk
        yield tail

    length = len(head) + size_mb * 1024 * 1024 + len(tail)
    started = time.perf_counter()
    try:
        response = await target.request(
            client,
            "POST",
            f"{target.api}/files",
            token=token,
            content=body(),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(length),
            },
        )
    except httpx.HTTPError as exc:
        return {"name": name, "ok": False, "error": classify_exc(exc)}
    upload_s = time.perf_counter() - started
    if response.status_code != 201:
        return {
            "name": name,
            "ok": False,
            "status": response.status_code,
            "detail": response.text[:200],
        }
    file_id = response.json()["id"]

    started = time.perf_counter()
    downloaded = hashlib.sha256()
    try:
        real_url, headers, extensions = target.route(f"{target.api}/files/{file_id}/download")
        headers["Cookie"] = f"{COOKIE}={token}"
        async with client.stream("GET", real_url, headers=headers, extensions=extensions) as r:
            status = r.status_code
            async for chunk in r.aiter_bytes():
                downloaded.update(chunk)
    except httpx.HTTPError as exc:
        return {"name": name, "ok": False, "error": f"download {classify_exc(exc)}"}
    download_s = time.perf_counter() - started
    same = status == 200 and downloaded.hexdigest() == digest.hexdigest()
    return {
        "name": name,
        "ok": same,
        "status": status,
        "same_bytes": same,
        "upload_mb_s": round(size_mb / upload_s, 1),
        "download_mb_s": round(size_mb / download_s, 1) if download_s else None,
    }


async def stream_one(target: Target, client: httpx.AsyncClient, token: str) -> dict:
    started = time.perf_counter()
    first_delta = done = None
    deltas = 0
    try:
        real_url, headers, extensions = target.route(f"{target.api}/stream")
        headers.update({"Cookie": f"{COOKIE}={token}", "Origin": target.origin})
        async with client.stream(
            "POST",
            real_url,
            headers=headers,
            extensions=extensions,
            timeout=httpx.Timeout(target.timeout, read=120),
        ) as response:
            if response.status_code != 200:
                return {"ok": False, "status": response.status_code}
            async for line in response.aiter_lines():
                if line.startswith("event: delta"):
                    deltas += 1
                    if first_delta is None:
                        first_delta = time.perf_counter() - started
                elif line.startswith("event: done"):
                    done = time.perf_counter() - started
                    break
    except httpx.HTTPError as exc:
        return {"ok": False, "error": classify_exc(exc), "deltas": deltas}
    return {
        "ok": done is not None and deltas == 40,
        "deltas": deltas,
        "first_delta_s": None if first_delta is None else round(first_delta, 2),
        "done_s": None if done is None else round(done, 2),
        # Pieces that arrive together mean a proxy buffered them.
        "spread_s": None if done is None or first_delta is None else round(done - first_delta, 2),
    }


# --- Modes -------------------------------------------------------------------------


async def mode_smoke(target: Target, token: str, run_id: str) -> dict:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})
        log(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")

    async with target.client(4) as client:

        async def get(path: str, with_token=True, base=None):
            return await target.request(
                client, "GET", f"{base or target.api}{path}", token=token if with_token else None
            )

        r = await get("/health/live", with_token=False)
        check(
            "health/live",
            r.status_code == 200,
            r.json().get("version", "") if r.status_code == 200 else str(r.status_code),
        )
        r = await get("/health/ready", with_token=False)
        check(
            "health/ready (database)",
            r.status_code == 200 and r.json().get("db") == "ok",
            str(r.status_code),
        )
        r = await get("/auth/me")
        me = r.json() if r.status_code == 200 else {}
        check(
            "session (/auth/me)",
            r.status_code == 200,
            f"{me.get('email')} seen as {me.get('seen_ip')} over {me.get('scheme')}",
        )
        if target.api.startswith("https://"):
            # The address is shown, not checked: from the laptop it must be the
            # laptop's public IP; from the server itself it is Docker's gateway.
            check(
                "the api sees https through nginx (X-Forwarded-Proto)",
                me.get("scheme") == "https",
                f"scheme={me.get('scheme')} ip={me.get('seen_ip')}",
            )
        r = await get("/items")
        items = r.json() if r.status_code == 200 else []
        check(
            "items",
            r.status_code == 200 and len(items) > 0,
            f"{len(items)} items" if r.status_code == 200 else str(r.status_code),
        )
        r = await get(f"/items/search?q={quote('caietul de sarcini')}")
        check(
            "full-text search",
            r.status_code == 200 and len(r.json()) > 0,
            f"{len(r.json())} hits" if r.status_code == 200 else str(r.status_code),
        )
        if items:
            r = await get(f"/items/{items[0]['id']}/similar")
            check(
                "similar (pgvector)",
                r.status_code == 200 and len(r.json()) == 5,
                str(r.status_code),
            )
        r = await get("/files")
        check("file list", r.status_code == 200, str(r.status_code))
        result = await upload_one(target, client, token, f"loadtest-{run_id}-smoke.pdf", 1, seed=1)
        check("upload 1 MB and download the same bytes", result["ok"], json.dumps(result))
        r = await get("/items", with_token=False)
        check("no session: 401", r.status_code == 401, str(r.status_code))
        r = await target.request(
            client, "POST", f"{target.api}/auth/logout", headers={"Origin": "https://evil.example"}
        )
        check("foreign origin on a POST: 403", r.status_code == 403, str(r.status_code))
        if target.web:
            r = await get("/login", with_token=False, base=target.web)
            check(
                "web: login page",
                r.status_code == 200 and "<html" in r.text.lower(),
                str(r.status_code),
            )
            r = await get("/", base=target.web)
            check(
                "web: dashboard (server-rendered with the session)",
                r.status_code == 200 and str(me.get("email")) in r.text,
                str(r.status_code),
            )
            csp = r.headers.get("content-security-policy", "")
            check("web: CSP with a nonce", "nonce-" in csp, csp[:60])
    log("Stream (waits through the silent phase, about 25 s on the server)")
    async with target.client(1) as client:
        result = await stream_one(target, client, token)
    check(
        "stream arrives in pieces, then done",
        result["ok"] and (result.get("spread_s") or 0) > 3,
        json.dumps(result),
    )
    return {"checks": checks, "passed": all(c["ok"] for c in checks)}


def mode_ramp(target, tokens, ids, mix, args, normal_ms) -> dict:
    steps = []
    breaking = None
    for level in args.levels:
        log(f"Ramp: {level} concurrent for {args.step_seconds} s")
        stats, monitor = run_load(
            target,
            tokens,
            ids,
            mix,
            [(0, args.step_seconds)] * level,
            args.procs,
            args.think_ms / 1000,
            args.step_seconds,
            args.seed,
        )
        s = summarize(stats, args.step_seconds)
        s.update({"concurrency": level, **monitor})
        steps.append(s)
        log(
            f"  {s['rps']:>8} req/s  ok {s['ok_rps']:>8}/s  p50 {s['p50_ms']:>7} ms  "
            f"p95 {s['p95_ms']:>7} ms  failures {s['failure_rate']:.1%}  "
            f"429 {s['outcomes'].get('limited', 0)}  probe max {monitor['probe_max_ms']} ms"
        )
        if s["failure_rate"] > args.max_failure_rate or s["p95_ms"] > args.max_p95_ms:
            breaking = level
            log(
                f"  Breaking point: {level} (failures {s['failure_rate']:.1%}, p95 {s['p95_ms']} ms)"
            )
            break
        time.sleep(3)
    best = max(steps, key=lambda s: s["ok_rps"]) if steps else None
    log("Waiting for the server to recover")
    rec = asyncio.run(recovery(target, tokens[0], normal_ms))
    log(f"  Recovery: {rec}")
    return {
        "steps": steps,
        "breaking_point": breaking,
        "best_ok_rps": best["ok_rps"] if best else 0,
        "best_at_concurrency": best["concurrency"] if best else None,
        "recovery": rec,
    }


def mode_spike(target, tokens, ids, mix, args, normal_ms) -> dict:
    calm, peak = args.calm, args.peak
    t1 = args.calm_seconds
    t2 = t1 + args.peak_seconds
    t3 = t2 + args.after_seconds
    schedule = [(0, t3)] * calm + [(t1, t2)] * (peak - calm)
    log(f"Spike: {calm} concurrent, {peak} from {t1} s to {t2} s, {calm} until {t3} s")
    stats, monitor = run_load(
        target, tokens, ids, mix, schedule, args.procs, args.think_ms / 1000, 5, args.seed
    )
    rows = timeline(stats, 5)
    for row in rows:
        log(
            f"  t={row['t']:>4}s  {row['rps']:>8} req/s  p95 {row['p95_ms']:>7} ms  "
            f"failures {row['failure_rate']:.1%}  429 {row['limited']}"
        )
    before = summarize(stats, t1, {b for b, _ in stats if b * 5 < t1})
    after = summarize(stats, 15, {b for b, _ in stats if t3 - 15 <= b * 5 < t3})
    during = summarize(stats, args.peak_seconds, {b for b, _ in stats if t1 <= b * 5 < t2})
    rec = asyncio.run(recovery(target, tokens[0], normal_ms))
    return {
        "timeline": rows,
        "before": before,
        "during": during,
        "last_15_s": after,
        "recovered_to_before": after["p95_ms"] <= max(2 * before["p95_ms"], 50)
        and after["failure_rate"] <= 0.01,
        "recovery": rec,
        **monitor,
    }


def mode_soak(target, tokens, ids, mix, args) -> dict:
    seconds = args.minutes * 60
    log(f"Soak: {args.concurrency} concurrent for {args.minutes} min")
    stats, monitor = run_load(
        target,
        tokens,
        ids,
        mix,
        [(0, seconds)] * args.concurrency,
        args.procs,
        args.think_ms / 1000,
        60,
        args.seed,
    )
    rows = timeline(stats, 60)
    for row in rows:
        log(
            f"  min {row['t'] // 60:>3}  {row['rps']:>8} req/s  p95 {row['p95_ms']:>7} ms  "
            f"failures {row['failure_rate']:.1%}"
        )
    first = rows[: max(1, len(rows) // 4)]
    last = rows[-max(1, len(rows) // 4) :]
    drift = (sum(r["p95_ms"] for r in last) / len(last)) / max(
        1.0, sum(r["p95_ms"] for r in first) / len(first)
    )
    return {
        "timeline": rows,
        "total": summarize(stats, seconds),
        "p95_drift_last_vs_first_quarter": round(drift, 2),
        **monitor,
    }


async def mode_streams(target, tokens, count) -> dict:
    log(f"Streams: {count} at once")
    async with target.client(count) as client:
        results = await asyncio.gather(
            *(stream_one(target, client, tokens[i % len(tokens)]) for i in range(count))
        )
    ok = [r for r in results if r["ok"]]
    firsts = sorted(r["first_delta_s"] for r in ok)
    summary = {
        "count": count,
        "complete": len(ok),
        "failures": Counter(
            r.get("error") or str(r.get("status", "incomplete")) for r in results if not r["ok"]
        ),
        "first_delta_s_median": firsts[len(firsts) // 2] if firsts else None,
        "first_delta_s_max": firsts[-1] if firsts else None,
        "buffered": sum(1 for r in ok if (r.get("spread_s") or 0) < 3),
    }
    log(f"  {summary}")
    return summary


async def mode_uploads(target, tokens, args, run_id) -> dict:
    total = args.count * args.mb
    if total > args.max_total_mb:
        sys.exit(
            f"{args.count} x {args.mb} MB = {total} MB is over --max-total-mb {args.max_total_mb}."
        )
    log(f"Uploads: {args.count} x {args.mb} MB, {args.concurrency} at a time")
    semaphore = asyncio.Semaphore(args.concurrency)
    async with target.client(args.concurrency * 2) as client:

        async def one(n):
            async with semaphore:
                return await upload_one(
                    target,
                    client,
                    tokens[n % len(tokens)],
                    f"loadtest-{run_id}-{n:03}.pdf",
                    args.mb,
                    seed=1000 + n,
                )

        results = await asyncio.gather(*(one(n) for n in range(args.count)))
    for result in results:
        log(f"  {result}")
    good = [r for r in results if r["ok"]]
    return {
        "count": args.count,
        "mb_each": args.mb,
        "identical_after_download": len(good),
        "results": results,
        "upload_mb_s_median": sorted(r["upload_mb_s"] for r in good)[len(good) // 2]
        if good
        else None,
    }


# --- Report -------------------------------------------------------------------------


def markdown(report: dict) -> str:
    lines = [
        f"# Load test {report['run_id']}",
        "",
        f"- Target: `{report['target']['api']}`"
        + (f", web `{report['target']['web']}`" if report["target"]["web"] else ""),
        f"- Mode: {report['mode']}, {report['started']} UTC, {report.get('duration_s')} s",
        f"- Normal /items latency before the load: {report.get('normal_ms')} ms",
        "",
    ]
    smoke = report.get("smoke")
    if smoke:
        lines += ["## Smoke", "", "| Check | Result | Detail |", "|---|---|---|"]
        lines += [
            f"| {c['check']} | {'ok' if c['ok'] else '**FAIL**'} | {c['detail'][:120]} |"
            for c in smoke["checks"]
        ]
        lines.append("")
    ramp = report.get("ramp")
    if ramp:
        lines += [
            "## Ramp",
            "",
            "| Concurrency | req/s | ok/s | p50 ms | p95 ms | p99 ms | failures | 429 | probe max ms | load1 | mem free MB |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for s in ramp["steps"]:
            lines.append(
                f"| {s['concurrency']} | {s['rps']} | {s['ok_rps']} | {s['p50_ms']} | {s['p95_ms']} | {s['p99_ms']} | "
                f"{s['failure_rate']:.1%} | {s['outcomes'].get('limited', 0)} | {s['probe_max_ms']} | "
                f"{s.get('host_max_load1', '')} | {s.get('host_min_mem_available_mb', '')} |"
            )
        lines += [
            "",
            f"- Breaking point: **{ramp['breaking_point'] or 'not reached'}**",
            f"- Best throughput: {ramp['best_ok_rps']} ok/s at {ramp['best_at_concurrency']} concurrent",
            f"- Recovery: {ramp['recovery']}",
            f"- Watchdog would have restarted the API: {any(s['watchdog_would_restart'] for s in ramp['steps'])}",
            "",
        ]
    spike = report.get("spike")
    if spike:
        lines += [
            "## Spike",
            "",
            "| t (s) | req/s | p95 ms | failures | 429 |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {r['t']} | {r['rps']} | {r['p95_ms']} | {r['failure_rate']:.1%} | {r['limited']} |"
            for r in spike["timeline"]
        ]
        lines += [
            "",
            f"- p95 before {spike['before']['p95_ms']} ms, during {spike['during']['p95_ms']} ms, last 15 s {spike['last_15_s']['p95_ms']} ms",
            f"- Back to normal by itself: **{spike['recovered_to_before']}**; watchdog would restart: {spike['watchdog_would_restart']}",
            "",
        ]
    soak = report.get("soak")
    if soak:
        lines += ["## Soak", "", "| Minute | req/s | p95 ms | failures |", "|---|---|---|---|"]
        lines += [
            f"| {r['t'] // 60} | {r['rps']} | {r['p95_ms']} | {r['failure_rate']:.1%} |"
            for r in soak["timeline"]
        ]
        lines += [
            "",
            f"- p95 drift (last quarter / first quarter): {soak['p95_drift_last_vs_first_quarter']}",
            f"- Host: max load {soak.get('host_max_load1')}, min free memory {soak.get('host_min_mem_available_mb')} MB, min free disk {soak.get('host_min_disk_free_gb')} GB",
            "",
        ]
    if report.get("streams"):
        lines += ["## Streams", "", f"```\n{json.dumps(report['streams'], indent=2)}\n```", ""]
    if report.get("uploads"):
        u = report["uploads"]
        lines += [
            "## Uploads",
            "",
            f"- {u['identical_after_download']} of {u['count']} x {u['mb_each']} MB came back identical; median upload {u['upload_mb_s_median']} MB/s",
            "",
        ]
    if report.get("final_smoke"):
        lines += [
            "## Smoke after everything",
            "",
            f"- Passed: **{report['final_smoke']['passed']}**",
            "",
        ]
    return "\n".join(lines)


def write_report(report: dict, out: str | None) -> None:
    text = markdown(report)
    print("\n" + text)
    if out:
        folder = Path(out)
        folder.mkdir(parents=True, exist_ok=True)
        stem = folder / f"loadtest-{report['run_id']}-{report['mode']}"
        stem.with_suffix(".json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        stem.with_suffix(".md").write_text(text, encoding="utf-8")
        log(f"Report: {stem}.md and .json")


# --- Main -------------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--api", required=True, help="https://rootfolder.app/api or http://api:8000"
    )
    common.add_argument(
        "--web", help="https://rootfolder.app or http://web:3000 (adds the page to the mix)"
    )
    common.add_argument("--email", required=True, help="a throwaway member account")
    common.add_argument(
        "--origin",
        default="https://rootfolder.app",
        help="the Origin header for POSTs: one of the app's CORS_ORIGINS",
    )
    common.add_argument(
        "--connect-to",
        help="send to this address instead (nginx, 127.0.0.1); "
        "the Host header and TLS name stay those of --api",
    )
    common.add_argument(
        "--insecure", action="store_true", help="skip TLS verification (local certificate)"
    )
    common.add_argument(
        "--allow-host", action="append", default=[], help="another host this may load"
    )
    common.add_argument("--allow-admin", action="store_true")
    common.add_argument("--sessions", type=int, default=4, help="logins to spread the load over")
    common.add_argument("--procs", type=int, default=2, help="worker processes (each one CPU core)")
    common.add_argument(
        "--think-ms", type=float, default=0, help="average pause between a worker's requests"
    )
    common.add_argument(
        "--timeout", type=float, default=30, help="seconds before a request counts as a timeout"
    )
    common.add_argument("--seed", type=int, default=7)
    common.add_argument("--out", help="folder for the JSON and Markdown report")
    common.add_argument("--yes", action="store_true", help="no 5-second pause before starting")

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    modes = parser.add_subparsers(dest="mode", required=True)

    def levels(text):
        values = [int(v) for v in text.split(",")]
        if any(v < 1 or v > MAX_CONCURRENCY for v in values):
            raise argparse.ArgumentTypeError(f"between 1 and {MAX_CONCURRENCY}")
        return values

    modes.add_parser("smoke", parents=[common])
    for name in ("ramp", "suite"):
        p = modes.add_parser(name, parents=[common])
        p.add_argument("--levels", type=levels, default=levels("10,25,50,100,200,400"))
        p.add_argument("--step-seconds", type=float, default=20)
        p.add_argument("--max-failure-rate", type=float, default=0.05)
        p.add_argument("--max-p95-ms", type=float, default=5000)
        if name == "suite":
            p.add_argument("--streams", type=int, default=50)
            p.add_argument("--upload-count", type=int, default=4)
            p.add_argument("--upload-mb", type=int, default=20)
            p.add_argument("--max-total-mb", type=int, default=2048)
            p.add_argument("--peak", type=int, default=300)
    p = modes.add_parser("spike", parents=[common])
    p.add_argument("--calm", type=int, default=10)
    p.add_argument("--peak", type=int, default=300)
    p.add_argument("--calm-seconds", type=float, default=15)
    p.add_argument("--peak-seconds", type=float, default=30)
    p.add_argument("--after-seconds", type=float, default=45)
    p = modes.add_parser("soak", parents=[common])
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--minutes", type=float, default=15)
    p = modes.add_parser("streams", parents=[common])
    p.add_argument("--count", type=int, default=50)
    p = modes.add_parser("uploads", parents=[common])
    p.add_argument("--count", type=int, default=6)
    p.add_argument("--mb", type=int, default=50)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--max-total-mb", type=int, default=2048)
    args = parser.parse_args()

    for url in filter(None, (args.api, args.web)):
        check_host_allowed(url, args.allow_host)
    if args.connect_to:
        check_host_allowed(f"https://{args.connect_to}/", args.allow_host)
    if (
        args.insecure
        and not args.connect_to
        and urlsplit(args.api).hostname in ALLOWED_HOSTS - {"localhost"}
    ):
        sys.exit(
            "--insecure only with --connect-to or localhost: the public site has a real certificate."
        )
    concurrency = getattr(args, "concurrency", 0) or 0
    if getattr(args, "peak", 0) > MAX_CONCURRENCY or concurrency > MAX_CONCURRENCY:
        sys.exit(f"At most {MAX_CONCURRENCY} concurrent.")
    return args


def main() -> int:
    # Windows consoles default to a code page without ă, ț or emoji.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    password = os.environ.get("LOADTEST_PASSWORD") or getpass.getpass(
        f"Password for {args.email}: "
    )
    target = Target(args.api, args.web, args.origin, args.connect_to, args.insecure, args.timeout)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    mix = {k: v for k, v in MIX.items() if k != "web" or args.web}

    log(
        f"Run {run_id}: {args.mode} against {target.api}"
        + (f" (connecting to {args.connect_to})" if args.connect_to else "")
    )
    if not args.yes and sys.stdin.isatty():
        log("Starting in 5 s; Ctrl+C to stop")
        time.sleep(5)

    started = time.monotonic()
    tokens, me = asyncio.run(
        open_sessions(target, args.email, password, max(1, args.sessions), args.allow_admin)
    )
    log(
        f"{len(tokens)} sessions as {me.get('email')} ({me.get('role')}, {me.get('org_name')}); "
        f"the api sees {me.get('seen_ip')} over {me.get('scheme')}"
    )
    ids = asyncio.run(item_ids(target, tokens[0]))
    if not ids:
        mix.pop("similar", None)
        log("No items in this org (seed.py): similar left out of the mix")
    normal_ms = asyncio.run(baseline(target, tokens[0]))
    log(f"Normal /items latency: {normal_ms} ms")

    report: dict = {
        "run_id": run_id,
        "mode": args.mode,
        "target": target.as_dict(),
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "account": me.get("email"),
        "normal_ms": normal_ms,
        "args": {k: v for k, v in vars(args).items() if k not in ("email",)},
    }
    try:
        if args.mode in ("smoke", "suite"):
            report["smoke"] = asyncio.run(mode_smoke(target, tokens[0], run_id))
        if args.mode in ("ramp", "suite"):
            report["ramp"] = mode_ramp(target, tokens, ids, mix, args, normal_ms)
        if args.mode == "spike":
            report["spike"] = mode_spike(target, tokens, ids, mix, args, normal_ms)
        if args.mode == "suite":
            args.calm, args.calm_seconds, args.peak_seconds, args.after_seconds = 10, 15, 30, 45
            report["spike"] = mode_spike(target, tokens, ids, mix, args, normal_ms)
        if args.mode == "soak":
            report["soak"] = mode_soak(target, tokens, ids, mix, args)
        if args.mode == "streams":
            report["streams"] = asyncio.run(mode_streams(target, tokens, args.count))
        if args.mode == "suite":
            report["streams"] = asyncio.run(mode_streams(target, tokens, args.streams))
        if args.mode == "uploads":
            report["uploads"] = asyncio.run(mode_uploads(target, tokens, args, run_id))
        if args.mode == "suite":
            args.count, args.mb, args.concurrency = args.upload_count, args.upload_mb, 2
            report["uploads"] = asyncio.run(mode_uploads(target, tokens, args, run_id))
            report["final_smoke"] = asyncio.run(mode_smoke(target, tokens[0], run_id))
    except KeyboardInterrupt:
        log("Stopped; the report covers what ran")
        report["interrupted"] = True
    finally:
        asyncio.run(close_sessions(target, tokens))
    report["duration_s"] = round(time.monotonic() - started)
    write_report(report, args.out)

    failed = (
        (report.get("smoke") and not report["smoke"]["passed"])
        or (report.get("final_smoke") and not report["final_smoke"]["passed"])
        or (report.get("ramp") and not report["ramp"]["recovery"]["recovered"])
        or (report.get("spike") and not report["spike"]["recovery"]["recovered"])
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
