"""``postcheck projects list|add|remove`` (v0).

Manage project records in the local postcheck DB. v0 typically has a single
project per ``.postcheck/`` directory, but the commands are present for
symmetry with the future API and for users who want to centralise records.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import UUID

import typer

from ...db.models import Project
from ...db.repository import (
    create_project,
    delete_project,
    get_or_create_default_org,
    get_project_by_id,
    get_project_by_path,
    list_projects,
)
from ...db.session import create_engine, session_factory
from ..utils import find_project_root


def _colors_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _fmt_created(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def _strip_ansi(s: str) -> str:
    out: list[str] = []
    in_esc = False
    for ch in s:
        if ch == "\x1b":
            in_esc = True
            continue
        if in_esc:
            if ch == "m":
                in_esc = False
            continue
        out.append(ch)
    return "".join(out)


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    plain = [[_strip_ansi(c) for c in r] for r in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in plain)) for i, h in enumerate(headers)
    ]
    out: list[str] = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
        "  ".join("-" * w for w in widths),
    ]
    for row, p in zip(rows, plain):
        out.append(
            "  ".join(
                cell + (" " * (widths[i] - len(p[i])) if widths[i] - len(p[i]) > 0 else "")
                for i, cell in enumerate(row)
            )
        )
    return "\n".join(out)


def _require_project_root(explicit: Path | None = None) -> Path:
    """Return the postcheck project root. The DB always lives in a project."""
    if explicit is not None and (explicit / ".postcheck").is_dir():
        return explicit.resolve()
    found = find_project_root()
    if found is None:
        typer.echo("no postcheck project here, run 'postcheck init'", err=True)
        raise typer.Exit(code=1)
    return found


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def _list_async(project_root: Path) -> list[Project]:
    engine = create_engine(project_root)
    try:
        async with session_factory(engine)() as session:
            org = await get_or_create_default_org(session)
            return await list_projects(session, org_id=org.id)
    finally:
        await engine.dispose()


def list_cmd() -> None:
    """List projects registered in this postcheck DB."""
    project_root = _require_project_root()
    projects = asyncio.run(_list_async(project_root))
    if not projects:
        typer.echo("(no projects registered)")
        raise typer.Exit(code=0)
    table = _render_table(
        ["ID", "NAME", "LOCAL_PATH", "ADAPTER", "CREATED"],
        [
            [
                str(p.id)[:8],
                p.name,
                p.local_path,
                p.default_adapter or "-",
                _fmt_created(p.created_at),
            ]
            for p in projects
        ],
    )
    typer.echo(table)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


async def _add_async(
    db_root: Path,
    *,
    target_path: Path,
    name: str,
    adapter: str | None,
) -> Project:
    engine = create_engine(db_root)
    try:
        async with session_factory(engine)() as session:
            org = await get_or_create_default_org(session)
            existing = await get_project_by_path(
                session, org_id=org.id, local_path=target_path
            )
            if existing is not None:
                raise typer.BadParameter(
                    f"project already registered for {target_path} "
                    f"(id={str(existing.id)[:8]})"
                )
            return await create_project(
                session,
                org_id=org.id,
                name=name,
                local_path=target_path,
                default_adapter=adapter,
            )
    finally:
        await engine.dispose()


def add_cmd(
    path: Optional[Path] = typer.Argument(
        None,
        help="Project path to register (defaults to current directory).",
        resolve_path=True,
        file_okay=False,
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Human-readable project name (default: directory name)."
    ),
    adapter: Optional[str] = typer.Option(
        None,
        "--adapter",
        help="Default route adapter for this project (e.g. react_vite, plain_html).",
    ),
) -> None:
    """Register a project record (does not create ``.postcheck/`` at the path)."""
    target = (path or Path.cwd()).resolve()
    project_root = _require_project_root()
    project_name = name or target.name or str(target)

    try:
        project = asyncio.run(
            _add_async(
                project_root,
                target_path=target,
                name=project_name,
                adapter=adapter,
            )
        )
    except typer.BadParameter as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(code=3) from None

    typer.echo(f"registered project '{project.name}' (id={str(project.id)[:8]})")


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


async def _resolve_project(
    db_root: Path, ident: str
) -> tuple[Project | None, str | None]:
    """Resolve a project by full UUID, short prefix, or local path."""
    engine = create_engine(db_root)
    try:
        async with session_factory(engine)() as session:
            org = await get_or_create_default_org(session)

            # Try full UUID.
            try:
                full = UUID(ident)
            except ValueError:
                full = None
            if full is not None:
                proj = await get_project_by_id(
                    session, org_id=org.id, project_id=full
                )
                return proj, None

            # Try path.
            candidate_path = Path(ident).expanduser().resolve()
            if candidate_path.exists() or "/" in ident or "\\" in ident:
                proj = await get_project_by_path(
                    session, org_id=org.id, local_path=candidate_path
                )
                if proj is not None:
                    return proj, None

            # Try short id prefix among the org's projects.
            all_projects = await list_projects(session, org_id=org.id)
            matches = [p for p in all_projects if str(p.id).startswith(ident.lower())]
            if len(matches) == 1:
                return matches[0], None
            if len(matches) > 1:
                ids = ", ".join(str(p.id)[:8] for p in matches[:5])
                return None, f"prefix '{ident}' is ambiguous ({ids}...)"
            return None, None
    finally:
        await engine.dispose()


async def _delete_async(db_root: Path, project: Project) -> None:
    engine = create_engine(db_root)
    try:
        async with session_factory(engine)() as session:
            org = await get_or_create_default_org(session)
            # Re-fetch in this session to attach to it before delete.
            attached = await get_project_by_id(
                session, org_id=org.id, project_id=project.id
            )
            if attached is None:
                return
            await delete_project(session, org_id=org.id, project=attached)
    finally:
        await engine.dispose()


def remove_cmd(
    ident: str = typer.Argument(
        ..., help="Project UUID, short id prefix, or local path."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt."
    ),
) -> None:
    """Remove a project record (cascades runs + bugs)."""
    project_root = _require_project_root()
    project, err = asyncio.run(_resolve_project(project_root, ident))
    if err is not None:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(code=1)
    if project is None:
        typer.echo(f"error: no project matches '{ident}'", err=True)
        raise typer.Exit(code=1)

    if not yes:
        confirm = typer.confirm(
            f"Delete project '{project.name}' ({project.local_path}) "
            f"and all of its runs?",
            default=False,
        )
        if not confirm:
            typer.echo("aborted")
            raise typer.Exit(code=0)

    asyncio.run(_delete_async(project_root, project))
    typer.echo(f"removed project '{project.name}' (id={str(project.id)[:8]})")


__all__ = ["add_cmd", "list_cmd", "remove_cmd"]
