"""Async SQLAlchemy session factory (v0).

Local SQLite via ``aiosqlite``. Database file lives at
``<project_root>/.postcheck/postcheck.db``. ``init_db`` creates the
``.postcheck/`` directory, the database file, and brings the schema up to
the latest Alembic revision. Repeated calls are idempotent.

SQLite gotcha: SQLAlchemy does not enable foreign keys by default. We do it
explicitly via a ``PRAGMA foreign_keys=ON`` event listener on every
connection — both async via the SQLAlchemy engine and sync via the Alembic
migration runner.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel.ext.asyncio.session import AsyncSession

DB_RELATIVE_PATH = Path(".postcheck") / "postcheck.db"


def _enable_sqlite_fk(dbapi_connection: Any, _connection_record: Any) -> None:
    """PRAGMA foreign_keys=ON for every new SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _attach_sqlite_fk_listener(engine: AsyncEngine | Engine) -> None:
    """Register the foreign-keys PRAGMA on the underlying sync engine."""
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if sync_engine.dialect.name != "sqlite":
        return
    if event.contains(sync_engine, "connect", _enable_sqlite_fk):
        return
    event.listen(sync_engine, "connect", _enable_sqlite_fk)


def database_path(project_root: Path | str) -> Path:
    """Return the absolute SQLite path for ``project_root``."""
    return (Path(project_root) / DB_RELATIVE_PATH).resolve()


def sqlite_url(project_root: Path | str) -> str:
    """Return the ``sqlite+aiosqlite://`` URL for ``project_root``."""
    return f"sqlite+aiosqlite:///{database_path(project_root)}"


def create_engine(project_root: Path | str) -> AsyncEngine:
    """Build an ``AsyncEngine`` with the SQLite FK PRAGMA installed."""
    engine = create_async_engine(sqlite_url(project_root), future=True)
    _attach_sqlite_fk_listener(engine)
    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an ``async_sessionmaker`` bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def get_session(project_root: Path | str) -> AsyncIterator[AsyncSession]:
    """Yield an ``AsyncSession`` for ``project_root``.

    The engine is created per call and disposed on exit — fine for CLI
    commands. Long-running wrappers should hold their own engine.
    """
    engine = create_engine(project_root)
    maker = session_factory(engine)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


def _alembic_config(project_root: Path) -> Any:
    """Build an Alembic ``Config`` pointed at the SQLite DB for ``project_root``."""
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    # Override package-relative script_location so it resolves regardless of CWD.
    cfg.set_main_option(
        "script_location",
        str(repo_root / "postcheck" / "db" / "migrations"),
    )
    # Use the synchronous SQLite driver for migrations — Alembic's command API
    # is sync, and our env.py picks up this URL via ``config.get_main_option``.
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{database_path(project_root)}")
    return cfg


async def init_db(project_root: Path | str) -> Path:
    """Create ``.postcheck/`` and the SQLite DB; run ``alembic upgrade head``.

    Idempotent: safe to call repeatedly. Returns the absolute path to the
    SQLite database file.
    """
    import asyncio

    root = Path(project_root).resolve()
    (root / ".postcheck").mkdir(parents=True, exist_ok=True)

    db_path = database_path(root)

    # Run alembic in a thread because its command API is fully synchronous.
    def _upgrade() -> None:
        from alembic import command

        cfg = _alembic_config(root)
        # env.py also honours POSTCHECK_ALEMBIC_URL — set it so direct
        # ``alembic upgrade head`` invocations against the same project
        # behave identically to ``init_db``.
        prev = os.environ.get("POSTCHECK_ALEMBIC_URL")
        os.environ["POSTCHECK_ALEMBIC_URL"] = f"sqlite:///{database_path(root)}"
        try:
            command.upgrade(cfg, "head")
        finally:
            if prev is None:
                os.environ.pop("POSTCHECK_ALEMBIC_URL", None)
            else:
                os.environ["POSTCHECK_ALEMBIC_URL"] = prev

    await asyncio.to_thread(_upgrade)
    return db_path


__all__ = [
    "DB_RELATIVE_PATH",
    "create_engine",
    "database_path",
    "get_session",
    "init_db",
    "session_factory",
    "sqlite_url",
]
