"""Alembic environment.

Migrations always run against a synchronous URL — Alembic's command API is
sync, and using a sync driver for migrations is the standard recommendation
even when the application runs async.

For SQLite we enable foreign keys via PRAGMA so referential integrity is
enforced when running migrations and tests against the dev DB.
"""
from __future__ import annotations

import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, event, pool
from sqlmodel import SQLModel

# Import models so SQLModel.metadata is populated.
from postcheck.db import models  # noqa: F401

config = context.config

# Allow overriding the URL from the environment — used by ``init_db`` to
# point Alembic at a per-project SQLite file and by tests to point at a
# temp DB without rewriting alembic.ini.
_url_override = os.environ.get("POSTCHECK_ALEMBIC_URL")
if _url_override:
    config.set_main_option("sqlalchemy.url", _url_override)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def _enable_sqlite_fk(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=(url or "").startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if connectable.dialect.name == "sqlite":
        event.listen(connectable, "connect", _enable_sqlite_fk)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connectable.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
