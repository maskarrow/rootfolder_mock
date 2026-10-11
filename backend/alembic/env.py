"""Alembic environment: the URL comes from the app's settings, so migrations run
against whatever `DATABASE_URL` the environment gives (the local `.env`, the test
database, or the container's environment in a deploy)."""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401  (registers every model)
from app.config import settings
from app.db import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    # `False`, or every app logger created before this point (tests import the app
    # first) would be silenced for the rest of the process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


_LOCK_TIMEOUT = os.environ.get("MIGRATION_LOCK_TIMEOUT", "5s")


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # A migration that has to wait for a table (behind a long query or a backup)
        # gives up after this rather than hold its place in the lock queue, where
        # every app query on that table would wait behind it: the site would hang.
        # deploy/scripts/migrate.py tries again. No statement timeout: a migration
        # may rightly run for minutes (an index on a large table).
        connect_args={"options": f"-c lock_timeout={_LOCK_TIMEOUT}"},
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
