"""Load test for rootfolder-mock: how many requests the app answers, how fast, and
what breaks first.

Two ways to run it, and they measure different things:

- **Through nginx, from your laptop** (`--base https://rootfolder.app/api`): the
  whole path a user takes, including nginx's rate limits. From a single IP nginx
  starts answering 429 above its limit (20 requests per second per IP, login 1 per
  second, stream 10 per minute). That is the limit working, not the app failing;
  what matters is that nothing answers 5xx and `/health` stays 200.
- **On the server, straight to the api** (`--base http://api:8000`, inside the
  compose network): no nginx, no internet, so this is the app's own capacity.

The password is read from the LOADTEST_PASSWORD environment variable or asked in
the terminal; it never goes on the command line.

See loadtest/README.md for the exact commands, from the laptop and on the server.
"""

import argparse
import asyncio
import getpass
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx

COOKIE = "mock_session"
SEARCH = "/items/search?q=licita%C8%9Bie%20garan%C8%9Bii"


async def login(
    client: httpx.AsyncClient, base: str, email: str, password: str
) -> dict:
    response = await client.post(
        f"{base}/auth/login",
        json={"email": email, "password": password, "remember": False},
    )
    if response.status_code != 200:
        sys.exit(f"Login failed: {response.status_code} {response.text[:200]}")
    # Sent by hand: the cookie is `Secure`, which httpx does not return over plain http.
    return {"Cookie": f"{COOKIE}={response.cookies[COOKIE]}"}


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


async def hammer(
    client: httpx.AsyncClient,
    headers: dict,
    method: str,
    url: str,
    seconds: float,
    workers: int,
):
    """`workers` concurrent loops sending the same request until the time is up."""
    statuses: Counter = Counter()
    latencies: list[float] = []
    deadline = time.monotonic() + seconds

    async def worker():
        while time.monotonic() < deadline:
            started = time.perf_counter()
            try:
                response = await client.request(method, url, headers=headers)
                key = response.status_code
            except httpx.HTTPError as exc:
                key = type(exc).__name__
            latencies.append((time.perf_counter() - started) * 1000)
            statuses[key] += 1

    started = time.monotonic()
    await asyncio.gather(*(worker() for _ in range(workers)))
    return statuses, sorted(latencies), time.monotonic() - started


def row(
    name: str, workers: int, statuses: Counter, latencies: list[float], elapsed: float
) -> str:
    total = sum(statuses.values())
    ok = sum(
        n for key, n in statuses.items() if isinstance(key, int) and 200 <= key < 300
    )
    limited = statuses.get(429, 0)
    bad = total - ok - limited
    return (
        f"{name:<9} {workers:>5} {total:>7} {total / elapsed:>7.0f} "
        f"{ok:>6} {limited:>5} {bad:>5} "
        f"{percentile(latencies, 0.50):>7.0f} {percentile(latencies, 0.95):>7.0f} "
        f"{percentile(latencies, 0.99):>7.0f} {latencies[-1] if latencies else 0:>7.0f}"
    )


HEADER = (
    f"{'scenario':<9} {'conc':>5} {'reqs':>7} {'req/s':>7} {'2xx':>6} {'429':>5} {'bad':>5} "
    f"{'p50ms':>7} {'p95ms':>7} {'p99ms':>7} {'maxms':>7}"
)


async def run_streams(client, headers, base: str, count: int) -> None:
    print(
        f"\n{count} concurrent streams (each waits for the silent phase, then 40 words):"
    )

    async def one() -> tuple[object, float, float]:
        started = time.perf_counter()
        first = 0.0
        try:
            async with client.stream(
                "POST", f"{base}/stream", headers=headers
            ) as response:
                if response.status_code != 200:
                    return response.status_code, 0.0, 0.0
                async for line in response.aiter_lines():
                    if line.startswith("event: delta") and not first:
                        first = time.perf_counter() - started
        except httpx.HTTPError as exc:
            return type(exc).__name__, 0.0, 0.0
        return 200, first, time.perf_counter() - started

    results = await asyncio.gather(*(one() for _ in range(count)))
    statuses = Counter(status for status, _, _ in results)
    done = [r for r in results if r[0] == 200]
    print(f"  statuses: {dict(statuses)}")
    if done:
        firsts = sorted(r[1] for r in done)
        totals = sorted(r[2] for r in done)
        print(
            f"  first word after: median {percentile(firsts, 0.5):.1f} s, "
            f"worst {firsts[-1]:.1f} s; finished after: median {percentile(totals, 0.5):.1f} s, "
            f"worst {totals[-1]:.1f} s"
        )


async def run_upload(client, headers, base: str, megabytes: int) -> None:
    name = f"loadtest-{megabytes}mb.pdf"
    print(f"\nUploading {name} ({megabytes} MB)...")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(b"%PDF-1.4\n")
        block = os.urandom(1024 * 1024)
        for _ in range(megabytes):
            tmp.write(block)
        tmp.write(b"\n%%EOF\n")
        path = Path(tmp.name)
    try:
        started = time.perf_counter()
        with path.open("rb") as handle:
            response = await client.post(
                f"{base}/files",
                headers=headers,
                files={"file": (name, handle, "application/pdf")},
                timeout=None,
            )
        elapsed = time.perf_counter() - started
        print(
            f"  {response.status_code} in {elapsed:.1f} s ({megabytes / elapsed:.1f} MB/s)"
        )
        print(f"  {response.text[:300]}")
        if response.status_code == 201:
            file_id = response.json()["id"]
            download = await client.get(
                f"{base}/files/{file_id}/download", headers=headers
            )
            print(
                f"  download: {download.status_code}, {len(download.content) / 1e6:.1f} MB"
            )
    finally:
        path.unlink(missing_ok=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Load test for rootfolder-mock")
    parser.add_argument(
        "--base", required=True, help="API base, e.g. https://rootfolder.app/api"
    )
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--seconds", type=float, default=15, help="per scenario and level"
    )
    parser.add_argument("--levels", default="1,5,10,25,50", help="concurrent workers")
    parser.add_argument(
        "--scenarios",
        default="health,ready,items,search,similar",
        help="comma separated",
    )
    parser.add_argument(
        "--streams", type=int, default=0, help="concurrent streams to open"
    )
    parser.add_argument(
        "--upload-mb", type=int, default=0, help="one upload of this size"
    )
    args = parser.parse_args()

    base = args.base.rstrip("/")
    password = os.environ.get("LOADTEST_PASSWORD") or getpass.getpass("Password: ")
    levels = [int(level) for level in args.levels.split(",")]

    limits = httpx.Limits(max_connections=max(levels + [args.streams, 10]) + 10)
    async with httpx.AsyncClient(timeout=60, limits=limits, verify=True) as client:
        headers = await login(client, base, args.email, password)

        urls = {
            "health": "/health/live",
            "ready": "/health/ready",
            "items": "/items",
            "search": SEARCH,
        }
        items = await client.get(f"{base}/items", headers=headers)
        items.raise_for_status()
        if items.json():
            urls["similar"] = f"/items/{items.json()[0]['id']}/similar"
        else:
            print("No items in this org: run seed.py. Skipping the similar scenario.")

        print(f"Target {base}, {args.seconds:.0f} s per scenario and level\n")
        print(HEADER)
        total_bad = 0
        for scenario in args.scenarios.split(","):
            if scenario not in urls:
                continue
            for workers in levels:
                statuses, latencies, elapsed = await hammer(
                    client,
                    headers,
                    "GET",
                    f"{base}{urls[scenario]}",
                    args.seconds,
                    workers,
                )
                print(row(scenario, workers, statuses, latencies, elapsed), flush=True)
                total_bad += sum(
                    n
                    for key, n in statuses.items()
                    if key != 429 and not (isinstance(key, int) and 200 <= key < 300)
                )

        if args.streams:
            await run_streams(client, headers, base, args.streams)
        if args.upload_mb:
            await run_upload(client, headers, base, args.upload_mb)

        health = await client.get(f"{base}/health/ready")
        print(
            f"\nAfter the test: /health/ready answers {health.status_code} {health.text}"
        )
        print(f"Responses that were neither 2xx nor 429: {total_bad}")
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
