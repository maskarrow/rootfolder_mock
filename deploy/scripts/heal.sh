#!/usr/bin/env bash
# Restarts the containers of this stack that Docker reports as unhealthy.
#
# Docker restarts a container that exits (`restart: unless-stopped`), but not one
# that keeps running while failing its health check: a hung process stays down
# until someone notices. A systemd timer (systemd/rootfolder-heal.timer) runs this
# every minute. Every restart is written to the system journal:
#   journalctl -t rootfolder-heal
#
# The health checks are chosen so an outage elsewhere does not cause restarts:
# the api's checks /health/live, which does not touch the database.

set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.." # /opt/rootfolder

# Not during a deploy, which restarts containers itself. Held only for a moment;
# deploy.sh waits for it.
exec 9>.deploy.lock
flock -n 9 || exit 0

for id in $(docker compose ps -q); do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id")"
    if [[ "$health" == "unhealthy" ]]; then
        name="$(docker inspect --format '{{.Name}}' "$id")"
        logger -t rootfolder-heal "restarting unhealthy container ${name#/}"
        docker restart "$id" >/dev/null
    fi
done
