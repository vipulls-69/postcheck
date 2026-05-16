"""Tests for ``postcheck projects list|add|remove``."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from postcheck.cli.main import app
from postcheck.db.repository import (
    create_project,
    create_run,
    finalize_run,
    get_or_create_default_org,
    list_projects,
    persist_bugs,
)
from postcheck.db.session import create_engine, init_db, session_factory

runner = CliRunner()


@pytest.fixture
def initialised(tmp_path: Path) -> Path:
    asyncio.run(init_db(tmp_path))
    return tmp_path


def _projects_in_db(tmp_path: Path) -> list[tuple[str, str]]:
    db = sqlite3.connect(tmp_path / ".postcheck" / "postcheck.db")
    try:
        return db.execute("SELECT name, local_path FROM project").fetchall()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_projects_list_no_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["projects", "list"])
    assert result.exit_code == 1, result.output
    assert "no postcheck project" in result.output.lower()


def test_projects_list_empty(initialised: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(initialised)
    result = runner.invoke(app, ["projects", "list"])
    assert result.exit_code == 0, result.output
    assert "(no projects registered)" in result.output


def test_projects_list_renders_table(
    initialised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _seed() -> None:
        engine = create_engine(initialised)
        try:
            async with session_factory(engine)() as session:
                org = await get_or_create_default_org(session)
                await create_project(
                    session,
                    org_id=org.id,
                    name="alpha",
                    local_path=initialised,
                    default_adapter="react_vite",
                )
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    monkeypatch.chdir(initialised)
    result = runner.invoke(app, ["projects", "list"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "react_vite" in result.output
    assert "ID" in result.output


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_projects_add_with_explicit_path(
    initialised: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(initialised)
    target = tmp_path / "external"
    target.mkdir()
    result = runner.invoke(
        app,
        ["projects", "add", str(target), "--name", "ext", "--adapter", "plain_html"],
    )
    assert result.exit_code == 0, result.output
    assert "registered project 'ext'" in result.output
    rows = _projects_in_db(initialised)
    assert ("ext", str(target.resolve())) in rows


def test_projects_add_defaults_to_cwd(
    initialised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(initialised)
    result = runner.invoke(app, ["projects", "add"])
    assert result.exit_code == 0, result.output
    rows = _projects_in_db(initialised)
    assert any(p == str(initialised.resolve()) for _, p in rows)


def test_projects_add_duplicate_path_fails(
    initialised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(initialised)
    r1 = runner.invoke(app, ["projects", "add"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, ["projects", "add"])
    assert r2.exit_code == 3, r2.output
    assert "already registered" in r2.output.lower()


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def _seed_with_run(initialised: Path) -> str:
    """Insert a project + 1 run with a bug; return project id as str."""
    project_id_holder: dict[str, str] = {}

    async def _seed() -> None:
        engine = create_engine(initialised)
        try:
            async with session_factory(engine)() as session:
                org = await get_or_create_default_org(session)
                p = await create_project(
                    session, org_id=org.id, name="todelete", local_path=initialised
                )
                project_id_holder["id"] = str(p.id)
                r = await create_run(
                    session, org_id=org.id, project_id=p.id, since_ref="HEAD"
                )
                await finalize_run(
                    session,
                    org_id=org.id,
                    run_id=r.id,
                    status="failed",
                    total_bugs=1,
                    report_json={"summary": {"total": 1}},
                )
                await persist_bugs(
                    session,
                    org_id=org.id,
                    run_id=r.id,
                    bugs=[
                        {
                            "probe": "runtime",
                            "route": "/",
                            "confidence": "deterministic",
                            "error_message": "x",
                        }
                    ],
                )
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    return project_id_holder["id"]


def test_projects_remove_by_path_yes(
    initialised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_with_run(initialised)
    monkeypatch.chdir(initialised)
    result = runner.invoke(
        app, ["projects", "remove", str(initialised), "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert _projects_in_db(initialised) == []
    # Cascading delete removed the run + bug.
    db = sqlite3.connect(initialised / ".postcheck" / "postcheck.db")
    try:
        assert db.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM bug").fetchone()[0] == 0
    finally:
        db.close()


def test_projects_remove_by_short_prefix(
    initialised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _seed_with_run(initialised)
    monkeypatch.chdir(initialised)
    result = runner.invoke(app, ["projects", "remove", pid[:8], "--yes"])
    assert result.exit_code == 0, result.output
    assert _projects_in_db(initialised) == []


def test_projects_remove_unknown(
    initialised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(initialised)
    result = runner.invoke(app, ["projects", "remove", "nothing-here", "--yes"])
    assert result.exit_code == 1, result.output
    assert "no project matches" in result.output.lower()


def test_projects_remove_prompts_without_yes_and_aborts(
    initialised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_with_run(initialised)
    monkeypatch.chdir(initialised)
    result = runner.invoke(
        app, ["projects", "remove", str(initialised)], input="n\n"
    )
    assert result.exit_code == 0, result.output
    assert "aborted" in result.output.lower()
    # Still present.
    assert _projects_in_db(initialised) != []


def test_projects_remove_prompts_without_yes_and_confirms(
    initialised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_with_run(initialised)
    monkeypatch.chdir(initialised)
    result = runner.invoke(
        app, ["projects", "remove", str(initialised)], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert _projects_in_db(initialised) == []
