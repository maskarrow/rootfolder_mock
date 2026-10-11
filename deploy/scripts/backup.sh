#!/usr/bin/env bash
# Nightly dump of the database to the storage volume, kept 14 days.
#
# The database lives on the server's own disk (a Docker volume); the dumps go to
# the Hetzner Volume, a different disk that survives the server being rebuilt. This
# is the first line, not the whole answer: the Volume is in the same data centre,
# and the PDFs on it have no second copy. An off-site copy (PLAN.md 5.13, B2 with
# Object Lock) is the next step.
#
# Each dump is checked to be readable before older ones are removed. The result is
# written to the system journal:  journalctl -t rootfolder-backup

set -euo pipefail
umask 077

cd "$(dirname "$(readlink -f "$0")")/.." # /opt/rootfolder

readonly KEEP_DAYS=14
readonly DEST=/mnt/storage/backups

fail() { logger -t rootfolder-backup -p user.err "FAILED: $*"; echo "FAILED: $*" >&2; exit 1; }

env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }
pg_user="$(env_value POSTGRES_USER)"
pg_db="$(env_value POSTGRES_DB)"

mountpoint -q /mnt/storage || fail "/mnt/storage is not mounted"
mkdir -p "$DEST"

file="$DEST/$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T db pg_dump -U "$pg_user" -Fc "$pg_db" >"$file.partial" ||
    { rm -f "$file.partial"; fail "pg_dump"; }
# A dump that pg_restore cannot list is not a backup.
docker compose exec -T db pg_restore --list <"$file.partial" >/dev/null ||
    { rm -f "$file.partial"; fail "the dump is not readable"; }
mv "$file.partial" "$file"

find "$DEST" -name '*.dump' -mtime +"$KEEP_DAYS" -delete
size="$(du -h "$file" | cut -f1)"
logger -t rootfolder-backup "ok: $file ($size)"
echo "ok: $file ($size)"
