"""Alembic environment — wired to db.get_engine() so the same migrations run
against SQLite (pilot) and Postgres (cutover) with no config edits.

DATABASE_URL drives the target. If unset, db._database_url() falls back to the
repo-local SQLite DB, matching `python qa_dashboard.py` behavior on Windows.
"""
from logging.config import fileConfig
import os
import sys

from alembic import context

# Make the app package importable so `import db` works no matter where alembic
# is invoked from (repo root, CI, or a Docker container).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, '..')))

import db as _db  # noqa: E402  (sys.path manipulation must come first)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# We don't use declarative models yet — schema is managed via hand-written
# migrations captured from db.create_fresh_db_schema() + the ALTER TABLE tail
# of db.ensure_tables(). `target_metadata = None` disables autogenerate against
# a declarative Base; revisions are authored manually.
target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`alembic upgrade --sql`)."""
    url = str(_db.get_engine().url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection driven by db.get_engine()."""
    connectable = _db.get_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
