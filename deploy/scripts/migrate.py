"""Brings the database up to this image's latest migration, unless it is ahead, then
makes sure the app's own database role exists with only the rights it needs.

deploy.sh pipes this file into the `migrate` service (the api image, with the
database owner's credentials, which the api itself never receives):

    docker compose run --rm --no-deps -T migrate python - < scripts/migrate.py

It is piped rather than baked into the image so that it works with every image,
including an older one when a release is rolled back.

Why not plain `alembic upgrade head`: after a release that added a migration, the
database is at a revision an older image has never heard of, and alembic stops with
"Can't locate revision". Migrations only add (PLAN.md 5.8), so the older release
runs fine on the newer schema; the right thing to do is skip, and say so.

The app's role (APP_DB_USER, APP_DB_PASSWORD): it may read and write rows, nothing
else. It cannot create, alter, drop or truncate tables, cannot touch the migration
history, and is not a superuser, so it cannot run programs on the server through
Postgres (`COPY ... PROGRAM`). A bug that lets SQL through the app is then limited
to rows. The grants are applied on every run, so a table added by a migration is
covered at once; without APP_DB_USER (local development) this part is skipped.
"""

import os
import sys

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from psycopg import sql
from sqlalchemy import inspect, text

from app.db import engine


def migrate() -> None:
    # Run from the image's /app, where alembic.ini lives.
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)

    with engine.connect() as conn:
        if inspect(conn).has_table("alembic_version"):
            applied = list(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
        else:
            applied = []  # a new database

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
        return

    command.upgrade(config, "head")
    print("Migrations are up to date.")


def ensure_app_role(user: str, password: str) -> None:
    role = sql.Identifier(user)
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute("SELECT current_database()")
            database = sql.Identifier(cur.fetchone()[0])
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,))
            verb = "ALTER" if cur.fetchone() else "CREATE"
            cur.execute(
                sql.SQL(
                    verb + " ROLE {} LOGIN PASSWORD {} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(role, sql.Literal(password))
            )
            for statement in (
                "GRANT CONNECT ON DATABASE {db} TO {role}",
                "GRANT USAGE ON SCHEMA public TO {role}",
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
                # Tables created later by migrations (run as the owner) get the same.
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO {role}",
            ):
                cur.execute(sql.SQL(statement).format(db=database, role=role))
            cur.execute("SELECT to_regclass('public.alembic_version')")
            if cur.fetchone()[0] is not None:
                # The migration history is the owner's alone.
                cur.execute(sql.SQL("REVOKE ALL ON alembic_version FROM {}").format(role))
        raw.commit()
    finally:
        raw.close()
    print(f"Database role {user}: rows only, on every table.")


migrate()
app_user = os.environ.get("APP_DB_USER", "")
if app_user:
    app_password = os.environ.get("APP_DB_PASSWORD", "")
    if len(app_password) < 16:
        sys.exit("APP_DB_PASSWORD is missing or shorter than 16 characters.")
    ensure_app_role(app_user, app_password)
engine.dispose()
