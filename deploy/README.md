# Deploying rootfolder-mock

How the mock gets from a commit to https://rootfolder.app, and every command used
on the server. The real app will use the same files with its own names.

## The pipeline

```
push to main ──> CI (.github/workflows/ci.yml): ruff, pytest on Postgres,
                 alembic check, eslint, tsc, next build

"Deploy" button (.github/workflows/deploy.yml), with a ref, default main:
  build   on GitHub's ARM runner: api and web images for that commit,
          tagged sha-<commit>, pushed to ghcr.io/maskarrow/rootfolder-mock-*
          (reused if that commit was built before)
  deploy  ssh deploy@server "<api>@sha256:... <web>@sha256:..."
          └─> /opt/rootfolder/scripts/deploy.sh (the only thing that key can run)
                1. stops if the storage volume is not mounted
                2. pulls both images
                3. dumps the database to backups/ (keeps 3)
                4. runs the migrations once, with the new api image (scripts/migrate.py:
                   skipped when the database is already ahead of the image)
                5. recreates api and web, waits until they are healthy
                6. waits for https://rootfolder.app/api/health/ready to report
                   the new version, through nginx
                7. if 5 or 6 fails: back to the previous images
                8. records the release in deploy.log, removes old images
```

The database is never rolled back: migrations only add, so the previous release
also runs on the new schema.

## On the server

```
/opt/rootfolder/                  owner deploy
  compose.yaml                    from deploy/ in this repo
  nginx/bootstrap/, nginx/app/    from deploy/ in this repo
  scripts/deploy.sh               from deploy/ in this repo
  .env                            secrets and settings (from .env.example), chmod 640
  compose.override.yaml           the running release's images (deploy.sh)
  compose.override.previous.yaml  the release before it (deploy.sh)
  deploy.log                      one line per successful deploy
  backups/                        database dumps from the last 3 deploys
  certbot/                        certificates and the ACME challenge folder
/mnt/storage/pdfs/                uploaded PDFs, owner 10001 (the api image's user)
```

Everything below runs on the server as your own user, `alex`, from
`/opt/rootfolder`. `alex` is in the `docker` group, and in the `deploy` group so it
can read `.env`, which compose needs for every command.

## First time

1. **Copy the files**, from the repo folder on the laptop:

   ```bash
   scp -r deploy alex@2.31.2.210:~/
   ```

   then on the server:

   ```bash
   sudo cp -r ~/deploy/. /opt/rootfolder/ && rm -rf ~/deploy
   sudo mkdir -p /opt/rootfolder/certbot/conf /opt/rootfolder/certbot/www /opt/rootfolder/backups
   sudo chown -R deploy:deploy /opt/rootfolder
   sudo usermod -aG deploy alex
   ```

   Log out and back in once, so the new group applies.

2. **The PDF folder** on the volume:

   ```bash
   sudo mkdir -p /mnt/storage/pdfs && sudo chown 10001:10001 /mnt/storage/pdfs
   ```

3. **`.env`**: copy `.env.example`, fill in the empty values (the commands to
   generate them are in the file), keep a copy in the password manager:

   ```bash
   sudo -u deploy cp /opt/rootfolder/.env.example /opt/rootfolder/.env
   sudo -u deploy chmod 640 /opt/rootfolder/.env
   sudo -u deploy nano /opt/rootfolder/.env
   ```

   `640`: readable by `deploy` and its group (you), by nobody else.

4. **Pulling images from GHCR**: as `deploy`, with a GitHub token that can only
   read packages (classic PAT, `read:packages`):

   ```bash
   sudo -iu deploy docker login ghcr.io -u maskarrow
   ```

5. **The first certificate.** nginx starts with the port-80-only config, certbot
   proves the domain is ours, then the full config takes over at the first
   deploy:

   ```bash
   cd /opt/rootfolder
   NGINX_CONFIG=bootstrap docker compose up -d nginx
   docker compose run --rm certbot certonly --webroot -w /var/www/certbot -d rootfolder.app -d www.rootfolder.app --email <your email> --agree-tos --no-eff-email
   ```

6. **Renewal**, checked daily:

   ```bash
   sudo cp /opt/rootfolder/systemd/certbot-renew.* /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now certbot-renew.timer
   ```

7. **The CI key**: a key pair only GitHub uses, without a passphrase (GitHub
   cannot type one). Generate it on the laptop, in Git Bash:

   ```bash
   ssh-keygen -t ed25519 -N "" -C github-actions-deploy -f ~/.ssh/rootfolder_ci
   ```

   Its private half (`~/.ssh/rootfolder_ci`) becomes the `DEPLOY_SSH_KEY` secret
   and nothing else. Its public half (`~/.ssh/rootfolder_ci.pub`) goes to
   `deploy`, locked to the script:

   ```bash
   sudo -u deploy mkdir -p -m 700 /home/deploy/.ssh
   echo 'command="/bin/bash /opt/rootfolder/scripts/deploy.sh",restrict <public key>' | sudo -u deploy tee /home/deploy/.ssh/authorized_keys
   sudo chmod 600 /home/deploy/.ssh/authorized_keys
   ```

8. **Deploy** from GitHub (Actions > Deploy > Run workflow), then create the
   first user and the demo data:

   ```bash
   docker compose run --rm api python create_user.py you@example.com "Your Name" --org Rootfolder --admin
   docker compose run --rm api python seed.py
   ```

## Every kind of change, and how to do it

Two things decide the path of a change: **where it lives** (in the repo, or only on
the server) and **whether it is structure or content**.

- **In the repo, goes through the Deploy button:** application code (backend,
  frontend) and the database's *structure* (migrations). Everything that must be
  the same on your laptop, in CI and on the server.
- **Only on the server, no button:** `.env`, the files in `deploy/` once copied,
  and the database's *content* (accounts, demo data, keys).

| You want to change | Where it lives | Do this | Build | Restart | Risk to know |
|---|---|---|---|---|---|
| **Database structure**: add a field or a table | a migration file in the repo | Edit the model, generate and review a migration, push, Deploy | yes (every commit builds both images) | `api`, `web`: seconds | None for data: the column is added, the old code ignores it |
| **Database structure**: rename or remove a field | three migrations over three deploys | See "Renaming a field" below | yes, three times | seconds each | A one-step rename breaks the running release and the rollback |
| **Database content**: users, demo data, a key | rows, not files | The scripts under "Accounts, demo data and keys" | no | none | None for the app; the audit log records accounts |
| **Backend only** | `backend/` | Push, Deploy | yes | `api`, `web` | An API that gets stricter breaks the old frontend for a few seconds |
| **Frontend only** | `frontend/` | Push, Deploy | yes | `api`, `web` | The browser tab open during a deploy asks for old files; the build ID is the version, so a refresh fixes it |
| **Backend and frontend** | both | One commit, one Deploy | yes | `api`, `web` | While they restart, the old one talks to the new one: make API changes additive |
| **Database, backend and frontend** | all three | One commit with the migration, the code and the UI, one Deploy | yes | `api`, `web` | The migration must work for the *old* code too (it runs before the restart) |
| **nginx** (routing, limits, timeouts) | `deploy/nginx/` | Edit, commit, copy to the server, test, recreate nginx | no | `nginx`: ~1 s | A typo takes the whole site down; test before recreating |
| **compose.yaml** (limits, ports, a service) | `deploy/compose.yaml` | Edit, commit, copy to the server, `docker compose up -d` | no | the services that changed | A wrong value stops a service: run `docker compose config` first |
| **Configuration** (`.env`) | the server only | Edit, `docker compose up -d` | no | the services that read it | Not in git: keep the password manager's copy in step |
| **deploy.sh, migrate.py** | `deploy/scripts/` | Copy to the server (below); the next Deploy uses them | no | none | The pipeline cannot update its own script |
| **The workflows** | `.github/workflows/` | Push | no | none | Takes effect on the next run |
| **A third-party image** (nginx, Postgres patch) | an image tag | `docker compose pull nginx db && docker compose up -d` | no | the ones that changed | Postgres *major* versions (17 to 18) need a dump and restore: a separate procedure |
| **Server software** | Ubuntu | Automatic security updates; `sudo apt update && sudo apt full-upgrade` | no | the server, rarely | A kernel update needs a reboot |
| **Going back** | an older commit | Deploy with that commit's full hash as the ref | no (images exist) | `api`, `web` | Migrations are skipped; the older code runs on the newer schema |
| **GitHub secrets** (deploy key, host) | GitHub settings | Replace the secret; for the key also replace the line in `/home/deploy/.ssh/authorized_keys` | no | none | A wrong value shows up at the next deploy, not before |

### How a database change is made

Always from the repo, never by typing SQL on the server. A change made by hand is
not recorded: your laptop and CI no longer match the server, `alembic check` fails,
and the next migration can collide with it. The server's database has one history,
the migration files, and the deploy is what applies them.

1. Change the model in `backend/app/models/`.
2. Generate the migration, from `backend/`:

   ```bash
   uv run alembic revision --autogenerate -m "add items.category"
   ```

3. **Read the generated file.** Autogenerate does not understand a rename: it
   writes "drop the old column, add a new one", which deletes the data. Check what
   it does to existing rows, and add the `UPDATE` that fills a new column if needed
   (migration 0003 is the example).
4. `just test` (it climbs all migrations from zero) and `alembic check`.
5. Commit, push, Deploy. `deploy.sh` dumps the database first (`backups/`), then
   runs the migration once, then restarts.

### Renaming a field, without breaking anything

Renaming `items.title` to `items.name`, over three releases. At each step the
previous release still works on the new schema, so a rollback is always possible.

| Release | Migration | Code |
|---|---|---|
| 1 (expand) | Add `name`, copy `title` into it | Writes both columns, reads `title` |
| 2 (switch) | none | Reads and writes only `name` |
| 3 (contract) | Drop `title` | none |

The shortcut, `RENAME COLUMN` in one release, is instant in Postgres, but the old
containers are still running while the migration runs, so every request that
touches the field fails until the restart, and going back to the previous release
is no longer possible. For this mock, which tolerates seconds of downtime, you may
accept that for a field nobody uses; do not do it for one that is.

### Changing nginx or the stack, safely

On the laptop, edit the file under `deploy/` and commit it. Copy it to the server
(see below), then:

```bash
docker compose run --rm --no-deps nginx nginx -t
```

It checks the new configuration in a throwaway container; the running nginx is not
touched. Only if it prints `test is successful`:

```bash
docker compose up -d --force-recreate nginx
```

For `compose.yaml`, `docker compose config` checks it, then `docker compose up -d`.

### Updating files on the server

From the laptop, in the repo folder:

```bash
scp deploy/scripts/deploy.sh deploy/scripts/migrate.py alex@2.31.2.210:~/
```

On the server:

```bash
sudo install -o deploy -g deploy -m 755 ~/deploy.sh ~/migrate.py /opt/rootfolder/scripts/
rm ~/deploy.sh ~/migrate.py
```

`compose.yaml` and `nginx/` are copied the same way, into `/opt/rootfolder/` and
`/opt/rootfolder/nginx/`.

### Going back to an older release

Find its full commit hash (`git log -1 --format=%H <short hash>` on the laptop),
then Actions > Deploy > Run workflow with that hash in the "ref" field. The images
for that commit already exist, so the build is a check, not a build. Then deploy
`main` again to come back.

## Accounts, demo data and keys

None of these needs a deploy: the scripts run inside a one-off api container, on
the live database. Passwords and keys are typed in the terminal, never on the
command line.

| What | Command |
|---|---|
| List the accounts | `docker compose run --rm api python list_users.py` |
| A colleague's account (member) | `docker compose run --rm api python create_user.py colleague@example.com "Name" --org Rootfolder` |
| The same, as an admin | add `--admin` |
| A new password (logs the account out everywhere) | `docker compose run --rm api python set_password.py colleague@example.com` |
| Delete an account (asks to type the email again) | `docker compose run --rm api python delete_user.py colleague@example.com` |
| The demo data (idempotent) | `docker compose run --rm api python seed.py` |
| The org's Anthropic key, encrypted | `docker compose run --rm api python set_org_key.py --org Rootfolder --provider anthropic` |

A member sees everything except the two admin checks (provider key, test email).

## Every day

| What | Command |
|---|---|
| State of the containers | `docker compose ps` |
| Logs, live | `docker compose logs -f api` (or `web`, `nginx`, `db`) |
| Which release runs | `tail -n 3 deploy.log` |
| A deploy by hand | `sudo -u deploy scripts/deploy.sh <api>@sha256:... <web>@sha256:...` |
| Back to an older release | Actions > Deploy with the older commit as the ref (or by hand, with the two images from its line in `deploy.log`) |
| A changed `.env` | `docker compose up -d` |
| Changed files in `deploy/` | see "Updating files on the server" and "Changing nginx or the stack, safely" |
| A database shell | `docker compose exec db psql -U rootfolder rootfolder` |
| Restore a dump | `docker compose exec -T db pg_restore -U rootfolder -d rootfolder --clean < backups/<file>.dump` |

## Removing the mock, before the real app

```bash
cd /opt/rootfolder
docker compose down -v          # containers, network and the mock's database
sudo rm -rf /mnt/storage/pdfs/* # its PDFs
```

The certificate and the server setup stay; the real app's files replace these.
