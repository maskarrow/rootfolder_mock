# Delegate: deployment plan

How Delegate goes from "runs on our laptops" to a production service that
clients reach at a public address: what gets built, which decisions were taken
and why, and what is still open. [STEPS.md](STEPS.md) is the checklist to follow
in order; this file explains each part in depth.

Last updated: 2026-10-07.

---

## 1. How these two files work

- **STEPS.md** is the work list, steps 0-40, in Romanian: what to do, in which
  files, how to verify. Follow it in order. Steps marked "Mock:" are done on the
  rehearsal app first (§5.17).
- **PLAN.md** (this file) holds the decisions, the reasons behind them, the specs
  the steps implement and the open questions. Each step in STEPS.md points to
  the section here that explains it.
- Both are updated at the end of every step. At the start of a session, read §2.

Working rules:
- Claude writes only these two files. It changes nothing in the application: no
  code, Dockerfiles, compose files, workflows or scripts.
- Alex Visoiu runs DevOps alone. The other two founders develop and manage their
  own branches; DevOps consumes only `main` (deployed to dev on demand) and tags
  (deployed to prod).
- Alerts and notifications go by email.
- Decisions that cost money or change who owns an account need all three
  founders.

---

## 2. Where we are

All the architecture decisions are taken (§5). Nothing of the production setup
is built or bought yet.
Steps 1-16 need neither money nor the domain and can start now: containers,
nginx and the deploy script on a laptop, the application changes, CI. The domain
`rootfolder.app` was bought on 2026-10-07; dev (step 24 onward) now waits
for the servers. Eight decisions are still open (§3). No company exists yet: accounts open in
Alex Visoiu's name.

The rehearsal app, `delegate-mock` (§5.17), is built and verified on Alex's
laptop (2026-10-03). Every infrastructure file is written and tested on it first,
and the first deploys to dev and prod use it, so stages 2, 5 and 7 do not wait
for the application changes of stage 3.

| Stage in STEPS.md | Steps | Status |
|---|---|---|
| Laptop prerequisites | 0 | done on Alex's laptop (WSL 2, Docker Desktop); vault not chosen |
| Rehearsal app | §5.17 | built and verified locally; not on GitHub yet |
| 1. The plan in the repo | 1 | todo: these two files are not committed anywhere yet |
| 2. Containers on a laptop | 2-7 | todo, on the mock first |
| 3. Application changes | 8-14 | todo |
| 4. CI | 15-16 | todo |
| 5. Servers | 17-23 | prod bought 2026-10-07 (IPv4 2.31.2.210; type and location to record), SSH key and firewall `web` done (22 open, 5432 blocked, checked); created without cloud-init, manual setup next (STEPS, Proceduri); dev later |
| 6. Dev | 24-28 | blocked: servers (stage 5) |
| 7. Deploy workflows | 29-31 | blocked: stage 6 |
| 8. Backups | 32-36 | todo; details open (§3) |
| 9. Prod and first client | 37-40 | blocked: backups, company and DPA |

---

## 3. Open decisions

1. **Domain name.** Decided: `rootfolder.app`, bought on 2026-10-07 (§5.3). Left
   to do: the trademark check for "rootfolder".
2. **Server architecture and sizes.** The CX line (x86) was sold out across EU
   locations on 2026-10-05. Two ways forward (§5.2): x86 on CPX, prod CPX32 and
   dev CPX22 (or CX23 when in stock), about €42-56/month; or ARM on CAX, prod
   CAX21 with a 100 GB Volume and dev CAX11, about €23/month, after checking that
   both images build for arm64. Money: all three founders.
3. **Backups.** Alex's intent is in §5.13. To confirm: the whole database rather
   than selected tables; the conditions for the laptop copy; up to 24 hours of
   data loss.
4. **Monitoring tools.** Proposed: UptimeRobot, Sentry in the EU region, Beszel
   (§5.14).
5. **Upload cap for chat and workspace PDFs.** 50 MB today (`MAX_UPLOAD_MB`);
   500 MB like tender uploads? (§5.12)
6. **Shared password vault.** Recovery codes, keys and `.env` values need one
   place; which tool (Bitwarden, 1Password or other) is not chosen.
7. **Company and DPA.** What a paying client's contract and data processing
   agreement require from a legal entity. A question for an accountant or a
   lawyer; it blocks real client data on prod.
8. **Automatic reboots after security updates.** Proposed: yes, at 04:30, after
   the 03:00 backup. Containers come back on their own; the cost is a minute of
   downtime on the nights a kernel update needs it. Without it, kernel fixes wait
   for a manual reboot (§5.15).
9. **Company email plan.** Zoho's setup screen offered only paid plans on
   2026-10-07; the free plan is "available only in select data centers" (§5.16).
   Proposed: try the free signup link once; if it is not offered, Mail Lite,
   one user per founder plus an `admin@` group.

---

## 4. The target architecture

```
                         Cloudflare DNS (DNS only, no proxy)
                                      │
Browser ──HTTPS 443──> nginx ──/api/*──> api: FastAPI + uvicorn, one process ──> Postgres 17 + pgvector
                         │                  │  (also OCR, batch polling,
                         └──everything else──> web: Next.js ──server-side──> api
                                            │
                                            └──> Anthropic, OpenAI, Mistral (each org's keys), Resend
```

- **Two Hetzner Cloud servers**, dev and prod, each running the same Docker
  Compose stack: `nginx`, `web`, `api`, `db`. Only nginx publishes ports.
- **PDFs** stay on disk; on prod, on a Hetzner Volume mounted at
  `/mnt/storage`.
- **Images** are built by GitHub Actions and stored in GitHub Container
  Registry (GHCR).
- **Deploys** are manual workflows: `main` to dev, a tag to prod.
- **Backups** go nightly to Backblaze B2.
- **Monitoring**: an external uptime check, error tracking, host metrics, all
  alerting by email.

---

## 5. Decisions

Each subsection gives the decision, the reason and what it forces on the rest.

### 5.1 Tenancy: one shared deployment

- **Decision.** One installation serves every client. A new client is a new org,
  created from the superadmin panel, not a new site or server.
- **Why.** The code is already multi-tenant: every row carries an `org_id`,
  lookups go through `tenancy.py`, and the superadmin panel creates, deactivates
  and reactivates orgs. One stack means one deploy, one backup, one set of
  servers.
- **Consequences.**
  - Isolation between clients rests on the code filtering by `org_id`. The
    access tests (`backend/tests/test_access.py`) must stay green.
  - Deleting a client is the existing org deletion: a grace period, then the
    daily cleanup. Their data stays in backups until those expire, which the
    client contract should say.
  - A client whose contract demands physical isolation would need its own
    stack; the compose files should stay parametrizable for that case.

### 5.2 Servers: Hetzner Cloud, dev and prod apart

- **Decision.** Two Hetzner Cloud servers, x86, in Germany (Nuremberg or
  Falkenstein, wherever there is stock), both in the same location.
  - **Prod:** CX33 (4 vCPU, 8 GB RAM, 80 GB disk) with a 100 GB Volume for PDFs
    and a primary IP that survives a rebuild. Fallback: CPX32 (4 vCPU, 8 GB,
    160 GB disk), where the Volume can wait.
  - **Dev:** CX23 (2 vCPU, 4 GB, 40 GB). Fallback: CPX22.
  - IPv4 on both: GitHub-hosted runners have no IPv6.
  - No Hetzner automatic backups (§5.13 covers the data).
- **Why apart.** Dev is where releases get tested and things break; on a shared
  machine a command in the wrong folder can take prod down. The sizes come from
  §7: prod peaks around 5 GB of RAM, dev around 3.
- **Prices** (excl. VAT, after Hetzner's 15 June 2026 adjustment): CX23 €5.49,
  CX33 €8.49, CX43 €15.99, CPX22 €19.49, CPX32 €35.49 per month; IPv4 €0.50;
  Volume about €0.057/GB/month (third-party source; check at purchase). Total
  about €21/month on CX, about €56 on CPX.
- **Stock, 2026-10-05** (hetzner.thegoated.dev): CX33, CX43 and CX53 sold out in
  Nuremberg, Falkenstein and Helsinki; CX23 limited in Falkenstein and Helsinki;
  CPX22 and CPX32 available in Nuremberg and Falkenstein; CAX21 and CAX31 (ARM)
  available everywhere.
- **The ARM option.** CAX is about half the price of CPX: CAX11 (2 vCPU, 4 GB,
  40 GB) €5.99, CAX21 (4 vCPU, 8 GB, 80 GB) €10.49, CAX31 (8 vCPU, 16 GB,
  160 GB) €20.99. Since January 2026 GitHub offers arm64 runners in private repos
  on the Free plan, so CI builds arm64 images natively. It reverses the x86
  choice: images built for `linux/arm64` (the laptop keeps building x86 for local
  tests), and every Python dependency needs an aarch64 wheel for Python 3.14.
  Checked on 2026-10-07 from the lock files: all 15 compiled packages in
  Delegate's `uv.lock` (pymupdf, psycopg-binary, cryptography, pydantic-core,
  regex, lxml and the rest) and all of the mock's ship manylinux aarch64 wheels
  for Python 3.14; `pgvector/pgvector:pg17` is published for arm64; the
  frontend locks carry the linux-arm64 builds of Next's SWC, Tailwind's oxide
  and lightningcss. The first arm64 image build in CI is the final proof.
- **The disk rule.** A rescale only goes to plans with a disk at least as large
  as the current one. A CPX32 (160 GB) can later move to CX43 (160 GB, €15.99,
  16 GB RAM) when it is back in stock, but never to CX33 (80 GB).
- **Rescaling.** Hetzner only rescales within the same architecture, so x86
  stays x86. A rescale needs the server powered off for a few minutes (plan it
  at night) and moves it to current pricing. Choose "CPU and RAM only" when
  scaling up, so a downgrade stays possible. The Volume grows without downtime.

### 5.3 Domain and DNS

- **DNS on Cloudflare**, free. Records are "DNS only": Cloudflare answers where
  the servers are and never sees the traffic.
- **Domain:** `rootfolder.app`, bought on Cloudflare Registrar on 2026-10-07, in
  Alex's name until the company exists. It expires on 2027-10-07 and renews
  automatically; the nameservers are Cloudflare's.
- **What `.app` changes.** The whole `.app` TLD is on the browsers' HSTS preload
  list: Chrome, Firefox, Safari and Edge only ever open `rootfolder.app` and
  every subdomain over HTTPS. Consequences:
  - no page of the app can be opened over plain HTTP, not even on dev before its
    certificate exists; port 80 serves only Let's Encrypt's check and the
    redirect, and is tested with `curl`;
  - Let's Encrypt still validates over port 80 (the HTTP-01 check is not a
    browser), so the certbot plan in §5.4 is unchanged;
  - a lapsed certificate makes the site unreachable with no "continue anyway"
    button, so certificate renewal is monitored (§5.14).
- **The `.com` trap.** `rootfolder.com` belongs to someone else, registered in
  2000 (no website answered on 2026-10-07). People type `.com` out of habit:
  mail to `@rootfolder.com` reaches that owner. Keep the exact domain in every
  signature, contract and invite; clients send documents through the app, never
  by email; a DMARC policy stops others sending as you, not mistyped inbound
  mail.
- **Trademark check for "rootfolder":** not done yet (TMview classes 9 and 42,
  OSIM). The earlier check covered "Delegate Technology" only.
- **After purchase:** auto-renew on (default), DNSSEC on, no server records yet.
- **Records, and when:**

  | Record | When |
  |---|---|
  | MX, SPF, DKIM, DMARC for Zoho on `rootfolder.app` | when the Zoho account is set up (§5.16) |
  | SPF and DKIM for Resend on `mail.rootfolder.app` | when the app's email is set up |
  | `dev` → dev IP | step 24 |
  | `rootfolder.app` and `www` → prod IP; CAA for `letsencrypt.org` | steps 24 and 37 |

  A domain may have only one SPF record; that is why Resend lives on `mail.`.
- **Addresses:** `rootfolder.app` is prod (the landing page is part of the app),
  `www.rootfolder.app` redirects to it, `dev.rootfolder.app` is dev. Mailboxes:
  `admin@rootfolder.app` first.
- **Cloudflare API tokens** are not needed for this plan: certbot gets
  certificates over port 80. If a script ever needs one: one zone, DNS only,
  with an expiry, stored as a secret. Never the Global API Key.

### 5.4 Traffic and nginx

- **Decision.** Traffic goes straight to the servers, with no CDN or proxy in
  front. nginx is the reverse proxy, with certbot for Let's Encrypt
  certificates.
- **Why direct.** Cloudflare's free proxy caps request bodies at 100 MB and cuts
  a response that stays silent for 125 seconds; tender PDFs reach 500 MB and
  answers stream for minutes. Direct traffic also keeps client documents away
  from another company.
- **Why nginx.** Caddy is easier for certificates, nginx has rate limiting built
  in (Caddy needs a plugin and a custom build), and Alex knows nginx. Overall
  the effort is the same, so nginx.
- **Why `/api` on the same domain stays.** The number of requests does not depend
  on the path; a separate `api.` subdomain would be just as public. Same-origin
  `/api` keeps the session cookie on one host and needs no CORS opening. The
  code already works this way: the browser calls `/api/...`, the proxy strips
  the prefix.
- **What the nginx config must do:**
  - `/api/` to `api:8000` without the prefix (`proxy_pass http://api:8000/;`, the
    trailing slash is what strips it); everything else to `web:3000`;
  - `client_max_body_size 510m`; `proxy_request_buffering off` on upload routes,
    so nginx streams the file instead of writing a second copy to disk. In
    Delegate every upload route ends in `/documents`;
  - `proxy_http_version 1.1` and `proxy_read_timeout 300s`, so long streamed
    answers are not cut (the backend already sends `X-Accel-Buffering: no`).
    `text/event-stream` stays out of `gzip_types`;
  - `Host`, `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host` to both
    upstreams, and `X-Request-ID $request_id`, which the app reuses so nginx and
    app log lines share the ID;
  - HSTS on `/api/` only (the frontend sets its own on the UI);
  - rate limits: a general one per IP on `/api/`, a strict one on login, one on
    the chat route that generates answers, since every call there costs money.
    All with `limit_req_status 429`; nginx's default for a limited request is
    503;
  - port 80 serves the Let's Encrypt challenge and redirects to HTTPS;
  - certificate renewal runs daily and reloads nginx. The first certificate
    needs a bootstrap: nginx on port 80 only, certbot, then the HTTPS config.
- **uvicorn trusts nginx by subnet.** The compose network has a fixed subnet
  (`172.30.0.0/24`) and uvicorn runs with `--forwarded-allow-ips` set to it; the
  nginx container's own address changes on every restart.
- **`CORS_ORIGINS` holds the public origin** (`https://<domain>`). The app's
  cross-site write check refuses every POST from an origin not listed, login
  included.
- **What already protects the app:** login limits in the app (10 failures per
  email, 50 per IP, per 15 minutes), authentication on every route except
  login, a check that rejects cross-site writes, security headers and a CSP
  with nonces, `/docs` hidden on a public address. fail2ban can also ban IPs
  that pile up refusals.

### 5.5 SSH access and how CI deploys

- **Access.** SSH on port 22, open to the internet, keys only. Each person has
  their own key pair, generated on their laptop; the server holds the public
  keys. Private keys never leave the laptop and are never shared. Removing a
  person means deleting one line; the logs show who logged in.
- **Hardening** (free, on the server): no password login, no root login,
  fail2ban, unattended security upgrades, and the Hetzner Cloud Firewall
  allowing only TCP 22, 80, 443 and ICMP. Break-glass access: the Hetzner web
  console.
- **CI path.** A GitHub-hosted runner connects over SSH as a `deploy` user whose
  key is locked to one command, the deploy script. Even if the key leaked from
  GitHub, it could only deploy an existing image version.
  - The lock is a `command="..."` entry with `restrict` in `authorized_keys`. SSH
    then ignores the command the runner sends and passes it to the script in
    `SSH_ORIGINAL_COMMAND`, so the script reads its arguments from there and
    accepts only `dev|prod` and two `sha256:` digests.
  - The runner checks the server's host key against a stored secret (the
    server's line from Alex's `known_hosts`), never with host key checking off.
- **Why not the alternatives.** Tailscale's free plan is non-commercial only
  ($8/user/month otherwise); Cloudflare Access adds another service; a runner
  installed on the server would run any workflow code with Docker rights,
  which is root in practice.
- When Claude helps debug on a server, every remote command needs Alex's
  approval.

### 5.6 Background work and deploy downtime

- **Decision.** Background work stays inside the API process, and each deploy
  restarts the API with a downtime of seconds. Accepted at any hour.
- **Why it matters.** OCR, polling of Mistral batch jobs, the daily cleanup,
  titles and emails run inside the API. On startup, `fail_interrupted()` marks
  documents still in progress as failed, because it assumes a single process.
  Two API containers cannot overlap, so a deploy stops the old one before
  starting the new one.
- **Consequences.**
  - One API container per environment.
  - A deploy cuts answers being generated and fails synchronous OCR in
    progress; users retry. uvicorn runs with `--timeout-graceful-shutdown 30`
    so short requests finish. The api container needs `stop_grace_period: 40s`:
    Docker kills a container 10 seconds after asking it to stop, which would cut
    the 30 seconds short.
  - A second app server, or deploys with no downtime at all, first need a
    separate worker process with a job queue in Postgres (no Redis needed).

### 5.7 Deploy tooling: Compose, GitHub Actions, a script

- **Decision.** Each server runs the stack with Docker Compose. GitHub Actions
  builds the images and calls `deploy/scripts/deploy.sh` over SSH.
- **What the script does**, with `set -euo pipefail` and a `--dry-run` flag:
  1. on prod, stops if the Volume is not mounted, so PDFs never land on the
     server's own disk;
  2. dumps the database: on dev so the deploy can be reverted, on prod before
     any migration;
  3. pulls the images by digest;
  4. runs the migrations once, in a one-off api container of the new image;
  5. starts the new containers (`up -d`);
  6. waits for `/health/ready` through nginx and checks that its `version` is the
     new one, so a healthy old container does not pass for the new release;
  7. if it does not answer, starts the previous version again;
  8. records the current and previous versions;
  9. keeps the images of the last two versions and removes older ones.
- **What it does not do:** copy `deploy/` (compose files, nginx config) to the
  server. Those change rarely and are updated by hand, with a written procedure,
  before the deploy that needs them; the CI key can only run the script.
- **On the laptop** the script is tested against a local registry
  (`registry:2`), since an image has a digest only once it is pushed.
- **Why not Kamal, Coolify, Swarm or Kubernetes.** Nothing extra runs on the
  server, and nothing extra needs patching. Coolify also had 11 critical
  vulnerabilities disclosed in January 2026. Move up only on a measured signal:
  a second app server, or deploys with no downtime at all.

### 5.8 Images and the release flow

- **Two images**, `api` and `web`, x86 only, in GHCR (free for private images;
  GitHub promises a month's notice before charging). The `api` image also runs
  migrations and the admin scripts (`create_superadmin.py`, `set_password.py`,
  `cleanup.py`, `ingest_foundation.py`).
- **Servers deploy by digest**, the image's unique fingerprint, so dev and prod
  run exactly the bytes that were built.
- **Release flow:**
  - **Dev, on demand:** a manual workflow deploys a ref, `main` by default. It
    builds images for that commit once, tagged `sha-<commit>`, unless they
    already exist.
  - **Prod, from a tag:** a manual workflow deploys a tag `vX.Y.Z`. It finds the
    tag's commit, reuses the `sha-<commit>` images and adds the version tag
    without rebuilding; it builds only if that commit never went to dev.
    Creating a tag deploys nothing by itself. GitHub generates the release
    notes.
  - **Rollback:** the prod workflow with an older tag. Dev back to prod's
    version: the dev workflow with prod's tag as the ref.
- **Rules:**
  - Put the tag on the commit you tested on dev; only then does prod run exactly
    what was tested.
  - Never delete or move a published tag. A failed release stays in history; the
    fix gets the next version.
  - Migrations are expand/contract: a release only adds (columns, tables); what
    is removed goes in a later release. The previous app version then still
    runs on the new schema, and a rollback does not touch the database.
  - Heavy data changes, such as re-embedding the archive or a new search index,
    run as separate jobs: add a new column, fill it in the background, switch
    reads, drop the old one later. Never inside a deploy.
- **Version everywhere:** in the image, `/health`, the logs and the UI footer.
  - The version is `sha-<commit>`, given to both image builds as `APP_VERSION`.
    Prod reuses dev's images, so prod also shows `sha-<commit>`; the GitHub
    Release ties the tag to that commit.
  - `APP_ENV` is not baked in: it comes from each server's `.env`, otherwise one
    image could not serve both environments.
  - Adding the version tag to existing images is a manifest copy
    (`docker buildx imagetools create`), with no rebuild; the digest stays the
    same.
- **The first images** come from the build half of the dev workflow, written and
  run by hand at step 27; the SSH half is added at step 29.

### 5.9 Git and GitHub

- The repo stays under the personal account `maskarrow` on GitHub Free. Nothing
  in the deploy design needs a paid plan: manual workflows, repository secrets
  and 2,000 Actions minutes a month (estimated use: 1,000-1,200) all work on
  Free.
- `main` is not technically protected; that needs GitHub Pro ($4/month) or Team.
  CI checks run on every PR but only inform. `docs/DEVELOPMENT.md` claims `main`
  is protected; it should be corrected.
- Branches are the developers' business. DevOps consumes `main` and tags.
- Deploy keys for dev and prod are repository secrets; the forced command on the
  servers limits what they can do.
- When the company exists, moving the repo to an organization it owns is worth
  doing.

### 5.10 Secrets and provider keys

- **Every provider key belongs to an org** and lives encrypted in the database.
  Each client's admin enters Anthropic, OpenAI, Mistral and any other keys on the
  admin keys screen; the app encrypts them with `API_KEYS_ENCRYPTION_KEY`. With
  about 4 keys per client, keys could never live in `.env`.
- **Delegate itself is a normal client org**, "Delegate", with the team's own
  keys, used for QA in prod. Its admin is a founder's regular admin account, not
  the superadmin.
- **Code change needed** (step 10): today the install's keys in `.env` still
  serve the agent, embeddings, titles and OCR. Every provider call, background
  work included, must use the key of the org it works for.
- **`.env` on each server** holds only infrastructure secrets: the database
  password, `API_KEYS_ENCRYPTION_KEY`, the Resend key, plus non-secret settings
  (`APP_URL`, `CORS_ORIGINS`, `EMAIL_FROM`, `APP_ENV`). `chmod 600`, owned by
  `deploy`, never in git, values kept in the vault.
- **Encrypting `.env` on the server was rejected:** the app needs the values in
  clear at startup, so the decryption key would sit on the same machine.
- **The master key** (`API_KEYS_ENCRYPTION_KEY`) is the one to guard. Losing it
  makes every saved client key unreadable. It stays in the vault, apart from the
  database backups, so a stolen backup exposes no keys.
- Separate keys for dev and prod at each provider, with a monthly spend limit
  set at the provider.

### 5.11 Database, files and dev data

- **Postgres** (`pgvector/pgvector:pg17`, major version pinned) runs in a
  container on each server, managed by the team. No managed database, no
  separate DB server. It is never published on a host port.
- **Postgres needs `shm_size: 1g`.** Docker gives containers 64 MB of shared
  memory, and a parallel HNSW index build can fail with "No space left on
  device".
- **PDFs** stay on disk. On prod they go on the Volume, which survives a server
  rebuild and can be moved to a new server instead of downloading everything
  from backups.
  - The Volume is mounted at `/mnt/storage` through `/etc/fstab` (by UUID, with
    `nofail`); the PDFs live in `/mnt/storage/pdfs`, owned by uid 10001, the
    non-root user of the api image.
  - In the container they are at `/data/storage` (`STORAGE_DIR`). The path must
    be absolute: inside the image the project root is `/`, so a relative
    `storage` would point at `/storage`, which the non-root user cannot write.
- **Dev holds test data only:** documents the team uploads plus a subset of an
  archive for validation. Never a copy of prod's client data.
- **File paths** must be stored relative to `STORAGE_DIR` (step 8). Today the
  database holds absolute paths like `C:\...\storage\<uuid>.pdf`, which break on
  any other machine.
- **The first client's archive** is ingested fresh on prod (step 40), not moved
  from a laptop database.

### 5.12 Upload limits

- **Every PDF is capped at 500 MB**, with a clear message asking for a smaller
  file. Mistral accepts uploads of at most 512 MB, and the app sends each PDF
  whole; 500 leaves a margin. No splitting.
- **What fits.** A 500-page document is 25-100 MB when exported from Word,
  25-50 MB scanned in black and white, 150-500 MB scanned in color, 100-250 MB
  from a phone scanning app. Only phone photos pasted straight into a PDF
  (1-2 GB) go over.
- **Code changes** (step 9): tender uploads are uncapped today, and
  `ingest_foundation.py` copies files with no size check. Chat and workspace
  uploads use `MAX_UPLOAD_MB` (50 by default); raising it is open (§3).
- **Where an upload waits.** Starlette writes a multipart upload to the
  container's `/tmp` before the route copies it into storage. `/tmp` stays on
  the container's disk (not `tmpfs`, where 500 MB would count against the
  container's memory limit); §7 counts the space.
- **Bulk archives** of tens of GB go to the server with `rsync`, which resumes
  after a dropped connection, then through `ingest_foundation.py`. The import
  folder is mounted read-only into the one-off api container; the container
  cannot see host folders otherwise.

### 5.13 Backups (proposed, to confirm)

- **What a backup must survive:** a mistake in the data (a bad migration, a
  bug that deletes rows); the server or its disk dying; a problem with the
  Hetzner account; someone on the server deleting everything they can reach; a
  backup that silently does not restore.
- **Alex's intent:** one method.
  - Every night at 03:00, a full `pg_dump` of the database, compressed and
    encrypted, streamed straight to Backblaze B2 (EU region), and the new PDFs
    with restic to the same place.
  - Weekly, or when a new client joins, an encrypted copy of the database on a
    founder's laptop.
  - No Hetzner backups, no continuous WAL archiving.
- **Points to confirm:**
  - **The whole database, not selected tables.** Documents belong to chats,
    workspaces or tenders, and messages to chats; a dump that skips tables can
    restore with errors or orphaned rows. Chats cost a few MB compressed.
  - **The laptop copy holds clients' documents as text.** Conditions: BitLocker
    on the laptop, the file encrypted, not in a synced folder like OneDrive,
    only the latest copy, the master key not on the same laptop, and the client
    contract allows it.
  - **Up to 24 hours of data loss.** A server lost at 18:00 loses the day's work
    since 03:00.
- **Recommended additions, both cheap:**
  - a B2 bucket with Object Lock and a server key that can write but not delete,
    so nobody on the server can wipe the backups. A lifecycle rule removes
    copies after 30 days, once their lock has expired. restic writes and removes
    its own lock files, so two consecutive backup runs with that key are the
    test that it is enough;
  - a monthly restore test on prod in a temporary container, with the result by
    email; once before launch, a full drill on a new server, timed.
- **Cost:** B2 costs $6.95 per TB per month and the first 10 GB are free; under
  100 GB comes to less than $1 a month.
- **Dev** gets no off-site backup, only the dump before each deploy.

### 5.14 Monitoring

- **Decision.** Watch whether the app is up, what fails and why, and whether the
  servers need more CPU, RAM or disk. No tracking of individual user actions.
- **Analysis data is already in the database:** each message stores the exact
  prompt, the answer, the sources, tokens and cost, and the usage screen shows
  them. Log files carry errors and system events only, never document text.
- **Proposed tools**, all free, alerts by email:
  - **UptimeRobot:** checks the site from outside every 5 minutes (50 monitors
    free, commercial use allowed again since August 2026);
  - **Sentry**, free plan, EU region: errors, 5,000 a month, one user. Configured
    to send no request bodies and no document text;
  - **Beszel:** CPU, RAM and disk on both servers, with history and alerts. The
    hub runs on dev, so prod's metrics stay visible when prod is down. It sends
    email through Resend, since Zoho's free plan has no SMTP;
  - **Docker logs** with rotation, read over SSH;
  - **certificate renewal:** certbot renews 30 days before expiry, and the daily
    renewal timer emails through Resend when a renewal fails. On `.app` an
    expired certificate leaves the site unreachable (§5.3), so a failure gets
    a month of daily warnings first.
- A full stack (Grafana, Loki, Alloy, Prometheus, 1.5-2 GB of RAM) only when
  searching logs becomes routine.

### 5.15 Server setup

- **cloud-init plus scripts.** A cloud-init file, pasted when creating a server,
  sets up the users (Alex, and `deploy` with its forced-command key), SSH
  without password or root, Docker, fail2ban, unattended upgrades and Docker log
  rotation. A bootstrap script does the rest. No OpenTofu or Ansible for now;
  reconsider past three servers.
- **Details that matter:**
  - Docker from Docker's own apt repository, with the `compose` plugin;
  - log rotation in `/etc/docker/daemon.json` (`json-file`, 10 MB, 5 files);
  - a 2 GB swap file: the servers come without swap, and without it a memory
    peak kills a container instead of slowing it down;
  - automatic reboots for kernel updates at 04:30, if decided (§3, item 8);
  - on prod, the Volume formatted at creation and mounted by `/etc/fstab`, not by
    Hetzner's automount, which would use `/mnt/HC_Volume_<id>`.
- **Target:** a new server serving the app with restored data in about 30
  minutes of work plus restore time, measured in a drill every quarter.
- **Procedures** (bootstrap, deploy, rollback, rotating a secret) are steps in
  STEPS.md.

### 5.16 Company email

- **Zoho Mail** for the founders' addresses. The free plan (up to 5 users, 5 GB
  each, web and mobile apps only, no IMAP, POP or SMTP) is "available only in
  select data centers", and Zoho's setup screen for `rootfolder.app` offered
  only paid plans on 2026-10-07: Mail Lite €1.13/user/month at 10 GB (5 GB a
  bit less), Mail Premium €3.60, Workplace Standard €2.70 and Professional
  €5.40, all billed yearly, excl. VAT. Open (§3, item 9):
  - first, the free signup link on the EU data center
    (`https://mail.zoho.eu/signup?type=org&plan=free`); it either offers the
    plan or says it is not available;
  - otherwise Mail Lite, the cheapest plan: about €41/year for three users at
    10 GB. It includes IMAP, POP and SMTP, so phone and desktop mail apps work,
    and Beszel could send its alerts through Zoho instead of Resend (§5.14).
    Workplace adds an office suite and file storage the team does not need.
- **Users:** one per founder, each with their own login, plus a group
  `admin@rootfolder.app` that delivers to all three. Groups and aliases cost
  nothing extra; a user can be added when someone actually needs a mailbox.
- **Emails the app sends** (invites, password resets) go through **Resend**, not
  Zoho: free up to 3,000 a month and 100 a day. Users' own addresses never
  count against Zoho's limit.
- The admin accounts (Cloudflare, Hetzner, GitHub) move to `admin@rootfolder.app`,
  so their recovery does not depend on one person.

### 5.17 Rehearsal with delegate-mock

- **What it is.** A small app, in its own repo (`delegate-mock`), with Delegate's
  technical shape and none of its product:
  - the same stack (Next.js 16, FastAPI on uvicorn, Postgres 17 with pgvector
    and the `romanian` configuration);
  - the same patterns, copied from Delegate's code: session cookie, cross-site
    write check, security headers, CSP with nonces, `/api` rewrite, org scoping,
    encrypted keys, Resend;
  - one dashboard card per thing a deploy can break, listed in its README.
- **Why.** It already has everything stage 3 adds to Delegate: relative file
  paths, the 500 MB cap, per-org keys, `/health/live` and `/health/ready`, JSON
  logs with request IDs, the email check and the version in the UI. So the
  containers, nginx, the deploy script, the servers and the workflows can be
  built and proven before Delegate's code changes land, and a mistake in them
  shows up on an app with no client data.
- **How it is used.**
  - Steps 2-7: every infrastructure file is written in the mock's repo first,
    verified there, then copied into Delegate and adapted (image names, scripts,
    upload and chat routes, smoke test).
  - Steps 27 and 37: the first deploy on dev and the first on prod are the mock,
    run through its smoke test, then removed (`docker compose -p delegate-mock
    down -v`, plus its PDFs on prod's Volume). It uses the same folder and the
    same deploy key as Delegate, since it rehearses exactly that layout.
  - Step 31: the full release cycle is run with the mock first.
- **Cost.** Its CI and image builds use the same 2,000 free Actions minutes per
  month as Delegate (§5.9); a few rehearsal deploys fit comfortably.
- **What building it found** (2026-09-30 to 2026-10-03), now in STEPS.md:
  - Docker Desktop on Windows needs WSL 2, which needs an administrator and a
    restart;
  - `alembic check` fails when an index exists only in a migration;
  - a server-side call from Next to the API never passes nginx, so it cannot
    show whether nginx forwards the client's IP; the check must come from the
    browser (Delegate's audit log shows it per login);
  - FastAPI writes a keep-alive comment after 15 seconds of silence on an SSE
    stream, so nginx's read timeout only has to cover 15 seconds;
  - Next's local `/api` rewrite silently truncates bodies above
    `proxyClientMaxBodySize`, and its destination is fixed at build time;
  - uploads wait in `/tmp` before reaching storage; Docker's 10-second stop
    timeout undercuts uvicorn's 30-second graceful shutdown.

---

## 6. What the application needs before prod

Found by reading the code on 2026-09-28.

**Must change** (steps 8-14):

| Gap | Why it matters | Step |
|---|---|---|
| Absolute file paths in `documents.stored_path` | break on the server and on any move | 8 |
| Tender uploads uncapped | Mistral refuses files over 512 MB | 9 |
| Install-level provider keys in `.env` | keys must be per org (§5.10) | 10 |
| `/health` only checks the database | a liveness check would restart the API on every database blip; no version shown | 11 |
| No structured logs | no request ID, version or environment in logs | 12 |
| Email broken (TODO #28); startup refuses a public `APP_URL` without `RESEND_API_KEY` | no invites or password resets on prod | 13 |
| No version in the UI, no release build ID | tabs opened before a deploy ask for files that no longer exist (probably TODO #13) | 14 |

**Handled by the container setup** (steps 2-7):
- there are no Dockerfiles or production compose files yet;
- `docker-compose.yml` publishes Postgres with password `delegate` on all
  interfaces (local only, stays so);
- uvicorn must run with `--proxy-headers` and `--forwarded-allow-ips` set to the
  compose subnet, otherwise every user shares nginx's IP and the
  50-failures-per-IP limit locks everyone out;
- `STORAGE_DIR` must be absolute in the container (the project root there is
  `/`);
- `next.config.ts` needs `output: "standalone"`, and the image must copy
  `.next/static` and `public` itself and set `HOSTNAME=0.0.0.0`;
- on a fresh database, migrations run before the api starts: its startup checks
  read tables.

**Already right, keep it:**
- no `NEXT_PUBLIC_*` variables, so one `web` image serves dev and prod;
  `API_INTERNAL_URL` is read at runtime;
- `next/font/google` downloads fonts at build time: the build needs internet,
  the running container does not;
- migrations do not run on app startup;
- security headers, CSP with nonces, HSTS on the UI, rate-limited login,
  encrypted provider keys scrubbed from errors;
- startup refuses a missing encryption key when encrypted rows exist;
- SSE responses already carry `X-Accel-Buffering: no` and `no-transform`;
- the tests (735) are CI-ready: real Postgres, network banned, providers faked.

---

## 7. Sizing

Prod, with everything that runs on it:

| What runs | RAM, normal | RAM, peak | Disk |
|---|---|---|---|
| OS and Docker | 0.5 GB | 0.5 GB | ~6 GB |
| nginx and certbot | 0.05 GB | 0.1 GB | little |
| web (Next.js) | 0.3 GB | 0.5 GB | image ~0.3 GB |
| api | 0.4 GB | 1 GB | image ~1 GB |
| Postgres | 1-2 GB | 3 GB | a few GB for the first client |
| Beszel agent | 0.02 GB | 0.02 GB | little |
| Nightly backup | 0 | +0.3 GB | none, streamed to B2 |
| Deploy (migration and new image next to the old one) | 0 | +0.4 GB for a minute | current and previous images, ~3 GB |
| A 500 MB upload in progress | | | ~1 GB temporary |
| **Total** | **~3 GB** | **~5 GB** | **~15 GB plus the PDFs** |

- **The database stays small even for archives of tens of GB.** Scans are
  large because they are images; the database holds only the text and the
  vectors. A 500-page document makes about 600 chunks, 15-20 MB in the
  database. 100 tenders of 200 pages each fit under 1 GB, and the search index
  fits in RAM easily. Around 500,000 chunks (about 10 large clients) the vector
  index needs 3-4 GB, which is the point for 16 GB.
- **Why 8 GB and not 4:** the peak is 5 GB. **Why not 16:** at the first client
  the normal use is 3 GB; Beszel shows when it is time.
- **Dev** uses about 2 GB normally and 3 at peak; 4 GB and 40 GB of disk hold a
  test subset of an archive.
- **CPU** is mostly idle: answers are generated by the providers. Peaks come from
  PDF exports, gzip during backups and index updates during ingestion.

---

## 8. External services and data flows

| Service | Used for | Sees client data |
|---|---|---|
| Hetzner (Germany) | servers, Volume | yes, everything |
| Anthropic | the agent and answer generation | yes: prompts with document passages |
| OpenAI | embeddings, chat titles, generation if chosen | yes: archive chunks, first message |
| Mistral | OCR | yes: whole PDFs |
| Google, DeepSeek, Groq | generation, only if an org saves those keys | yes |
| Resend | invite and reset emails | email addresses and links |
| Backblaze B2 (EU) | backups | encrypted copies only |
| Sentry (EU) | error tracking | errors only, configured without bodies or document text |
| UptimeRobot | uptime checks | no |
| Zoho | the founders' mailboxes | whatever is emailed |
| Cloudflare | DNS only | no |
| GitHub | code, CI, images | code only |

This is the list of data processors a client's GDPR questions will need (§3,
item 7). The servers must reach, outbound: the provider APIs above, the
Let's Encrypt CA, GHCR, Backblaze, OS updates and the monitoring services.

---

## 9. Facts from the founders

| Topic | Answer |
|---|---|
| Domain | `rootfolder.app`, bought on Cloudflare on 2026-10-07 |
| Email on the domain | none; Zoho Mail once the domain exists |
| Company | not founded; accounts in Alex Visoiu's name for now |
| Cloudflare account | exists (Alex) |
| Prod server | Hetzner, bought 2026-10-07, IPv4 `2.31.2.210`; only prod for now, dev later |
| Budget | start with the proposed servers, rescale when the metrics say so |
| Availability | a deploy downtime of seconds is fine at any hour |
| Largest file | about 500 pages, under 512 MB |
| GitHub | personal account `maskarrow`, Free plan |
| Who does DevOps | Alex alone; the other founders develop |
| Existing servers, rack | not part of the plan |
| First client's requirements | not answered |

---

## 10. Later: scaling, triggered by measurements

| Signal (Beszel) | Action |
|---|---|
| RAM above 75% for a week, or swap in use | rescale prod, e.g. to CX43 (16 GB, €15.99), a few minutes off at night |
| Disk above 80% | grow the Volume, no downtime |
| CPU above 70% through working hours | rescale |
| Need for a second app server or zero-downtime deploys | first the worker process (§5.6), then a load balancer (Hetzner LB11, €7.49/month) and Kamal or Swarm |
| Postgres needs more RAM than it can share | Postgres on its own server, on a Hetzner private network |

---

## 11. Sources (checked 2026-09-28 to 2026-09-30)

- [Hetzner price adjustment, 15 June 2026](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)
- [Hetzner prices, September 2026](https://costgoat.com/pricing/hetzner)
- [CX/CAX unavailable, September 2026](https://bex.co/blog/2026/09/24/hetzner-cheap-tier-unavailable-fleet-planning)
- [Hetzner Cloud FAQ, rescaling](https://docs.hetzner.com/cloud/servers/faq/)
- [Cloudflare connection limits](https://developers.cloudflare.com/fundamentals/reference/connection-limits/)
- [Cloudflare, registering a domain](https://developers.cloudflare.com/registrar/get-started/register-domain/)
- [.app requires HTTPS (HSTS preload)](https://get.app/)
- [Mistral known limitations, 512 MB uploads](https://docs.mistral.ai/resources/known-limitations)
- [GitHub plans and features in private repos](https://docs.github.com/en/get-started/learning-about-github/githubs-plans)
- [GitHub environments per plan](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
- [GHCR, free for private images](https://github.com/orgs/community/discussions/183054)
- [Tailscale pricing, Personal plan non-commercial](https://tailscale.com/pricing)
- [Coolify, 11 critical vulnerabilities](https://thehackernews.com/2026/01/coolify-discloses-11-critical-flaws.html)
- [Backblaze B2 pricing](https://www.backblaze.com/cloud-storage/pricing)
- [Sentry free plan](https://costbench.com/software/developer-tools/sentry/free-plan/)
- [UptimeRobot free plan](https://help.uptimerobot.com/en/articles/11604710-who-should-use-uptimerobot-s-free-plan)
- [Resend limits](https://resend.com/docs/knowledge-base/account-quotas-and-limits)
- [Zoho Mail pricing](https://www.zoho.com/mail/zohomail-pricing.html)
