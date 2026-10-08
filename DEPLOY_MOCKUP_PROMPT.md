# Deploy mock-up: prompt for a new Claude Code session

A small app with Delegate's technical shape (Next.js, FastAPI on uvicorn,
Postgres with pgvector, session login, uploads, streaming, outbound API calls)
and none of its product (no RAG, no chat). It exists so the deploy in
`docs/deploy/STEPS.md` can be practised end to end before the real app goes
through it.

**How to use:** create an empty folder, for example
`C:\Users\AlexVisoiu\Delegate\delegate-mock`, open Claude Code in it, and paste
everything below the line.

---

You are building **delegate-mock**, a deliberately small web app used to test a
production deployment pipeline. It must reproduce every *technical* trait of an
existing product, Delegate, that matters for deployment, and nothing of its
product logic. Correctness, clarity and fidelity to Delegate's patterns matter
more than features or looks.

## 1. Context

Delegate is a B2B SaaS: a Next.js 16 frontend, a FastAPI backend on uvicorn, and
PostgreSQL 17 with pgvector. Its deploy target is two Hetzner servers running
Docker Compose behind nginx, with images built by GitHub Actions. Before
deploying Delegate, the DevOps owner will deploy this mock through the same
pipeline to prove every piece works: TLS, the `/api` proxy, cookies, real client
IPs, large uploads, streamed responses, migrations, background jobs across
restarts, outbound HTTPS, email, secrets, logs and health checks.

**Reference implementation, read-only:**
`C:\Users\AlexVisoiu\Delegate\Delegate_WebApp`. Read these files before writing
code and copy their patterns, simplified. Never modify anything in that repo.

| Pattern | Files |
|---|---|
| Settings from `.env` | `backend/app/config.py` |
| Engine and sessions | `backend/app/db.py` |
| App, lifespan loops, startup checks, `/health` | `backend/app/main.py` |
| Cross-site write check (raw ASGI) | `backend/app/origin.py` |
| Security headers, SSE headers (raw ASGI) | `backend/app/headers.py` |
| Login, logout, me, rate limits from the audit log | `backend/app/routers/auth.py`, `backend/app/services/auth.py`, `backend/app/services/audit.py`, `backend/app/deps.py` |
| Org scoping (404 for other orgs' rows) | `backend/app/tenancy.py` |
| Upload streamed to disk with a cap | `backend/app/storage.py` |
| Background job and restart handling | `backend/app/services/ocr_watch.py`, `fail_interrupted` in `backend/app/services/ocr.py` |
| Encrypted provider keys (Fernet) | `backend/app/services/secrets.py` |
| Email through Resend | `backend/app/services/emails.py` |
| Streaming route (`fastapi.sse`) | `backend/app/routers/chats.py`, the stream endpoint only |
| Test setup: `<db>_test`, network ban, fakes | `backend/tests/conftest.py` |
| Interactive admin script | `backend/create_superadmin.py` |
| Next.js `/api` rewrite, headers, body size | `frontend/next.config.ts` |
| Session gate and CSP nonce | `frontend/src/proxy.ts`, `frontend/src/lib/session.ts` |
| Browser vs server base URL | `frontend/src/lib/api.ts` |
| Local tooling | `justfile`, `docker-compose.yml`, `.env.example`, `.gitattributes` |

## 2. Non-goals

- No RAG, no chat, no answer generation, no OCR. The only provider call is a key
  check that bills nothing (§4.6).
- No UI polish: no Tailwind, no component library. Plain CSS, readable.
- **No deploy files:** no Dockerfiles, no production compose, no nginx config,
  no GitHub Actions workflows, no `output: "standalone"`. The DevOps owner builds
  those himself from `docs/deploy/STEPS.md` in the Delegate repo; this app must
  work unchanged once they exist.
- No `NEXT_PUBLIC_*` variables anywhere. One frontend build must serve every
  environment.

## 3. Stack and versions

Match Delegate:
- **Backend:** Python 3.14 managed by uv (`.python-version`, `uv.lock`
  committed). FastAPI ≥ 0.139, uvicorn[standard], SQLAlchemy 2, Alembic,
  psycopg[binary] 3, pgvector, pydantic-settings, argon2-cffi, cryptography,
  httpx, python-multipart. Dev group: pytest, ruff.
- **Frontend:** Next.js 16 App Router, React 19, TypeScript, Node 22 pinned
  with Volta (`"volta": {"node": "22.23.1"}` in `package.json`). ESLint with
  `eslint-config-next`. One font through `next/font/google`, as Delegate does
  (it makes the build need internet, which the pipeline must handle).
- **Database:** `pgvector/pgvector:pg17` in a local `docker-compose.yml`.
- **Tooling:** `just` with a PowerShell shell on Windows, like Delegate's
  `justfile`.

**Local ports**, so the mock can run next to Delegate: Postgres `5434`, backend
`8010`, frontend `3010`.

## 4. Backend

### 4.1 Layout

```
backend/
  app/
    config.py  db.py  main.py  deps.py  origin.py  headers.py  logs.py
    tenancy.py  storage.py  secrets.py  emails.py
    models/     org.py  user.py  user_session.py  audit.py  item.py  file.py  api_key.py
    routers/    auth.py  items.py  files.py  stream.py  checks.py
    services/   auth.py  audit.py  jobs.py  cleanup.py
  alembic/  alembic.ini
  create_user.py  set_org_key.py  seed.py  cleanup.py
  tests/
  pyproject.toml  uv.lock  .python-version
```

### 4.2 Settings (`config.py`)

pydantic-settings, reading the repo-root `.env`; environment variables win.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | none, required | |
| `APP_ENV` | `local` | `local`, `dev`, `prod`; appears in logs and health |
| `APP_VERSION` | `dev` | set by the image build later |
| `APP_URL` | `http://localhost:3010` | public UI address; decides "local or public" |
| `CORS_ORIGINS` | `http://localhost:3010` | comma-separated |
| `SESSION_COOKIE_SECURE` | `true` | tests turn it off |
| `SESSION_IDLE_HOURS` / `SESSION_MAX_DAYS` | `8` / `30` | |
| `PASSWORD_MIN_LENGTH` | `12` | |
| `API_KEYS_ENCRYPTION_KEY` | empty | Fernet master key |
| `RESEND_API_KEY` / `EMAIL_FROM` | empty / `Mock <onboarding@resend.dev>` | |
| `STORAGE_DIR` | `storage` | relative to the repo root locally, absolute on servers |
| `MAX_UPLOAD_MB` | `500` | |
| `JOB_SECONDS` | `30` | length of the simulated processing job |
| `CLEANUP_INTERVAL_HOURS` | `24` | `0` disables the loop (tests) |
| `STREAM_SILENCE_S` | `20` | silent phase of the stream test |
| `LOG_LEVEL` | `INFO` | |

`.env.example` lists every name with a comment and no secret values.

### 4.3 Data model (Alembic, at least two migrations)

- **Migration 1:** `CREATE EXTENSION vector`; tables:
  - `orgs` (id uuid, name, created_at);
  - `users` (id, org_id, email unique, name, password_hash, role `admin` |
    `member`, is_active, created_at);
  - `user_sessions` (token hash, user_id, created_at, last_seen_at,
    expires_at);
  - `audit_log` (id, org_id nullable, user_id nullable, event, email, ip,
    user_agent, created_at), with indexes for the rate-limit queries;
  - `items` (id, org_id, title, body, `embedding vector(8)`,
    `search tsvector GENERATED ALWAYS AS (to_tsvector('romanian', title || ' ' || body)) STORED`),
    with a GIN index on `search` and an HNSW index (`vector_cosine_ops`) on
    `embedding`. This proves the prod Postgres has pgvector and the `romanian`
    text search config;
  - `files` (id, org_id, uploaded_by, filename, `stored_path` relative to
    `STORAGE_DIR`, size_bytes, status `processing` | `done` | `interrupted`,
    created_at, finished_at);
  - `api_keys` (id, org_id, provider, `ciphertext`, last_checked_at,
    last_error; unique on org and provider).
- **Migration 2:** an additive change, for example `items.pinned boolean not
  null default false`, so deploys run more than one migration and the
  expand-only rule can be practised.
- Migrations never run on app startup.

### 4.4 Middleware and app setup (`main.py`)

Order and behaviour as in Delegate:
- **`SecurityHeaders`**, outermost, raw ASGI: `nosniff`, `X-Frame-Options:
  DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy:
  same-origin`, `Cache-Control: no-store` unless set. For `text/event-stream`
  it replaces `Cache-Control` with `no-cache, no-transform` and adds
  `X-Accel-Buffering: no`.
- **CORS** with `CORS_ORIGINS`, credentials allowed.
- **`CheckOrigin`**, raw ASGI, inside CORS: a non-GET/HEAD/OPTIONS request with an
  `Origin` header not in `CORS_ORIGINS` gets 403.
- **Request logging** (`logs.py`, raw ASGI): assigns a request ID (takes
  `X-Request-ID` if present), returns it as a header, and writes one JSON line
  per request.
- `/docs`, `/redoc` and `/openapi.json` only when `APP_URL` is local.
- **Every router except `auth` requires a session** through a router-level
  dependency, as Delegate does.

**Lifespan:**
- refuse to start if `api_keys` has rows and `API_KEYS_ENCRYPTION_KEY` is empty;
- refuse to start if `APP_URL` is public and `RESEND_API_KEY` is empty;
- mark every `files` row still `processing` as `interrupted`, since this app
  assumes a single process, exactly like Delegate's `fail_interrupted`;
- start the cleanup loop (expired sessions, audit rows older than 365 days),
  every `CLEANUP_INTERVAL_HOURS`, never letting an exception escape.

### 4.5 Logging (`logs.py`)

- JSON to stdout, one object per line.
- Every line: `ts`, `level`, `logger`, `msg`, `request_id` (when inside a
  request), `version`, `env`.
- Request lines add `method`, `path`, `status`, `duration_ms`, `client_ip`.
- Never log request bodies, passwords, tokens, keys or file contents.
- The app writes its own request lines, so uvicorn runs with `--no-access-log`.

### 4.6 Routes

All JSON unless stated. Paths have **no** `/api` prefix: the proxy strips it.

| Method and path | Auth | Behaviour |
|---|---|---|
| `GET /health/live` | none | `{"status":"ok","version","env"}`; never touches the database |
| `GET /health/ready` | none | runs `select 1`; 503 if the database is down; same fields plus `"db"` |
| `POST /auth/login` | none | email, password, `remember`. argon2 verify; the same 401 and similar timing for any failure; rate limits from `audit_log`: 10 failures per email or 50 per IP in 15 minutes, then 429 without checking the password (blocked attempts are not logged). Success: an httpOnly cookie `mock_session`, `SameSite=Lax`, `Secure` from settings, `path=/`, max-age only if `remember` |
| `POST /auth/logout` | optional | 204 always; closes the session, deletes the cookie |
| `GET /auth/me` | session | user, org, role, plus `seen_ip` = `request.client.host` and `scheme` = `request.url.scheme`. These two fields show whether the proxy's forwarded headers reach the app |
| `GET /items` | session | the org's 20 newest items |
| `GET /items/search?q=` | session | full-text search with `websearch_to_tsquery('romanian', q)` on `search`, ranked |
| `GET /items/{id}/similar` | session | 5 nearest items by cosine distance on `embedding`, same org; another org's id gets 404 |
| `POST /files` | session | multipart PDF only; streamed to disk in 1 MB chunks under `STORAGE_DIR` as `<uuid>.pdf`; `stored_path` holds only that name; above `MAX_UPLOAD_MB` the partial file is removed and 413 returns a readable message; then a background job (§4.7) |
| `GET /files` | session | the org's files with status |
| `GET /files/{id}/download` | session | streams the file back; proves the storage mount |
| `POST /stream` | session | `EventSourceResponse` from `fastapi.sse`, as in Delegate: a `status` event, then `STREAM_SILENCE_S` seconds of silence, then 40 `delta` events 150 ms apart, then `done`. Exercises proxy buffering and timeouts |
| `POST /checks/provider` | session, admin | decrypts the org's Anthropic key and calls `GET https://api.anthropic.com/v1/models`, which bills no tokens; returns ok or a short reason, never the key. "No key saved" if none. Proves outbound HTTPS and the master key |
| `POST /checks/email` | session, admin | sends a test email to the current user through Resend, or logs the would-be email at WARNING when no key is set. The Resend test sender only delivers to the Resend account's own address |

### 4.7 Simulated background job (`services/jobs.py`)

- After an upload, a FastAPI `BackgroundTasks` function sleeps `JOB_SECONDS`
  and then marks the file `done`.
- With the lifespan rule above, a deploy that restarts the API mid-job leaves
  that file `interrupted`. This is the behaviour the deploy plan accepts, and the
  mock must make it visible.

### 4.8 Scripts (run from `backend/`, later inside the api container)

- **`create_user.py <email> <name> --org <name> [--admin]`:** creates the org if
  missing; asks for the password in the terminal with `getpass`, never as an
  argument.
- **`set_org_key.py --org <name> --provider anthropic`:** asks for the key with
  `getpass` and stores it Fernet-encrypted.
- **`seed.py`:** two orgs ("Delegate" and "Demo Client"), 50 items each with
  short Romanian texts about public tenders and random normalized 8-dimension
  embeddings. Idempotent.
- **`cleanup.py`:** runs the cleanup once, by hand.

## 5. Frontend

```
frontend/
  src/app/layout.tsx  page.tsx  login/page.tsx  globals.css
  src/components/     dashboard.tsx  login-form.tsx  logout-button.tsx
  src/lib/            api.ts  session.ts
  src/proxy.ts
  next.config.ts  package.json  tsconfig.json  eslint.config.mjs
```

- **`next.config.ts`:**
  - `poweredByHeader: false`;
  - the security headers Delegate sets (HSTS without `includeSubDomains`,
    `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`,
    CORP);
  - `experimental.proxyClientMaxBodySize: "510mb"`;
  - a rewrite from `/api/:path*` to `${API_INTERNAL_URL}/:path*`, for local
    development only; in production nginx does it first;
  - `generateBuildId` returns `APP_VERSION` when set at build time.
- **`src/lib/api.ts`:** in the browser the base URL is `/api`; on the server it
  is `process.env.API_INTERNAL_URL`, read at runtime, forwarding only the
  `mock_session` cookie.
- **`src/proxy.ts`**, as Delegate's:
  - public paths `/login`;
  - for other paths, asks `GET /auth/me` through `API_INTERNAL_URL`: a dead
    session redirects to `/login?next=...` and deletes the cookie, a backend
    that does not answer lets the page render its own error;
  - sets a nonce-based CSP (`'strict-dynamic'`, `unsafe-eval` and websockets
    only in development);
  - the matcher excludes `/api` and static files.
- **`/login`:** email, password, "remember me"; readable errors for 401 and 429.
- **`/`**, the dashboard:
  - **A Server Component** fetches `/auth/me` from the server side and shows the
    user, org, role, `seen_ip`, `scheme`, and the backend `version` and `env`
    from `/health/live`. The footer shows the frontend's build version.
  - **A client component** with one button per check, each showing its raw JSON
    result or error:
    - **Retrieve items:** `GET /api/items`;
    - **Search** (with a text box): `GET /api/items/search?q=`;
    - **Similar to first item:** `GET /api/items/{id}/similar`;
    - **Upload PDF:** `XMLHttpRequest` with a progress bar, then the file list
      with statuses, a refresh button and download links;
    - **Stream test:** `fetch` POST, read the body stream, append the deltas as
      they arrive, show the seconds elapsed;
    - **Check provider key** and **Send test email** (admin only);
    - **Logout.**
- A 401 from any client call sends the browser to `/login`, as Delegate does.

## 6. Tests

**Backend:** pytest against real Postgres, following Delegate's `conftest.py`:
- a `<db>_test` database recreated from the migrations each session, with a
  guard refusing names that do not end in `_test`;
- `TRUNCATE` between tests;
- a network ban that lets only the database connect;
- the Anthropic and Resend calls faked.

Cover at least:
- login success and failure, the per-email and per-IP limits, the idle and max
  session expiry, logout;
- `CheckOrigin` 403;
- org isolation on items, similar and files, with another org's id giving 404;
- full-text search in Romanian;
- the similarity order;
- upload under and over the cap, and that `stored_path` is relative;
- the startup `interrupted` marking;
- health live and ready;
- one JSON log line per request, with a request ID;
- the provider check without and with a (fake) key;
- the two startup refusals.

Ruff clean.

**Frontend:** `npm run lint`, `npx tsc --noEmit` and `npm run build` pass.

## 7. Local tooling and docs

- **`docker-compose.yml`:** Postgres only, `pgvector/pgvector:pg17`, host port
  5434, a named volume.
- **`justfile`:**
  - `up`: Postgres, `uv sync`, `npm install`, `alembic upgrade head`;
  - `api`: uvicorn with reload on `app/`, port 8010;
  - `web`: `next dev -p 3010`;
  - `test`;
  - `seed`;
  - `create-user email name org`;
  - `set-org-key org`.
- **`.gitattributes`:** LF everywhere except `.ps1` and `.cmd`.
- **`.gitignore`:** `.env`, `.venv`, `node_modules`, `.next`, `storage/`, caches.
- **`README.md`:** what the mock is for, the quick start, every environment
  variable, and a table "what each button proves in a deploy" (§9).

## 8. Conventions

- Simple and readable over clever: no calls nested several levels deep, no dicts
  inside dicts, nothing without a clear purpose.
- Comments explain *why*, not what. Docstrings on modules and non-obvious
  functions, in the style of the Delegate files you read.
- English identifiers, messages and docs.
- Blank lines inside function bodies keep the surrounding indentation where the
  format allows it; where the formatter forbids it (ruff strips whitespace on
  blank lines), follow the formatter.
- Secrets never in code, logs, test fixtures or commit messages.

## 9. What each part proves in a deploy

| Mock feature | Proves |
|---|---|
| `/login`, cookie, `/auth/me` from the server side | `/api` routing, cookie `Secure` over HTTPS, `API_INTERNAL_URL` between containers |
| `seen_ip`, `scheme` | nginx forwards the client IP and scheme; uvicorn runs with `--proxy-headers` |
| 11 failed logins | per-email rate limit in the app; nginx's own login limit |
| Retrieve, Search, Similar | migrations ran; pgvector and the `romanian` config exist in prod's Postgres |
| Upload of a 450 MB PDF, then download | nginx body size and streaming, the storage Volume mount, relative paths |
| Upload, then deploy during `JOB_SECONDS` | the restart window and the `interrupted` marking |
| Stream test | no proxy buffering, long read timeout |
| Check provider key | outbound HTTPS from the server, master key and encrypted keys in the database |
| Send test email | Resend key and sender |
| Footer and health versions | the version reaches images, health, logs and the UI |
| JSON logs with request IDs | log collection and rotation on the server |
| Two migrations | the deploy script runs migrations once, in order |
| Starting with a public `APP_URL` and no Resend key | the startup refusal is caught before prod |

## 10. How to work

1. Read the reference files in §1.
2. Present a short plan: the repo layout, the migration list, the order of work.
   Ask only what blocks you.
3. Build in this order, verifying each step before the next:
   1. backend skeleton, settings, logging, health;
   2. migrations;
   3. auth;
   4. items and seed;
   5. files and the job;
   6. stream;
   7. checks;
   8. backend tests;
   9. frontend;
   10. README.
4. `git init` in the new folder and commit after each milestone with a clear
   message. Do not create a remote or push; the owner does that.
5. **Done means:**
   - `just up` and `just seed` work;
   - `just api` and `just web` serve the app on `http://localhost:3010`;
   - every button works;
   - `just test` passes;
   - the frontend lint, typecheck and build pass;
   - the README explains all of it.

   Report what you verified and how.
