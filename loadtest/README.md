# Load test

`load.py` measures how many requests the app answers, how fast, and what breaks
first. Run it twice, because the two runs answer different questions.

| Run | From | Through | Measures |
|---|---|---|---|
| **A: the front door** | your laptop | internet, nginx, app | What a user sees, and that nginx's limits protect the app. From one IP, nginx answers 429 above 20 requests per second; that is the limiter working. |
| **B: the app itself** | the server | the compose network, no nginx | The capacity of uvicorn, the connection pool and Postgres. |

A healthy result in both: **no 5xx, no timeouts, `/health/ready` answers 200
afterwards**. In run A, 429 is expected at high concurrency.

## Before

Use a throwaway account, so your own password is never involved. On the server:

```bash
cd /opt/rootfolder
docker compose run --rm api python create_user.py loadtest@rootfolder.com "Load Test" --org Rootfolder
```

The org needs the demo data (`seed.py`) for the search and similar scenarios.

Don't run it while a colleague is testing. Don't point it at `/checks/email`: Resend
has a daily quota (the script never does).

## Run A: from the laptop

PowerShell, in the repo folder:

```bash
backend\.venv\Scripts\python.exe loadtest\load.py --base https://rootfolder.app/api --email loadtest@rootfolder.com --seconds 10 --levels 1,5,20
```

It asks for the password. Then the two heavier checks, separately:

```bash
backend\.venv\Scripts\python.exe loadtest\load.py --base https://rootfolder.app/api --email loadtest@rootfolder.com --scenarios health --levels 1 --seconds 1 --streams 5
```

```bash
backend\.venv\Scripts\python.exe loadtest\load.py --base https://rootfolder.app/api --email loadtest@rootfolder.com --scenarios health --levels 1 --seconds 1 --upload-mb 450
```

The stream limit in nginx is 10 per minute per IP (burst 5), hence only 5. The 450 MB
upload is the one the real archive needs: nginx's body limit, unbuffered streaming,
the volume, and the download of the same file afterwards.

## Run B: on the server, straight to the api

Copy the script (laptop, then server):

```bash
scp loadtest/load.py alex@2.31.2.210:~/
```

```bash
sudo install -D -o deploy -g deploy -m 644 ~/load.py /opt/rootfolder/loadtest/load.py && rm ~/load.py
```

Open a **second** SSH window and leave this running, to see what the machine does:

```bash
watch -n 2 'docker stats --no-stream; echo; uptime'
```

In the first window:

```bash
cd /opt/rootfolder
docker compose run --rm --no-deps -v ./loadtest:/loadtest:ro api python /loadtest/load.py --base http://api:8000 --email loadtest@rootfolder.com --seconds 20 --levels 1,10,25,50,100
```

And the streams, many at once (this has no nginx limit):

```bash
docker compose run --rm --no-deps -v ./loadtest:/loadtest:ro api python /loadtest/load.py --base http://api:8000 --email loadtest@rootfolder.com --scenarios health --levels 1 --seconds 1 --streams 100
```

## Reading the result

| Column | Meaning |
|---|---|
| `req/s` | Throughput at that concurrency |
| `2xx`, `429`, `bad` | Answered, refused by a limiter, failed (5xx, timeout, connection error) |
| `p50`, `p95`, `p99`, `max` | Latency in ms. Compare p95 across concurrency levels. |

- **The knee:** the concurrency where `req/s` stops growing and p95 climbs. Below it
  the app has headroom; past it requests queue.
- **`bad` above 0** is the finding. Check `docker compose logs api` for the same
  minute: a pool error (`QueuePool limit`) means too many concurrent database
  requests for the pool's size; a timeout means the queue got too long.
- **`health` against `items`:** `health` touches no database, so the gap between
  them is the cost of Postgres and the pool.

## What to expect, so a surprise is visible

These are predictions from the code, not measurements:

- uvicorn runs **one process**: Python code uses one of the four cores, so `req/s`
  stops growing early while the machine looks mostly idle. That is the single
  process, not a lack of hardware.
- SQLAlchemy's default pool is 5 connections plus 10 of overflow, and waits 30 s
  for a free one. Past about 15 simultaneous database requests they queue.
- Streams hold no connection and sleep cheaply, so 100 should all finish.

## After

Remove what the test left: the account and any uploaded file.

```bash
docker compose run --rm api python delete_user.py loadtest@rootfolder.com
```

```bash
docker compose exec db psql -U rootfolder rootfolder -c "SELECT id, stored_path FROM files WHERE filename LIKE 'loadtest-%'"
```

Then delete the rows and the files they name:

```bash
docker compose exec db psql -U rootfolder rootfolder -c "DELETE FROM files WHERE filename LIKE 'loadtest-%'"
```

```bash
sudo rm /mnt/storage/pdfs/<each stored_path from the SELECT>
```

```bash
sudo rm -r /opt/rootfolder/loadtest
```
