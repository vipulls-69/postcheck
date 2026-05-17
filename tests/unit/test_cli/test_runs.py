"""Tests for ``postcheck runs list|show``."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner

from postcheck.cli.main import app
from postcheck.db.repository import (
    create_project,
    create_run,
    finalize_run,
    get_or_create_default_org,
    persist_bugs,
)
from postcheck.db.session import create_engine, init_db, session_factory

runner = CliRunner()


@pytest.fixture
def seeded_project(tmp_path: Path) -> tuple[Path, list[UUID]]:
    """Set up a postcheck project + 3 runs (newest first) + extra project.

    Returns (project_root, [run_ids newest->oldest]).
    """
    asyncio.run(init_db(tmp_path))
    run_ids: list[UUID] = []

    async def _seed() -> None:
        engine = create_engine(tmp_path)
        try:
            async with session_factory(engine)() as session:
                org = await get_or_create_default_org(session)
                p1 = await create_project(
                    session,
                    org_id=org.id,
                    name="alpha",
                    local_path=tmp_path,
                    default_adapter="react_vite",
                )
                p2 = await create_project(
                    session,
                    org_id=org.id,
                    name="beta",
                    local_path=tmp_path / "other",
                    default_adapter="plain_html",
                )

                # 3 runs on p1 with increasing started_at, 1 run on p2.
                base = datetime.now(timezone.utc) - timedelta(hours=1)
                for i, status in enumerate(["succeeded", "failed", "succeeded"]):
                    run = await create_run(
                        session,
                        org_id=org.id,
                        project_id=p1.id,
                        since_ref=f"HEAD~{i}",
                    )
                    # Overwrite started_at deterministically.
                    run.started_at = base + timedelta(minutes=i * 10)
                    session.add(run)
                    await session.commit()
                    await finalize_run(
                        session,
                        org_id=org.id,
                        run_id=run.id,
                        status=status,  # type: ignore[arg-type]
                        total_bugs=2 if status == "failed" else 0,
                        report_json={
                            "run": {"run_id": str(run.id), "status": status},
                            "summary": {
                                "total": 2 if status == "failed" else 0,
                                "by_probe": {"runtime": 1, "network": 1} if status == "failed" else {},
                                "by_severity": {"high": 2} if status == "failed" else {},
                                "by_route": {"/cart": 2} if status == "failed" else {},
                            },
                            "bugs": (
                                [
                                    {
                                        "title": "boom",
                                        "probe": "runtime",
                                        "route": "/cart",
                                        "severity": "high",
                                    }
                                ]
                                if status == "failed"
                                else []
                            ),
                        },
                    )
                    if status == "failed":
                        await persist_bugs(
                            session,
                            org_id=org.id,
                            run_id=run.id,
                            bugs=[
                                {
                                    "probe": "runtime",
                                    "route": "/cart",
                                    "confidence": "deterministic",
                                    "title": "boom",
                                    "detail": "",
                                }
                            ],
                        )
                    run_ids.append(run.id)

                # One run for p2.
                run_p2 = await create_run(
                    session, org_id=org.id, project_id=p2.id, since_ref="HEAD"
                )
                await finalize_run(
                    session,
                    org_id=org.id,
                    run_id=run_p2.id,
                    status="succeeded",
                    total_bugs=0,
                    report_json={"run": {"status": "succeeded"}, "summary": {"total": 0}},
                )
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    # Reverse so first id is newest.
    return tmp_path, list(reversed(run_ids))


# ---------------------------------------------------------------------------
# runs list
# ---------------------------------------------------------------------------


def test_runs_list_no_project_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 1, result.output
    assert "no postcheck project" in result.output.lower()


def test_runs_list_default_shows_cwd_project(
    seeded_project: tuple[Path, list[UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, run_ids = seeded_project
    monkeypatch.chdir(project_root)
    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0, result.output
    assert "ID" in result.output and "PROJECT" in result.output
    # Newest first — first data line starts with first prefix.
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    data = lines[2:]  # skip header + separator
    assert data[0].startswith(str(run_ids[0])[:8])
    # beta project's run is filtered out (default = cwd project only).
    assert "beta" not in result.output
    assert "alpha" in result.output


def test_runs_list_all_shows_across_projects(
    seeded_project: tuple[Path, list[UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, _ = seeded_project
    monkeypatch.chdir(project_root)
    result = runner.invoke(app, ["runs", "list", "--all"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "beta" in result.output


def test_runs_list_limit(
    seeded_project: tuple[Path, list[UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, _ = seeded_project
    monkeypatch.chdir(project_root)
    result = runner.invoke(app, ["runs", "list", "--limit", "1"])
    assert result.exit_code == 0, result.output
    body = [ln for ln in result.output.splitlines() if ln.strip()][2:]
    assert len(body) == 1


def test_runs_list_project_filter(
    seeded_project: tuple[Path, list[UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, _ = seeded_project
    monkeypatch.chdir(project_root)
    # filter to the beta project (path "other" doesn't have a .postcheck dir,
    # but project filter only matches by local_path)
    result = runner.invoke(
        app, ["runs", "list", "--project", str(project_root / "other")]
    )
    assert result.exit_code == 0, result.output
    assert "beta" in result.output
    assert "alpha" not in result.output


def test_runs_list_project_unknown(
    seeded_project: tuple[Path, list[UUID]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_root, _ = seeded_project
    monkeypatch.chdir(project_root)
    bogus = tmp_path / "nope"
    bogus.mkdir()
    result = runner.invoke(app, ["runs", "list", "--project", str(bogus)])
    assert result.exit_code == 1, result.output
    assert "no project registered" in result.output.lower()


def test_runs_list_all_and_project_mutually_exclusive(
    seeded_project: tuple[Path, list[UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, _ = seeded_project
    monkeypatch.chdir(project_root)
    result = runner.invoke(
        app, ["runs", "list", "--all", "--project", str(project_root)]
    )
    assert result.exit_code == 3, result.output


def test_runs_list_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(init_db(tmp_path))
    monkeypatch.chdir(tmp_path)
    # --all bypasses the "cwd must be a registered project" check.
    result = runner.invoke(app, ["runs", "list", "--all"])
    assert result.exit_code == 0, result.output
    assert "(no runs yet)" in result.output


# ---------------------------------------------------------------------------
# runs show
# ---------------------------------------------------------------------------


def test_runs_show_by_full_uuid(
    seeded_project: tuple[Path, list[UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, run_ids = seeded_project
    monkeypatch.chdir(project_root)
    result = runner.invoke(app, ["runs", "show", str(run_ids[1])])
    assert result.exit_code == 0, result.output
    # Failed run is run_ids[1] (middle one).
    assert "boom" in result.output


def test_runs_show_by_short_prefix(
    seeded_project: tuple[Path, list[UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, run_ids = seeded_project
    monkeypatch.chdir(project_root)
    result = runner.invoke(app, ["runs", "show", str(run_ids[0])[:8]])
    assert result.exit_code == 0, result.output


def test_runs_show_json(
    seeded_project: tuple[Path, list[UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, run_ids = seeded_project
    monkeypatch.chdir(project_root)
    result = runner.invoke(app, ["runs", "show", str(run_ids[1]), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["total"] == 2


def test_runs_show_output_file(
    seeded_project: tuple[Path, list[UUID]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, run_ids = seeded_project
    monkeypatch.chdir(project_root)
    out = tmp_path / "report.md"
    result = runner.invoke(
        app, ["runs", "show", str(run_ids[1]), "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert "boom" in out.read_text()


def test_runs_show_unknown_id(
    seeded_project: tuple[Path, list[UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, _ = seeded_project
    monkeypatch.chdir(project_root)
    result = runner.invoke(app, ["runs", "show", "deadbeef"])
    assert result.exit_code == 1, result.output
    assert "no run matches" in result.output.lower()


def test_runs_show_ambiguous_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force collision by stuffing two runs whose UUID share a prefix."""
    from uuid import uuid4
    from postcheck.db.models import Run

    asyncio.run(init_db(tmp_path))

    async def _seed() -> str:
        engine = create_engine(tmp_path)
        try:
            async with session_factory(engine)() as session:
                org = await get_or_create_default_org(session)
                p = await create_project(
                    session, org_id=org.id, name="x", local_path=tmp_path
                )
                # Two runs whose UUIDs both start with 'aaaaaaaa'.
                for _ in range(2):
                    rid = UUID(f"aaaaaaaa{uuid4().hex[8:]}")
                    r = Run(
                        id=rid,
                        org_id=org.id,
                        project_id=p.id,
                        status="succeeded",
                        since_ref="HEAD",
                        started_at=datetime.now(timezone.utc),
                        total_bugs=0,
                        report_json={},
                    )
                    session.add(r)
                await session.commit()
                return "aaaaaaaa"
        finally:
            await engine.dispose()

    prefix = asyncio.run(_seed())
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["runs", "show", prefix])
    assert result.exit_code == 1, result.output
    assert "ambiguous" in result.output.lower()
