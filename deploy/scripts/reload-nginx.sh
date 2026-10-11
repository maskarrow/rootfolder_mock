#!/usr/bin/env bash
# Applies a changed nginx configuration only if it is valid.
#
#   scripts/reload-nginx.sh
#
# Tests the configuration in a throwaway container first; the running nginx is
# not touched unless the test passes. Recreating nginx with a broken file would
# take the whole site down until someone fixes it by hand.

set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.." # /opt/rootfolder

if ! docker compose run --rm --no-deps nginx nginx -t; then
    echo "The nginx configuration is invalid; the running nginx was not touched." >&2
    exit 1
fi

docker compose up -d --force-recreate nginx
echo "nginx recreated with the new configuration."
