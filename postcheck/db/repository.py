"""Async persistence helpers (v0).

Every query is scoped by ``org_id`` (CLAUDE.md principle 8). The CLI and the
future API/MCP wrappers both go through this module — no duplicated SQL.

v0 ships the helpers the CLI commands actually need; additional functions
(``list_runs``, ``finalize_run``, etc.) will be added as their consumers
land.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .models import Bug, Organization, Project, Run, RunStatus

DEFAULT_ORG_SLUG = "default"
DEFAULT_ORG_NAME = "Default"


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


async def get_or_create_default_org(session: AsyncSession) -> Organization:
    """Return the single v0 default organization, creating it if missing."""
    existing = (
        await session.exec(select(Organization).where(Organization.slug == DEFAULT_ORG_SLUG))
    ).first()
    if existing is not None:
        return existing
    org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return org


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def _normalize_path(local_path: Path | str) -> str:
    return str(Path(local_path).resolve())


async def get_project_by_path(
    session: AsyncSession, *, org_id: UUID, local_path: Path | str
) -> Project | None:
    """Find a Project for ``org_id`` by its absolute local path."""
    path = _normalize_path(local_path)
    result = await session.exec(
        select(Project).where(Project.org_id == org_id, Project.local_path == path)
    )
    return result.first()


async def create_project(
    session: AsyncSession,
    *,
    org_id: UUID,
    name: str,
    local_path: Path | str,
    default_adapter: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> Project:
    """Insert a new Project row scoped to ``org_id`` and return it."""
    project = Project(
        org_id=org_id,
        name=name,
        local_path=_normalize_path(local_path),
        default_adapter=default_adapter,
        config_overrides=config_overrides or {},
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def list_projects(
    session: AsyncSession, *, org_id: UUID
) -> list[Project]:
    """Return all projects in ``org_id`` ordered by created_at ASC."""
    result = await session.exec(
        select(Project)
        .where(Project.org_id == org_id)
        .order_by(Project.created_at.asc())  # type: ignore[attr-defined]
    )
    return list(result.all())


async def get_project_by_id(
    session: AsyncSession, *, org_id: UUID, project_id: UUID
) -> Project | None:
    """Find a Project by its UUID, scoped to ``org_id``."""
    result = await session.exec(
        select(Project).where(
            Project.org_id == org_id, Project.id == project_id
        )
    )
    return result.first()


async def delete_project(
    session: AsyncSession, *, org_id: UUID, project: Project
) -> None:
    """Delete ``project`` (cascades runs + bugs via FK ON DELETE CASCADE)."""
    if project.org_id != org_id:
        raise ValueError("project does not belong to this organization")
    await session.delete(project)
    await session.commit()


__all__ = [
    "DEFAULT_ORG_NAME",
    "DEFAULT_ORG_SLUG",
    "create_project",
    "create_run",
    "delete_project",
    "finalize_run",
    "find_runs_by_id_prefix",
    "get_bugs_for_run",
    "get_or_create_default_org",
    "get_project_by_id",
    "get_project_by_path",
    "get_run",
    "list_projects",
    "list_runs",
    "persist_bugs",
]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def create_run(
    session: AsyncSession,
    *,
    org_id: UUID,
    project_id: UUID,
    since_ref: str | None,
) -> Run:
    """Insert a Run row in ``status='running'`` and return it."""
    run = Run(
        org_id=org_id,
        project_id=project_id,
        status="running",
        since_ref=since_ref,
        started_at=datetime.now(timezone.utc),
        total_bugs=0,
        report_json={},
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def finalize_run(
    session: AsyncSession,
    *,
    org_id: UUID,
    run_id: UUID,
    status: RunStatus,
    total_bugs: int,
    report_json: dict[str, Any],
) -> Run:
    """Mark a Run finished with the given status and serialised report."""
    result = await session.exec(
        select(Run).where(Run.id == run_id, Run.org_id == org_id)
    )
    run = result.one()
    run.status = status
    run.total_bugs = total_bugs
    run.report_json = report_json
    run.finished_at = datetime.now(timezone.utc)
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def persist_bugs(
    session: AsyncSession,
    *,
    org_id: UUID,
    run_id: UUID,
    bugs: list[dict[str, Any]],
) -> None:
    """Insert ``Bug`` rows for ``run_id``.

    ``bugs`` is a list of plain dicts (one per :class:`postcheck.core.types.Bug`)
    matching the :class:`Bug` table's column names. Caller is responsible for
    the translation from the core ``Bug`` shape to this shape — the repository
    layer doesn't import from core.
    """
    if not bugs:
        return
    for payload in bugs:
        session.add(
            Bug(
                org_id=org_id,
                run_id=run_id,
                probe=payload["probe"],
                route=payload["route"],
                interaction_summary=payload.get("interaction_summary", ""),
                error_message=payload.get("error_message", ""),
                suspected_file=payload.get("suspected_file"),
                suspected_line=payload.get("suspected_line"),
                confidence=payload["confidence"],
                raw_event=payload.get("raw_event", {}),
            )
        )
    await session.commit()


async def list_runs(
    session: AsyncSession,
    *,
    org_id: UUID,
    project_id: UUID | None = None,
    limit: int | None = 20,
) -> list[Run]:
    """Return runs in ``org_id`` ordered newest-first.

    If ``project_id`` is None, returns runs across every project in the org.
    ``limit=None`` returns all runs.
    """
    stmt = select(Run).where(Run.org_id == org_id)
    if project_id is not None:
        stmt = stmt.where(Run.project_id == project_id)
    stmt = stmt.order_by(Run.started_at.desc())  # type: ignore[attr-defined]
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.exec(stmt)
    return list(result.all())


async def get_run(
    session: AsyncSession, *, org_id: UUID, run_id: UUID
) -> Run | None:
    """Fetch a Run by full UUID, scoped to ``org_id``."""
    result = await session.exec(
        select(Run).where(Run.org_id == org_id, Run.id == run_id)
    )
    return result.first()


async def find_runs_by_id_prefix(
    session: AsyncSession, *, org_id: UUID, prefix: str
) -> list[Run]:
    """Return every Run in ``org_id`` whose UUID string starts with ``prefix``.

    Empty prefix returns no matches (callers should validate first). Matching is
    done in Python after fetching all runs for the org — fine at v0 scale; a
    LIKE on the cast id column would be dialect-specific (sqlite stores UUIDs
    as 32-char hex without dashes, postgres uses the native uuid type).
    """
    norm = prefix.lower().replace("-", "")
    if not norm:
        return []
    result = await session.exec(
        select(Run).where(Run.org_id == org_id)
    )
    return [
        run
        for run in result.all()
        if str(run.id).lower().replace("-", "").startswith(norm)
    ]


async def get_bugs_for_run(
    session: AsyncSession, *, org_id: UUID, run_id: UUID
) -> list[Bug]:
    """Return bugs attached to ``run_id`` (caller already authorised on org)."""
    result = await session.exec(
        select(Bug)
        .where(Bug.org_id == org_id, Bug.run_id == run_id)
        .order_by(Bug.created_at.asc())  # type: ignore[attr-defined]
    )
    return list(result.all())
