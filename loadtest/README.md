# Load, stress and hostile-input tests

Two scripts. Both refuse to run against anything but this app's hosts.

| Script | Question | Runs against |
|---|---|---|
| `load.py` | How much does it take, where does it break, does it recover? | the server (straight at the API) **and** a laptop (through nginx) |
| `attack.py` | Does anything malformed or hostile get a 5xx, a leak, or through? | a **local** copy of the stack only (it refuses a real certificate) |

The pass bar, for both: **no 5xx the app did not deliberately turn into a 503, no
internal detail in any response, and `/health/ready` answers 200 afterwards.**
Through nginx, a 429 at high concurrency is the rate limiter working, reported
apart from failures.

The password is read from `LOADTEST_PASSWORD` or asked in the terminal; it never
goes on the command line. `load.py` refuses an admin account (use a throwaway
member, or pass `--allow-admin`).

---

## Before — a throwaway account

On the server, from `/opt/rootfolder`:

```bash
docker compose run --rm api python create_user.py loadtest@rootfolder.app "Load Test" --org Rootfolder
```

The org needs the demo data (`seed.py`) for the search and similar scenarios.
Don't run while a colleague is testing. The scripts never touch `/checks/email`
(Resend has a daily quota).

---

## load.py — load and stress

Modes: `smoke` `ramp` `spike` `soak` `streams` `uploads` `suite`. Every mode writes
a JSON + Markdown report to `--out`.

### On the server, straight at the API (the app's own capacity)

Copy the script up:

```bash
scp loadtest/load.py alex@2.31.2.210:~/
```

```bash
sudo install -D -o deploy -g deploy -m 644 ~/load.py /opt/rootfolder/loadtest/load.py && rm ~/load.py
```

In a second SSH window, watch the machine:

```bash
watch -n 2 'docker stats --no-stream; echo; uptime; df -h /'
```

In the first window — the full suite (smoke, ramp to the breaking point, spike,
streams, uploads, then smoke again), inside the compose network so there is no
nginx and no internet in the way:

```bash
cd /opt/rootfolder
```

```bash
docker compose run --rm --no-deps -e LOADTEST_PASSWORD -v ./loadtest:/loadtest:ro -v ./loadtest/reports:/reports api python /loadtest/load.py suite --api http://api:8000 --web http://web:3000 --email loadtest@rootfolder.app --yes --out /reports
```

Heavier ramp on its own, to find the knee:

```bash
docker compose run --rm --no-deps -e LOADTEST_PASSWORD -v ./loadtest:/loadtest:ro -v ./loadtest/reports:/reports api python /loadtest/load.py ramp --api http://api:8000 --email loadtest@rootfolder.app --yes --levels 25,50,100,200,400,800 --step-seconds 20 --procs 3 --out /reports
```

A soak, to see drift over time:

```bash
docker compose run --rm --no-deps -e LOADTEST_PASSWORD -v ./loadtest:/loadtest:ro -v ./loadtest/reports:/reports api python /loadtest/load.py soak --api http://api:8000 --email loadtest@rootfolder.app --yes --concurrency 50 --minutes 20 --out /reports
```

`LOADTEST_PASSWORD` must be set in the shell first (`export LOADTEST_PASSWORD=…`,
or the script asks). `--procs` is worker processes; use about the server's core
count.

### From a laptop, through nginx (what a user sees, with the limits)

PowerShell, in the repo folder:

```bash
backend\.venv\Scripts\python.exe loadtest\load.py ramp --api https://rootfolder.app/api --web https://rootfolder.app --email loadtest@rootfolder.app --levels 10,50,200,500 --max-failure-rate 0.5 --out loadtest\reports
```

429s appear as concurrency rises — the per-IP and per-session limiters. The bar is
still no 5xx and readiness 200 afterwards. A single `smoke` is the quickest check
after a deploy:

```bash
backend\.venv\Scripts\python.exe loadtest\load.py smoke --api https://rootfolder.app/api --web https://rootfolder.app --email loadtest@rootfolder.app
```

### Reading the report

`req/s` and `ok/s` are throughput; `p50/p95/p99/max` latency in ms; `429` the
rate-limited count; `failures` the 5xx/timeout/connection rate. The **breaking
point** is where failures or p95 cross the limits (`--max-failure-rate`,
`--max-p95-ms`). The report also states the **recovery** time and whether the
watchdog *would* have restarted the API (three failed liveness probes in a row). One
uvicorn process uses one core, so `req/s` stops growing while the box looks idle —
that is the single process, not the hardware.

---

## attack.py — hostile input (local only)

159 requests through nginx: malformed/wrong-type bodies, injection-shaped search
and ids, path traversal, forged and oversized cookies, spoofed `X-Forwarded-*`,
request smuggling and raw-socket protocol abuse, hostile upload names/shapes,
Next.js probes (middleware-bypass CVE, image optimizer), header and timing checks,
the login limits, a flood and slowloris sockets.

It refuses any target whose TLS certificate is not self-signed, so it only ever
runs against a local stack. Bring one up (the `deploy/` stack with the real nginx
config and a self-signed cert), create a member account on it, then:

```bash
backend\.venv\Scripts\python.exe loadtest\attack.py --base https://rootfolder.app --connect-to 127.0.0.1 --email loadtest@local.test --out loadtest\reports\attack.json
```

It uses part of the 15-minute login-failure budget for its IP, so leave 15 minutes
between runs, or clear `audit_log` on the local stack. Every one of the 159 checks
must pass; a `FAIL` is either a real finding or a case the app now handles
differently — read it before dismissing it.

---

## After — remove what the test left

```bash
docker compose run --rm api python delete_files.py --uploaded-by loadtest@rootfolder.app
```

```bash
docker compose run --rm api python delete_user.py loadtest@rootfolder.app
```

`delete_files.py` removes the rows and the PDFs on disk; `delete_user.py` removes
the account (its files, if any remain, stay with the org). Reports under
`loadtest/reports/` are yours to keep or delete.
