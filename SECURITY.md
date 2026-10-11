# Security and resilience review — rootfolder-mock

This is the write-up the testing produced: what was checked, what was found and
fixed, what is still open, and how to run the same tests again (against this mock,
and later against the real rootfolder app, which runs on the same server behind the
same nginx and pipeline).

It is organised as:

1. [Summary and score](#1-summary-and-score)
2. [How it was tested](#2-how-it-was-tested)
3. [Findings](#3-findings) — each with severity, status and the fix
4. [The advanced test plan](#4-the-advanced-test-plan)
5. [Hardening by area](#5-hardening-by-area) — reverse proxy, pipeline, migrations,
   how the parts are tied together and encrypted, and not losing data
6. [Open items](#6-open-items) — do these next
7. [Re-running against the real app](#7-re-running-against-the-real-app)

The methodology follows the red-team checklist in the firebase-security-rules
skill, applied to this stack rather than to Firestore rules: look for the update
that becomes a privilege escalation, the authority taken from user-supplied data,
the missing size and type limits, the control that can be bypassed by a sequence of
requests, and the resource-exhaustion path. The score uses the same 1–5 scale.

---

## 1. Summary and score

```json
{
  "score": 4,
  "summary": "After this round the code-level posture is strong: strict per-request org scoping (another org's row is a 404), argon2id with a uniform-time path for unknown emails, database-backed session cookies, a least-privilege database role that cannot drop/alter/truncate or run programs, origin checks on every state change, a per-request nonce CSP, and input that is bounded and type-checked. The red-team suite (159 hostile requests through nginx) finds no injection, no path traversal, no SSRF, no header/validation leak, and no 5xx; the load suite drives the app past its breaking point and it recovers by itself every time, with the watchdog never misfiring. It is a 4, not a 5, because the remaining risks are operational rather than code: a single server with no off-site immutable backup yet, a deploy key and docker group that are effectively root on the host, and — carried over to the real app — a super-admin who can delete an organisation's documents with no undo. Those are in section 6.",
  "findings": [
    {"check": "Resource exhaustion / DoS", "severity": "major", "issue": "An unauthenticated client could make the server spool a 510 MB upload to disk before the 401; a burst of normal traffic deadlocked the request path; a runaway or slow query could hang the pool and return 500s.", "recommendation": "Refuse anonymous uploads at nginx and before the body in the app; admission control + a connection pool sized to the thread pool; 503 + Retry-After instead of 500; done this round."},
    {"check": "The update bypass / authority source", "severity": "none", "issue": "Role and org are never taken from the request body; there is no self-service update of role or org; every row is fetched through in_org so another org's id is a 404.", "recommendation": "No change needed; covered by tests."},
    {"check": "Type safety / validation bypass", "severity": "moderate", "issue": "A number JSON cannot represent (1e999) and an over-long password were echoed by FastAPI's default validation error, one of them turning into an unauthenticated 500.", "recommendation": "A validation handler that returns the location and type of the error but never the submitted value; done."},
    {"check": "Path traversal / SSRF", "severity": "moderate", "issue": "Encoded traversal in a path (%2e%2e%2f) made nginx answer 500; Next's image optimizer would fetch a same-origin URL and 500 on a non-image.", "recommendation": "Reject encoded slash/dot/backslash in the path at nginx (clean 400); block /_next/image at nginx since the app has no images; done."},
    {"check": "Account lockout as a DoS", "severity": "moderate", "issue": "The old per-email login limit let anyone lock a known colleague out by failing logins from anywhere.", "recommendation": "Limit per email+IP (10) so another address still works, per IP (50), and per email across all IPs (100) only as a backstop; done."},
    {"check": "Supply chain", "severity": "moderate", "issue": "Base images were referenced by tag, so a republished tag could change what shipped; the runtime images carried pip/npm/yarn.", "recommendation": "Pin every base image by digest; strip the package managers; Dependabot + CI scans (gitleaks, pip-audit, npm audit, trivy); done."}
  ]
}
```

---

## 2. How it was tested

Two scripts, in `loadtest/`. Both refuse to point at anything but this app's hosts,
and `attack.py` additionally refuses any target whose TLS certificate is not
self-signed, so it can only ever run against a local stack.

- **`load.py`** — load and stress, no hostile input. Modes: `smoke` (every feature
  once, each answer checked), `ramp` (concurrency up to the breaking point, then a
  recovery check), `spike` (calm → burst → calm), `soak` (steady for minutes),
  `streams`, `uploads`, and `suite` (all of them). A liveness probe runs the whole
  time and reports whether the watchdog *would* have restarted the API.
- **`attack.py`** — 159 hostile requests through nginx: malformed and wrong-type
  bodies, injection-shaped search and ids, path traversal, forged and oversized
  cookies, spoofed `X-Forwarded-*`, request smuggling and other raw-socket protocol
  abuse, hostile upload names and shapes, Next.js-specific probes (the middleware
  bypass CVE, the image optimizer), header and timing checks, the login limits, a
  flood, and slow-client (slowloris) sockets. Every answer must be a clean 4xx or a
  normal response — never a 5xx, never a stack trace or an internal detail — and the
  server must be healthy at the end.

This mock was taken through all of it locally (16-core Docker VM, a copy of the
server stack behind the real nginx config with a self-signed certificate). The
commands are in [`loadtest/README.md`](loadtest/README.md).

Results that back the score:

- **Hostile suite: 159 checks, 0 failed**, across bodies, queries, paths, sessions,
  uploads, protocol, Next.js, headers, slow clients, limits and logins; readiness
  and the session both fine afterwards.
- **Ramp, straight at the API** (no nginx): steady to ~126 req/s; before the
  deadlock fix the app jammed at 100 concurrent with 34 % failures, after it, it
  served 800 concurrent (p95 climbs, as expected for one uvicorn process) with
  ~0 % failures and recovered to 11 ms in 0.1 s.
- **Spike 10 → 1000 → 10**: no 5xx that the admission layer did not turn into a
  503; p95 back to baseline within seconds of the burst ending; the liveness probe
  never saw three bad checks in a row, so the watchdog would not have restarted a
  busy-but-healthy API.
- **Ramp through nginx**: 429s appear at 200 and 500 concurrent — the per-IP and
  per-session limiters doing their job — with still no 5xx.
- **300 concurrent streams** all completed and arrived in pieces (no proxy
  buffering). **6 × 50 MB and 1 × 400 MB uploads** came back byte-identical; the
  in-flight upload spools to the storage volume, not the disk Postgres lives on.

---

## 3. Findings

Severity is the skill's scale. Status is **fixed** (this round), **in place**
(already true and confirmed by a test), or **open** (section 6).

| # | Severity | Finding | Status | Fix / where |
|---|---|---|---|---|
| 1 | Major | **Anonymous upload read in full before the 401.** `file: UploadFile` in the signature made FastAPI spool the whole multipart body (up to 510 MB) before any dependency ran, so an unauthenticated client could fill the disk and only then be refused. | **fixed** | Body is read after the session check, inside the route (`backend/app/routers/files.py`); nginx also refuses an upload with no session cookie at the door (`deploy/nginx/app/default.conf.template`). Tests: `test_an_anonymous_upload_is_refused_before_its_body_is_read`, and `attack.py` uploads. |
| 2 | Major | **Request-path deadlock under load.** Reading the session held a pooled DB connection across a thread-pool hop to the route; a burst made every thread wait for a connection held by a request waiting for a thread. The ramp jammed at 100 concurrent. | **fixed** | `read_session` now always ends its transaction before returning (`backend/app/services/auth.py`); pool sized to the thread pool (40); **admission control** caps in-flight requests and queues the rest with a 503 after a wait (`backend/app/admission.py`). Tests: `test_overload.py`. |
| 3 | Major | **A full pool / slow query / DB outage returned 500.** Under a burst this flooded the log with tracebacks and looked like the app breaking. | **fixed** | `OperationalError`/pool-timeout handler → **503 + Retry-After**, one log line, never the query parameters (`backend/app/main.py`); `statement_timeout` 30 s; liveness probe made `async` so it answers while the app is full. |
| 4 | Moderate | **Validation errors echoed the submitted value;** `1e999` (JSON infinity) became an unauthenticated 500. | **fixed** | `RequestValidationError` handler returns only the field location and error type, never the value (`backend/app/main.py`). Tests: `test_a_number_json_cannot_express_is_a_422_not_a_500`, `test_validation_errors_never_echo_what_was_sent`. |
| 5 | Moderate | **nginx 500 on encoded path traversal** (`%2e%2e%2f`, `..%2f`). The rewrite + variable upstream choked; the request never reached the app, but a 500 is a poor, noisy answer. | **fixed** | A server-level `if` rejects `%2e`/`%2f`/`%5c` in the path (not the query string) with a clean 400 before any rewrite runs. `attack.py` encoded-traversal checks. |
| 6 | Moderate | **Next.js image optimizer SSRF/500.** `/_next/image?url=…` refused remote and other-host URLs but still fetched a same-origin path, returning 500 on a non-image. The app has no images. | **fixed** | `/_next/image` blocked at nginx (404); `images.unoptimized` set in `next.config.ts`. `attack.py` next.js checks. |
| 7 | Moderate | **Account lockout as a DoS.** The per-email login limit let anyone lock a known colleague out for 15 minutes by failing logins from any address. | **fixed** | Limit is per **email+IP** (10), so the colleague still logs in from another address; per **IP** (50); per **email across all IPs** (100) only as a backstop (`backend/app/routers/auth.py`). Tests in `test_auth.py`. |
| 8 | Moderate | **Supply chain.** Base images by tag (a republished tag changes what ships); pip/npm/yarn present in the runtime images; no dependency or secret scanning in CI. | **fixed** | Every base image pinned by digest; package managers stripped; `.npmrc ignore-scripts=true`; CI runs gitleaks (full history), pip-audit, `npm audit --omit=dev`; the deploy workflow scans the images with trivy (report-only) and refuses any commit not on `main`; Dependabot proposes updates. Trivy now reports **0 fixable HIGH/CRITICAL**. |
| 9 | Minor | **Unbounded upload could fill the disk** the database lives on (temp spool) or the storage volume. | **fixed** | `TMPDIR` points at the storage volume so spooling never touches the DB disk; uploads get **507** when free space drops below one max upload plus a 2 GB margin (`backend/app/storage.py`, `config.py`). Test: `test_uploads_pause_when_the_storage_is_almost_full`. |
| 10 | Minor | **Container logs grew without bound** (a line per request). | **fixed** | json-file logging capped at 5 × 20 MB per container (`deploy/compose.yaml`). |
| 11 | Minor | **A migration could block the whole site.** A schema change waits for queries on the table, and every later query waits behind it. | **fixed** | Migrations use `lock_timeout` (5 s) and retry; if the table stays busy the deploy stops with the running release untouched (`backend/alembic/env.py`, `deploy/scripts/migrate.py`). Postgres also ends idle-in-transaction sessions after 60 s. Test: `test_a_migration_kept_waiting_for_a_table_gives_up_and_changes_nothing`. |
| — | In place | **Org isolation.** Every row is fetched through `in_org`; another org's id is a 404, not a 403 (no existence leak). | in place | `test_robustness.py` access matrix, `test_isolation.py`. |
| — | In place | **No authority from the request body.** Role and org come from the session's user; there is no endpoint to change one's own role or org. | in place | — |
| — | In place | **Login is uniform-time and quiet.** argon2id, a dummy hash for unknown emails, one 401 for every failure, blocked attempts not written to the audit log. | in place | `test_auth.py`, `attack.py` timing check. |
| — | In place | **SQL injection.** All queries are parameterised through SQLAlchemy; full-text search uses `websearch_to_tsquery`. Injection-shaped input returns 200 with no rows or 422. | in place | `attack.py` queries (38 checks). |
| — | In place | **Least-privilege DB role.** The app connects as a role that can only read/write rows — no DDL, no `alembic_version`, no `COPY … PROGRAM`, no `pg_read_file`, not a superuser. | in place | `test_db_privileges.py`. |
| — | In place | **Upload content type.** Only `.pdf` names; downloads are always `application/pdf` + `Content-Disposition: attachment` + `nosniff`, so an HTML payload in a `.pdf` is never rendered. | in place | `attack.py` uploads, `test_files.py`. |
| — | In place | **Security headers, HSTS, nonce CSP, no `X-Powered-By`, no version in `Server`.** | in place | `attack.py` headers (13 checks). |
| — | In place | **Spoofed `X-Forwarded-For/-Proto` ignored;** uvicorn trusts them only from the nginx subnet. | in place | `attack.py` sessions. |
| — | In place | **Request smuggling / protocol abuse** (CL+TE, double CL, bad versions, huge headers, NUL in headers) refused by nginx with no app 5xx. | in place | `attack.py` protocol (15 checks). |

---

## 4. The advanced test plan

The plan is the two scripts plus the backend suite. Run them in this order; the
first two run anywhere, the third is for the server.

### A. Code, in CI and locally — `backend/tests`

170 tests. They cover the access matrix (anonymous / member / admin / other-org
admin), the login limits and timing, session idle and absolute expiry, org
isolation on items/similar/files, Romanian full-text search and the similarity
order, upload under/over the cap and the anonymous-upload refusal, the storage-full
507, the overload 503 and the deadlock regression, the migration lock-timeout, the
least-privilege role, and the startup refusals. `uv run pytest` — part of CI, and a
deploy will not start unless they pass.

### B. Hostile input, locally — `loadtest/attack.py`

Against a local copy of the server stack (same nginx config, self-signed cert). 159
requests; the pass bar is **no 5xx, no leaked internals, healthy at the end**. This
is the one to extend whenever a new route or parser is added. It uses part of the
login-failure budget for its IP, so leave 15 minutes between runs or clear
`audit_log` on the local stack.

### C. Load and stress — `loadtest/load.py`

- **On the server, straight at the API** (`--api http://api:8000`, inside the
  compose network): the app's own capacity, the pool, Postgres. Run `ramp` to find
  the breaking point and confirm it recovers; `spike` for burst behaviour; `soak`
  for drift; `streams` and `uploads` for the long-lived paths.
- **From a laptop, through nginx** (`--api https://rootfolder.app/api`): what a user
  sees, including the rate limits. Expect 429 at high concurrency from one IP — that
  is the limiter, not a failure; the bar is still no 5xx and `/health/ready` 200
  afterwards.

The pass bar for every mode: **no 5xx the admission layer did not turn into a 503,
and the server returns to normal by itself** (the report states the recovery time
and whether the watchdog would have fired).

---

## 5. Hardening by area

### Reverse proxy (nginx)

Already in place and confirmed: unknown host names and the bare IP get nothing (444
on :80, `ssl_reject_handshake` on :443); per-IP (100 r/s), per-session (20 r/s),
login (2 r/s), stream (10/min) and upload (12/min) rate zones, plus a connection cap
per IP; slow-client timeouts (slowloris is cut off); `X-Forwarded-For` overwritten,
never appended; `ssl_session_tickets off`; dotfiles 404; server tokens off;
per-request DNS so a deploy's new container addresses are picked up.

Added this round: **encoded traversal (`%2e`/`%2f`/`%5c`) in a path → 400**;
**`/_next/image` blocked** (the app has no images); an **anonymous upload refused at
the door** before its body crosses to the API; **container log rotation**.

### Pipeline (GitHub Actions)

- **Deploy refuses any commit not on `main`** (`git merge-base --is-ancestor`), and
  runs the full CI — tests, lint, build, **and the security job** — on that exact
  commit before building. A deploy therefore cannot ship code that has not passed.
- **Security job**: gitleaks over the whole git history (a secret committed once and
  removed is still caught); pip-audit on the exported, hashed backend lockfile; `npm
  audit --omit=dev`. These **block**.
- **Image scan**: trivy on the built images in the deploy workflow, report-only (a
  base-image CVE should not block a release; the fix is the next Dependabot update,
  and our own dependencies already block in the security job).
- Every action pinned by commit SHA; every base image pinned by digest; **Dependabot**
  proposes action, Python, npm and image updates weekly (security fixes sooner), with
  a few days' cooldown so a hijacked release is usually pulled before it is proposed.
- The SSH deploy key is locked to one script via `command=…,restrict` in
  `authorized_keys`; the key the runner holds cannot do anything else on the server.

### Migrations

Expand/contract only (a migration never drops or rewrites what the running release
uses), so the previous release keeps working on the new schema and a rollback is a
redeploy of the old image with the database left as it is. `migrate.py` skips when
the database is ahead of the image (an older release), so a rollback does not fail.
Migrations run as the database **owner**, in the one-off `migrate` service; the app
never holds those credentials. A migration that cannot take its lock within 5 s
rolls back and retries, and the deploy stops cleanly rather than hang the site. A
dump is taken before every deploy.

### How the parts are tied, and encrypted

- **Browser ↔ web/API**: one origin (nginx), so the session cookie travels without
  CORS; the cookie is `HttpOnly`, `Secure`, `SameSite=Lax`; TLS 1.2/1.3 only; HSTS;
  a per-request nonce CSP with no `unsafe-eval`. State-changing requests are checked
  against the allowed origins (`CheckOrigin`) — a foreign or `null` origin is 403.
- **Web (Next server) ↔ API**: over the internal Docker network only; the API is not
  published to the host.
- **API ↔ Postgres**: the app connects as the least-privilege role (rows only). The
  connection string lives only in the API container's environment, not in the image.
  Passwords are `scram-sha-256`.
- **Secrets at rest**: provider API keys are encrypted with a Fernet master key
  (`API_KEYS_ENCRYPTION_KEY`), stored as ciphertext; the key is in the environment,
  never in the database or the image. The master key is required at startup if any
  key rows exist, so a misconfigured deploy fails fast instead of silently.
- **Passwords**: argon2id, never reversible, never logged.
- The `.env` on the server is `640 deploy:deploy`.

### Not losing data

- A `pg_dump` before **every** deploy (the last 3 kept next to the stack).
- A **nightly dump** to the Hetzner Volume — a different disk that survives the
  server being rebuilt — kept 14 days, and **each dump is verified readable**
  (`pg_restore --list`) before older ones are removed: a dump that cannot be
  restored is not counted as a backup.
- Uploaded PDFs live on the mounted volume; the deploy refuses to start if that
  volume is not mounted, so a PDF never silently lands on the ephemeral disk.
- The database is never rolled back by a deploy (migrations only add).

This is the first line, not the whole answer — see the next section.

---

## 6. Open items

Do these next. None is an exploitable code bug; they are the operational and
defence-in-depth gaps behind the score being 4 rather than 5. The ones the user
asked to defer (external services, a WAF, etc.) are noted as such.

| Item | Why it matters | Suggested action |
|---|---|---|
| **Off-site, immutable backup** | The nightly dump is on a Volume in the *same data centre* as the server, and the PDFs on that Volume have no second copy. A compromise with host access, or a data-centre loss, takes both. | Backblaze B2 (or S3) with **Object Lock** (write-once), a weekly restore drill. Deferred by the user to "later" but this is the single biggest gap. |
| **Super-admin can delete an org's documents with no undo** | The real app (not this mock) has a super-admin; a bug or a hostile operator could delete everything, exactly the morning-after scenario. | On the real app: soft-delete (a `deleted_at`, a trash window) before hard delete; require a second confirmation for bulk/destructive admin actions; keep the Object-Lock backups above so even a hard delete is recoverable. |
| **The deploy key and the docker group are root-equivalent** | Anyone in the `docker` group, or holding the deploy key, can effectively become root on the host (mount the filesystem in a container). | Keep the deploy key's `command=…,restrict` lock (done); minimise who is in `docker`; consider rootless Docker or a dedicated deploy user later. |
| **Monitoring / alerting** | The watchdog restarts a hung container and logs to the journal, but nobody is paged if the site is actually down. | UptimeRobot (or similar) on `/health/ready`, plus a log/metric alert. Deferred by the user to "later". |
| **Postgres major-version upgrades** | pgvector pg17 is pinned; a major upgrade needs a dump+restore, not a new image (Dependabot is told not to propose it). | A documented dump/restore procedure before pg18. |
| **Rate limits vs. office NAT** | A whole client office behind one IP shares the per-IP budget; the per-session budget mitigates this, but a large office could still see 429s. | Watch the nginx 429 rate once real clients are on; raise the per-IP zone if needed. |
| **Dev-only lint advisories** | 5 high npm advisories remain in the ESLint chain (`braces`→`@next/eslint-plugin-next`); they are dev-only and never ship (the runtime `npm audit --omit=dev` is clean). | No fix available without downgrading the lint config; tracked by Dependabot. |
| **Branch protection** | CI informs but does not block on `main` (branch protection needs a paid plan for private repos); the deploy gate compensates by refusing non-`main` and re-running CI. | Enable branch protection if the repo moves to a plan that includes it. |
| **IPv6** | The server has IPv6 but the app is reached over IPv4; nginx `resolver` is `ipv6=off`. | Add the AAAA path and firewall rules when wanted. Deferred. |
| **A WAF / external DDoS protection** | Application-level limits handle bursts and abuse; a volumetric DDoS is above the app. | Cloudflare in front, later. Explicitly deferred by the user ("external apps … in the future"). |

---

## 7. Re-running against the real app

The server, nginx, pipeline, migrations, backups and watchdog are the same for the
real rootfolder app; only the application code differs. So:

1. `attack.py` and `load.py` run unchanged against the real app — the same host
   allow-list already includes `rootfolder.app`. Create a throwaway **member**
   account first (the scripts refuse an admin), and `delete_files.py --uploaded-by`
   it, then `delete_user.py`, afterwards.
2. The hostile suite's pass bar is the same: no 5xx, no leaked internals, healthy at
   the end. Any route or parser the real app has that the mock does not **must** get
   its own cases added to `attack.py` (that is the point of keeping it a script).
3. The real app's **super-admin and document-deletion** paths are the highest-value
   thing to test and harden — see the open item above. The mock has no super-admin,
   so this is new ground on the real app.
4. Load-test straight at the API on the server to find the real app's own breaking
   point (its queries are heavier than the mock's), and confirm it recovers.

Run the full plan once before the real app's first production deploy, and again
after any change to auth, uploads, the database schema, or nginx.
