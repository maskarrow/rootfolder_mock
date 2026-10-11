"""Brings the database up to this image's latest migration, unless it is ahead.

deploy.sh pipes this file into the new api image:

    docker compose run --rm --no-deps -T api python - < scripts/migrate.py

It is piped rather than baked into the image so that it works with every image,
including an older one when a release is rolled back.

Why not plain `alembic upgrade head`: after a release that added a migration, the
database is at a revision an older image has never heard of, and alembic stops with
"Can't locate revision". Migrations only add (PLAN.md 5.8), so the older release
runs fine on the newer schema; the right thing to do is skip, and say so.
"""

import sys

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import inspect, text

from app.db import engine

# Run from the image's /app, where alembic.ini lives.
config = Config("alembic.ini")
script = ScriptDirectory.from_config(config)

with engine.connect() as conn:
    if inspect(conn).has_table("alembic_version"):
        applied = list(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    else:
        applied = []  # a new database
engine.dispose()

unknown = []
for revision in applied:
    try:
        script.get_revision(revision)
    except CommandError:
        unknown.append(revision)

if unknown:
    print(
        f"The database is at {', '.join(unknown)}, newer than this image knows "
        "(an older release on a newer schema). No migrations to run."
    )
    sys.exit(0)

command.upgrade(config, "head")
print("Migrations are up to date.")
