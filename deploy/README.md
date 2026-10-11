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

## What each kind of change costs

| Kind of change | What you do | What happens |
|---|---|---|
| **Code** (frontend, backend, a small fix) | Push to `main`, press Deploy | Both images are rebuilt (the version is baked into both, so a commit always rebuilds them), the server pulls them, and `api` and `web` are recreated: seconds of downtime. The database is untouched. |
| **Code and a migration** | The same | Before anything else `deploy.sh` dumps the database to `backups/`, then runs the migration once with the new image, then recreates the containers. Existing data stays and is changed by the migration (0003 fills `last_login_at` from the audit log). |
| **Only the database's data** (accounts, demo data, a key) | The scripts under "Accounts, demo data and keys" | No deploy, no build, no restart. |
| **Configuration** (`.env`) | Edit `.env`, then `docker compose up -d` | Only the containers whose configuration changed are recreated. No build, no GitHub. |
| **Server files** (`deploy/`: compose, nginx, scripts) | Copy them to the server (below) | Not part of the Deploy button: the pipeline can only run `deploy.sh`. |
| **Going back** | Deploy again with an older commit as the ref | The images already exist, so no build. Migrations are skipped when the database is ahead of the image (older code runs on the newer schema because migrations only add). |

The rule that makes this safe: **a migration only adds** (a column, a table, an
index) and never removes or renames in the same release. What you remove goes in a
later release, once no running code uses it.

### Updating the scripts or the stack files on the server

From the laptop, in the repo folder, for the two scripts:

```bash
scp deploy/scripts/deploy.sh deploy/scripts/migrate.py alex@2.31.2.210:~/
```

On the server:

```bash
sudo install -o deploy -g deploy -m 755 ~/deploy.sh ~/migrate.py /opt/rootfolder/scripts/
rm ~/deploy.sh ~/migrate.py
```

For `compose.yaml` or `nginx/`, copy them the same way into `/opt/rootfolder/`,
then `docker compose up -d` (and `docker compose up -d --force-recreate nginx` if
only the nginx configuration changed).

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
| Changed files in `deploy/` | see "Updating the scripts or the stack files on the server" |
| A database shell | `docker compose exec db psql -U rootfolder rootfolder` |
| Restore a dump | `docker compose exec -T db pg_restore -U rootfolder -d rootfolder --clean < backups/<file>.dump` |

## Removing the mock, before the real app

```bash
cd /opt/rootfolder
docker compose down -v          # containers, network and the mock's database
sudo rm -rf /mnt/storage/pdfs/* # its PDFs
```

The certificate and the server setup stay; the real app's files replace these.
