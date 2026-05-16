"""Round-trip and integrity tests for db.models."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from postcheck.db.models import Bug, Organization, Project, Run
from postcheck.db.session import create_engine, init_db, session_factory


@pytest.fixture
async def project_root(tmp_path: Path) -> Path:
    await init_db(tmp_path)
    return tmp_path


@pytest.fixture
async def session(project_root: Path):
    engine = create_engine(project_root)
    maker = session_factory(engine)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _make_org(session) -> Organization:
    org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return org


async def _make_project(session, org_id) -> Project:
    p = Project(
        org_id=org_id,
        name="demo",
        local_path=f"/tmp/{uuid4().hex}",
        default_adapter="react_vite",
        config_overrides={"timeout_ms": 5000, "tags": ["a", "b"]},
    )
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p


async def _make_run(session, org_id, project_id) -> Run:
    r = Run(
        org_id=org_id,
        project_id=project_id,
        status="running",
        since_ref="HEAD~1",
        started_at=datetime.now(timezone.utc),
        total_bugs=0,
        report_json={"routes": [{"path": "/", "bugs": []}]},
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return r


async def test_organization_round_trip(session) -> None:
    org = await _make_org(session)
    fetched = (await session.exec(select(Organization).where(Organization.id == org.id))).one()
    assert fetched.name == "Acme"
    assert fetched.slug.startswith("acme-")
    assert fetched.created_at.tzinfo is not None


async def test_project_json_column_serializes(session) -> None:
    org = await _make_org(session)
    p = await _make_project(session, org.id)
    fetched = (await session.exec(select(Project).where(Project.id == p.id))).one()
    assert fetched.config_overrides == {"timeout_ms": 5000, "tags": ["a", "b"]}
    assert fetched.default_adapter == "react_vite"


async def test_run_report_json_round_trips(session) -> None:
    org = await _make_org(session)
    p = await _make_project(session, org.id)
    r = await _make_run(session, org.id, p.id)
    fetched = (await session.exec(select(Run).where(Run.id == r.id))).one()
    assert fetched.report_json == {"routes": [{"path": "/", "bugs": []}]}
    assert fetched.status == "running"
    assert fetched.since_ref == "HEAD~1"
    assert fetched.finished_at is None


async def test_bug_round_trip(session) -> None:
    org = await _make_org(session)
    p = await _make_project(session, org.id)
    r = await _make_run(session, org.id, p.id)
    bug = Bug(
        org_id=org.id,
        run_id=r.id,
        probe="runtime",
        route="/",
        interaction_summary="click [data-testid=add]",
        error_message="TypeError: x is undefined",
        suspected_file="src/cart.ts",
        suspected_line=42,
        confidence="deterministic",
        raw_event={"stack": "..."},
    )
    session.add(bug)
    await session.commit()
    await session.refresh(bug)

    fetched = (await session.exec(select(Bug).where(Bug.id == bug.id))).one()
    assert fetched.raw_event == {"stack": "..."}
    assert fetched.confidence == "deterministic"
    assert fetched.suspected_line == 42


async def test_foreign_keys_enforced_on_bug(session) -> None:
    """SQLite FK PRAGMA must reject bugs pointing at nonexistent runs/orgs."""
    bug = Bug(
        org_id=uuid4(),
        run_id=uuid4(),
        probe="runtime",
        route="/",
        interaction_summary="x",
        error_message="x",
        confidence="deterministic",
        raw_event={},
    )
    session.add(bug)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_foreign_keys_enforced_on_project(session) -> None:
    p = Project(
        org_id=uuid4(),
        name="orphan",
        local_path=f"/tmp/{uuid4().hex}",
        config_overrides={},
    )
    session.add(p)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_org_id_required_on_project(session) -> None:
    """org_id is NOT NULL at the DB layer; insertion without it fails."""
    p = Project(
        name="no-org",
        local_path=f"/tmp/{uuid4().hex}",
        config_overrides={},
    )
    session.add(p)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_org_id_required_on_run(session) -> None:
    org = await _make_org(session)
    proj = await _make_project(session, org.id)
    r = Run(
        project_id=proj.id,
        status="running",
        started_at=datetime.now(timezone.utc),
        report_json={},
    )
    session.add(r)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_org_id_required_on_bug(session) -> None:
    org = await _make_org(session)
    proj = await _make_project(session, org.id)
    run = await _make_run(session, org.id, proj.id)
    bug = Bug(
        run_id=run.id,
        probe="runtime",
        route="/",
        interaction_summary="x",
        error_message="x",
        confidence="deterministic",
        raw_event={},
    )
    session.add(bug)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
