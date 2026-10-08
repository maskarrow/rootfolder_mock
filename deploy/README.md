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
                4. runs `alembic upgrade head` once, with the new api image
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
   docker compose run --rm api python create_user.py you@example.com "Your Name" --org Delegate --admin
   docker compose run --rm api python seed.py
   ```

## Every day

| What | Command |
|---|---|
| State of the containers | `docker compose ps` |
| Logs, live | `docker compose logs -f api` (or `web`, `nginx`, `db`) |
| Which release runs | `tail -n 3 deploy.log` |
| A deploy by hand | `sudo -u deploy scripts/deploy.sh <api>@sha256:... <web>@sha256:...` |
| Back to an older release | the same, with the two images from its line in `deploy.log` |
| A changed `.env` | `docker compose up -d` |
| Changed files in `deploy/` | copy them as in step 1, then `docker compose up -d --force-recreate nginx` if nginx changed |
| A database shell | `docker compose exec db psql -U rootfolder rootfolder` |
| Restore a dump | `docker compose exec -T db pg_restore -U rootfolder -d rootfolder --clean < backups/<file>.dump` |

## Removing the mock, before the real app

```bash
cd /opt/rootfolder
docker compose down -v          # containers, network and the mock's database
sudo rm -rf /mnt/storage/pdfs/* # its PDFs
```

The certificate and the server setup stay; the real app's files replace these.
