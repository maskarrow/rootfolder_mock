# delegate-mock

A small app with Delegate's technical shape and none of its product. It exists so
the deploy in Delegate's `docs/deploy/STEPS.md` can be practised end to end before
Delegate itself goes through it.

It has what matters to a deploy:

- a Next.js 16 frontend and a FastAPI backend on uvicorn;
- PostgreSQL 17 with pgvector and the `romanian` text search configuration;
- session cookies, uploads of up to 500 MB, a streamed response, a background job
  that a restart interrupts;
- outbound HTTPS, email through Resend, encrypted secrets;
- JSON logs with request IDs, and health checks.

There is no RAG, no chat and no OCR.

The repo has no Dockerfiles, compose files for servers, nginx config or CI
workflows. Those are built from `STEPS.md`, and this app must work under them
unchanged.

## Quick start (local)

Needs:

- Docker Desktop (on Windows, with WSL 2);
- [uv](https://docs.astral.sh/uv/);
- [just](https://just.systems/);
- Node 22 (pinned with [Volta](https://volta.sh/) in `frontend/package.json`).

uv installs Python 3.14 by itself.

```bash
cp .env.example .env
```

Put a master key in `API_KEYS_ENCRYPTION_KEY` in `.env`. You can generate one once
`just up` has installed the backend:

```bash
cd backend && uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Then:

```bash
just up                                             # Postgres, dependencies, migrations
just seed                                           # two orgs, 50 items each
just create-user you@example.com "Your Name" Delegate --admin   # asks the password twice
just api                                            # backend on http://localhost:8010
just web                                            # frontend on http://localhost:3010
```

Open http://localhost:3010 and sign in. The API docs are at
http://localhost:8010/docs; they exist only while `APP_URL` is local.

| Command | What it does |
|---|---|
| `just up` | Starts Postgres (port 5434) and waits until it is healthy; runs `uv sync`, `npm install` and `alembic upgrade head` |
| `just api` | uvicorn with reload on `app/`, port 8010, `--no-access-log` |
| `just web` | `next dev -p 3010` |
| `just test` | Backend tests, on a `<db>_test` database recreated each run |
| `just lint` | ruff check and format, eslint, `tsc --noEmit` |
| `just seed` | The orgs "Delegate" and "Demo Client" with 50 Romanian tender notes each. Idempotent |
| `just create-user email name org [--admin]` | Creates the org if missing. The password is typed in the terminal, never passed as an argument |
| `just set-org-key org` | Stores the org's Anthropic key, encrypted, typed in the terminal |
| `just cleanup` | The periodic cleanup, by hand: dead sessions, audit rows older than a year |

The ports differ from Delegate's (5433, 8000, 3000), so both run side by side.

## What each part proves in a deploy

| On the dashboard | Proves |
|---|---|
| Signing in, then the "Seen from the Next server" card | `/api` routing to the backend, the `Secure` cookie over HTTPS, and `API_INTERNAL_URL` between the containers. The card is rendered by the Next server, which calls the backend directly |
| "Who am I via /api": `seen_ip`, `scheme` | nginx forwards the client's address and scheme, and uvicorn trusts them (`--proxy-headers`, `--forwarded-allow-ips`). Behind a correct proxy you see your own IP and `https`. The server card shows the web container's address instead, since that call never passes nginx |
| 11 failed logins | The app's limit (10 per email, 50 per IP, from the audit log). With the IP wrong, the per-IP limit would block everyone at once. nginx's own login limit answers 429 earlier |
| Retrieve items, Search, Similar | The migrations ran (`pinned` comes from the second one); prod's Postgres has pgvector (HNSW, cosine) and the `romanian` configuration |
| Upload a 450 MB PDF, then download it | nginx's body size limit and request streaming, the storage volume mount, relative stored paths |
| Upload, then deploy within `JOB_SECONDS` | The restart window: the file ends `interrupted`, as Delegate's documents do |
| Stream test | No buffering and a long enough read timeout. The page shows when the first delta and `done` arrived; if they arrive together, something on the way buffers |
| Check provider key (admin) | Outbound HTTPS from the server, the master key, and the encrypted key in the database. Lists Anthropic's models, which bills nothing |
| Send test email (admin) | The Resend key and sender |
| Footer and `/health/*` versions | `APP_VERSION` reaches both images, health, logs and the UI |
| JSON logs with request IDs | Log collection and rotation on the server |
| Two migrations | The deploy script runs migrations once, in order |
| Starting with a public `APP_URL` and no Resend key | The startup refusal is caught before prod |

Each card shows the raw JSON answer or the error. If an error body is not JSON
(an nginx error page, say), the card says so and quotes the start of it.

## What the pipeline must provide

The app makes these assumptions; each one fails in a recognizable way if missed.

- **uvicorn behind nginx:**

  ```
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=<nginx network> --no-access-log --timeout-graceful-shutdown 30
  ```

  Run it from `backend/`. The app writes its own request lines, so uvicorn's
  access log stays off.
- **`CORS_ORIGINS` includes the public origin** (for example
  `https://mock.example.com`). Otherwise every POST, login included, gets
  `403 Cross-site request refused`.
- **A public `APP_URL` needs `RESEND_API_KEY`**, or the backend refuses to start.
  A public `APP_URL` also hides `/docs`.
- **`API_KEYS_ENCRYPTION_KEY` stays the same** for the life of the database. With
  keys saved and the master key missing, the backend refuses to start.
- **`STORAGE_DIR` is an absolute path on a mounted volume.** Rows hold only
  `<uuid>.pdf`. If a download says "recorded but missing from STORAGE_DIR", the
  volume is not the one the file was written to.
- **Temp space in the api container.** Starlette writes a multipart upload to the
  system temp directory before the route copies it into `STORAGE_DIR`. A 500 MB
  upload needs 500 MB free there.
- **nginx:**
  - `client_max_body_size` above 500 MB (STEPS.md says 510m), so the backend is
    the one that answers 413, with a readable message;
  - `proxy_request_buffering off` on `/api/files`;
  - `proxy_read_timeout` above the longest silence. FastAPI writes a `: ping`
    comment after 15 s of silence, so the stream never goes more than 15 s
    without bytes;
  - the `X-Accel-Buffering: no` header, which the backend sets on the stream,
    must not be stripped.
- **Migrations run once, before the new containers start:**

  ```
  alembic upgrade head
  ```

  Run it from `backend/` in the api image. Startup never migrates.
- **`APP_VERSION` at build time for the frontend image**: it becomes the Next
  build ID and the footer's version. It is also needed at run time for the api
  container.
- **`API_INTERNAL_URL` at run time for the web container** (for example
  `http://api:8000`). The same image must work with any value. The `/api` rewrite
  in `next.config.ts` is for local development only: its destination is fixed at
  build time, and in production nginx sends `/api/` to the backend first.
- **`next build` needs internet**, because `next/font/google` downloads the font
  at build time.
- **To see `interrupted` after a deploy**, set `JOB_SECONDS` above the graceful
  shutdown timeout (for example 120), upload, and deploy within that window. With
  the default 30 s and a 30 s graceful timeout, the job may just finish during
  shutdown.
- **One api process.** At startup, files still `processing` are marked
  `interrupted`, as in Delegate. Next to a second process, this would mark files
  that process is still working on.
- **Scripts run in the api image, from `backend/`**, and ask for secrets on the
  terminal (`docker compose run --rm api ...`):

  ```
  uv run python create_user.py you@example.com "Your Name" --org Delegate --admin
  uv run python set_org_key.py --org Delegate --provider anthropic
  uv run python seed.py
  uv run python cleanup.py
  ```

  In an image without uv, run them with plain `python`.

## Environment variables

The backend reads the repo-root `.env` locally; environment variables win.
`.env.example` lists every name with a comment and no secrets.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | none, required | `postgresql+psycopg://user:pass@host:port/db`. Tests use the same server with `_test` appended to the database name |
| `APP_ENV` | `local` | `local`, `dev` or `prod`; appears in logs and health |
| `APP_VERSION` | `dev` | Set by the image build |
| `APP_URL` | `http://localhost:3010` | The public UI address; anything but localhost counts as public |
| `CORS_ORIGINS` | `http://localhost:3010` | Comma-separated; the origins allowed to POST |
| `SESSION_COOKIE_SECURE` | `true` | Browsers accept it on http://localhost; only tests turn it off |
| `SESSION_IDLE_HOURS` / `SESSION_MAX_DAYS` | `8` / `30` | Idle timeout; absolute limit that activity does not extend |
| `PASSWORD_MIN_LENGTH` | `12` | |
| `API_KEYS_ENCRYPTION_KEY` | empty | Fernet master key for `api_keys` |
| `RESEND_API_KEY` / `EMAIL_FROM` | empty / `Mock <onboarding@resend.dev>` | Without a key the test email is logged at WARNING. The test sender only delivers to the Resend account's own address |
| `STORAGE_DIR` | `storage` | Relative to the repo root locally, absolute on servers |
| `MAX_UPLOAD_MB` | `500` | |
| `JOB_SECONDS` | `30` | Length of the simulated job after an upload |
| `CLEANUP_INTERVAL_HOURS` | `24` | `0` disables the periodic cleanup |
| `STREAM_SILENCE_S` | `20` | The stream's silent phase |
| `LOG_LEVEL` | `INFO` | |

The frontend reads its own environment, not `.env`:

| Variable | When | Notes |
|---|---|---|
| `API_INTERNAL_URL` | run time | Where the Next server reaches the backend. Default `http://localhost:8010` |
| `APP_VERSION` | build time | The build ID and the footer's version |

There are no `NEXT_PUBLIC_*` variables: one frontend build serves every
environment.

## Logs

Every line on stdout is one JSON object with these fields: `ts`, `level`,
`logger`, `msg`, `version`, `env`, and `request_id` when written inside a request.
The backend writes one line per request, which adds `method`, `path` (without the
query string), `status`, `duration_ms` and `client_ip`. The request ID comes from
an incoming `X-Request-ID`, or is generated, and is returned in the same header.
Background jobs log with the ID of the request that started them.

Nothing logs request bodies, query strings, cookies, passwords, tokens, keys or
file contents.

## Tests

```bash
just test    # backend: pytest on real Postgres
just lint    # ruff, eslint, tsc
cd frontend && npm run build
```

The backend tests follow Delegate's `conftest.py`:

- a `<db>_test` database recreated from the migrations each run, refused under any
  other name;
- `TRUNCATE` after each test;
- a socket ban that lets only the database connect;
- Anthropic and Resend faked.

## Layout

```
backend/
  app/          config, db, main (middleware, lifespan, health), logs, origin, headers,
                deps, tenancy, storage, secrets, emails
    models/     orgs, users, user_sessions, audit_log, items, files, api_keys
    routers/    auth, items, files, stream, checks
    services/   auth, audit, jobs, cleanup
  alembic/      0001 initial schema, 0002 items.pinned
  create_user.py  set_org_key.py  seed.py  cleanup.py
  tests/
frontend/
  src/proxy.ts  src/lib/api.ts  src/lib/session.ts
  src/app/      layout, page (dashboard), login
  src/components/ dashboard, login-form, logout-button
```
