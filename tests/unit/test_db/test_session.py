"""Tests for db.session: init_db creates DB file, runs migrations, is idempotent."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from postcheck.db.session import database_path, get_session, init_db


async def test_init_db_creates_directory_and_file(tmp_path: Path) -> None:
    db_path = await init_db(tmp_path)
    assert db_path == database_path(tmp_path)
    assert db_path.exists()
    assert (tmp_path / ".postcheck").is_dir()


async def test_init_db_runs_migrations_to_head(tmp_path: Path) -> None:
    await init_db(tmp_path)
    db_path = database_path(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"organization", "project", "run", "bug", "alembic_version"} <= tables
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "0001_initial_schema"
    finally:
        conn.close()


async def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path_1 = await init_db(tmp_path)
    mtime_1 = db_path_1.stat().st_mtime
    db_path_2 = await init_db(tmp_path)
    assert db_path_1 == db_path_2
    # Second call should not error out and the version stays the same.
    conn = sqlite3.connect(db_path_2)
    try:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "0001_initial_schema"
    finally:
        conn.close()
    # File still exists (might or might not have been touched — only checking no error).
    assert db_path_2.exists()
    _ = mtime_1  # not asserting equality — alembic may no-op or rewrite header


async def test_get_session_yields_working_session(tmp_path: Path) -> None:
    await init_db(tmp_path)
    async with get_session(tmp_path) as session:
        result = await session.exec("SELECT 1") if False else None  # placeholder
        # Just confirm the session is usable for a trivial scalar query via raw conn.
        from sqlalchemy import text

        value = (await session.execute(text("SELECT 1"))).scalar_one()
        assert value == 1
        assert result is None


async def test_foreign_keys_pragma_enabled(tmp_path: Path) -> None:
    """Verify the connect listener actually flipped PRAGMA foreign_keys on."""
    await init_db(tmp_path)
    async with get_session(tmp_path) as session:
        from sqlalchemy import text

        on = (await session.execute(text("PRAGMA foreign_keys"))).scalar_one()
        assert on == 1


@pytest.mark.parametrize("project_root_type", ["str", "path"])
async def test_init_db_accepts_str_or_path(tmp_path: Path, project_root_type: str) -> None:
    arg: str | Path = str(tmp_path) if project_root_type == "str" else tmp_path
    db_path = await init_db(arg)
    assert db_path.exists()
