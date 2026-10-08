#!/usr/bin/env bash
# Deploys one release: the api and web images, each pinned by digest.
#
#   deploy.sh [--dry-run] <api-image>@sha256:<digest> <web-image>@sha256:<digest>
#
# GitHub Actions runs it over SSH as the `deploy` user, whose key is locked to this
# script in authorized_keys (`command="..."`). SSH then ignores the command the
# runner sends and passes it here in SSH_ORIGINAL_COMMAND, so the arguments are read
# from there and checked strictly. Run by hand on the server, it takes ordinary
# arguments; deploy.log lists the images of every past release, for rollbacks.
#
# Steps: check the storage mount, pull the images, dump the database, run the
# migrations once with the new image, recreate the containers, wait until
# /health/ready answers through nginx with the new version, and go back to the
# previous images if it does not. The database is never rolled back: migrations
# only add, so the previous release runs on the new schema.

set -euo pipefail

# The only images this script deploys. For another app, change these two lines.
readonly API_RE='^ghcr\.io/maskarrow/rootfolder-mock-api@sha256:[0-9a-f]{64}$'
readonly WEB_RE='^ghcr\.io/maskarrow/rootfolder-mock-web@sha256:[0-9a-f]{64}$'
readonly IMAGES_PREFIX='ghcr.io/maskarrow/rootfolder-mock-'

readonly HEALTH_TRIES=24 # x 5 s = 2 minutes
readonly KEEP_DUMPS=3

cd "$(dirname "$(readlink -f "$0")")/.." # /opt/rootfolder

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# One value from .env, without executing the file as shell.
env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }

# --- Arguments ---------------------------------------------------------------

if [[ -n "${SSH_ORIGINAL_COMMAND:-}" ]]; then
    read -r -a args <<<"$SSH_ORIGINAL_COMMAND"
else
    args=("$@")
fi

dry_run=false
if [[ "${args[0]:-}" == "--dry-run" ]]; then
    dry_run=true
    args=("${args[@]:1}")
fi

if [[ ${#args[@]} -ne 2 || ! "${args[0]}" =~ $API_RE || ! "${args[1]}" =~ $WEB_RE ]]; then
    die "usage: deploy.sh [--dry-run] ${IMAGES_PREFIX}api@sha256:<64 hex> ${IMAGES_PREFIX}web@sha256:<64 hex>"
fi
readonly api_image="${args[0]}" web_image="${args[1]}"

# Two deploys at once would interleave migrations and restarts.
exec 9>.deploy.lock
flock -n 9 || die "another deploy is running"

[[ -f .env ]] || die "/opt/rootfolder/.env is missing"
domain="$(env_value DOMAIN)"
pg_user="$(env_value POSTGRES_USER)"
pg_db="$(env_value POSTGRES_DB)"
storage_mount="$(env_value STORAGE_MOUNT)"
readonly domain pg_user pg_db storage_mount

# --- Before touching anything ------------------------------------------------

# Without the volume, PDFs would land on the server's own disk and vanish with it.
if [[ -n "$storage_mount" ]] && ! mountpoint -q "$storage_mount"; then
    die "$storage_mount is not mounted"
fi

log "Pulling $api_image"
docker pull --quiet "$api_image" >/dev/null
log "Pulling $web_image"
docker pull --quiet "$web_image" >/dev/null

version="$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$api_image" |
    sed -n 's/^APP_VERSION=//p')"
readonly version
[[ -n "$version" ]] || die "the api image has no APP_VERSION"
log "Release $version"

if $dry_run; then
    log "Dry run: images pulled, nothing else changed"
    exit 0
fi

write_override() {
    cat >compose.override.yaml <<EOF
# Written by scripts/deploy.sh: the release running now. Do not edit by hand.
services:
  api:
    image: $1
  web:
    image: $2
EOF
}

# The release running now, to go back to if the new one does not come up.
previous=""
if [[ -f compose.override.yaml ]]; then
    previous="$(cat compose.override.yaml)"
fi

rollback() {
    if [[ -z "$previous" ]]; then
        log "No previous release to go back to"
        return
    fi
    log "Going back to the previous release"
    printf '%s\n' "$previous" >compose.override.yaml
    docker compose up -d --remove-orphans --wait --wait-timeout 120 || log "The previous release did not come up either"
}

# --- Database ----------------------------------------------------------------

docker compose up -d --wait db

mkdir -p backups
dump="backups/$(date -u +%Y%m%dT%H%M%SZ)-before-${version}.dump"
log "Dumping the database to $dump"
docker compose exec -T db pg_dump -U "$pg_user" -Fc "$pg_db" >"$dump"
# The names are this script's own timestamps, so `ls` is safe here.
# shellcheck disable=SC2012
ls -1t backups/*.dump | tail -n +$((KEEP_DUMPS + 1)) | xargs -r rm --

write_override "$api_image" "$web_image"

log "Running the migrations"
if ! docker compose run --rm --no-deps -T api alembic upgrade head; then
    # The containers still run the previous release; only its record changes back.
    [[ -n "$previous" ]] && printf '%s\n' "$previous" >compose.override.yaml
    die "migrations failed; the running release is unchanged"
fi

# --- Containers --------------------------------------------------------------

log "Starting the new containers"
if ! docker compose up -d --remove-orphans --wait --wait-timeout 120; then
    log "The new containers did not become healthy"
    rollback
    die "deploy of $version failed"
fi

log "Waiting for https://$domain/api/health/ready to report $version"
healthy=false
for _ in $(seq "$HEALTH_TRIES"); do
    body="$(curl -fsS --max-time 5 --resolve "$domain:443:127.0.0.1" "https://$domain/api/health/ready" 2>/dev/null || true)"
    if [[ "$body" == *"\"version\":\"$version\""* ]]; then
        healthy=true
        break
    fi
    sleep 5
done

if ! $healthy; then
    log "Last answer: ${body:-none}"
    rollback
    die "deploy of $version failed"
fi

# --- Bookkeeping -------------------------------------------------------------

[[ -n "$previous" ]] && printf '%s\n' "$previous" >compose.override.previous.yaml
printf '%s %s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$version" "$api_image" "$web_image" >>deploy.log

# Keep the images of this release and the previous one; remove older ones.
keep="$(grep -ho "${IMAGES_PREFIX}[a-z]*@sha256:[0-9a-f]*" compose.override.yaml compose.override.previous.yaml 2>/dev/null || true)"
docker image ls --digests --format '{{.Repository}}@{{.Digest}} {{.ID}}' |
    { grep "^${IMAGES_PREFIX}" || true; } |
    while read -r ref id; do
        grep -qxF "$ref" <<<"$keep" || docker image rm "$id" >/dev/null 2>&1 || true
    done

log "Deployed $version"
