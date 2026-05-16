"""``postcheck runs list`` and ``postcheck runs show`` (v0).

Read-only inspection of persisted runs. All queries scoped to the default
org (CLAUDE.md principle 8). Run ids may be passed as full UUIDs or as
unambiguous short prefixes; ambiguity raises so the user can disambiguate.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import typer

from ...db.repository import (
    get_or_create_default_org,
    get_project_by_path,
    list_projects,
    list_runs,
)
from ...db.session import create_engine, session_factory
from ..utils import AmbiguousRunIdError, find_project_root, resolve_run_id

# ---------------------------------------------------------------------------
# Shared output helpers
# ---------------------------------------------------------------------------


def _colors_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _fmt_started(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_duration(started: datetime | None, finished: datetime | None) -> str:
    if started is None or finished is None:
        return "-"
    delta = (finished - started).total_seconds()
    if delta < 1.0:
        return f"{int(delta * 1000)}ms"
    if delta < 60.0:
        return f"{delta:.1f}s"
    m, s = divmod(int(delta), 60)
    return f"{m}m{s:02d}s"


_STATUS_COLORS = {
    "succeeded": typer.colors.GREEN,
    "failed": typer.colors.RED,
    "errored": typer.colors.BRIGHT_RED,
    "running": typer.colors.YELLOW,
}


def _fmt_status(status: str) -> str:
    if not _colors_enabled():
        return status
    color = _STATUS_COLORS.get(status, typer.colors.WHITE)
    return typer.style(status, fg=color)


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
    plain_rows = [[_strip_ansi(c) for c in row] for row in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in plain_rows))
        for i, h in enumerate(headers)
    ]
    lines: list[str] = []
    lines.append("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    lines.append("  ".join("-" * w for w in widths))
    for row, plain in zip(rows, plain_rows):
        padded: list[str] = []
        for i, cell in enumerate(row):
            pad = widths[i] - len(plain[i])
            padded.append(cell + (" " * pad if pad > 0 else ""))
        lines.append("  ".join(padded))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Async drivers
# ---------------------------------------------------------------------------


async def _list_runs_async(
    project_root: Path,
    *,
    project_filter: Path | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    engine = create_engine(project_root)
    try:
        async with session_factory(engine)() as session:
            org = await get_or_create_default_org(session)

            project_id = None
            if project_filter is not None:
                proj = await get_project_by_path(
                    session, org_id=org.id, local_path=project_filter
                )
                if proj is None:
                    raise typer.BadParameter(
                        f"no project registered for path {project_filter}"
                    )
                project_id = proj.id

            runs = await list_runs(
                session, org_id=org.id, project_id=project_id, limit=limit
            )
            projects = {p.id: p for p in await list_projects(session, org_id=org.id)}

            return [
                {
                    "id": str(r.id),
                    "project": (projects[r.project_id].name if r.project_id in projects else "(deleted)"),
                    "started": r.started_at,
                    "finished": r.finished_at,
                    "status": r.status,
                    "bugs": r.total_bugs,
                }
                for r in runs
            ]
    finally:
        await engine.dispose()


async def _show_run_async(
    project_root: Path, short_id: str
) -> tuple[dict[str, Any], str] | None:
    engine = create_engine(project_root)
    try:
        async with session_factory(engine)() as session:
            org = await get_or_create_default_org(session)
            run = await resolve_run_id(session, short_id, org_id=org.id)
            if run is None:
                return None
            projects = {p.id: p for p in await list_projects(session, org_id=org.id)}
            proj = projects.get(run.project_id)
            return run.report_json or {}, (proj.name if proj else "(deleted)")
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _require_project_root(explicit: Path | None) -> Path:
    if explicit is not None:
        if not (explicit / ".postcheck").is_dir():
            typer.echo(
                f"error: {explicit} is not a postcheck project "
                "(no .postcheck/ found). Run 'postcheck init' first.",
                err=True,
            )
            raise typer.Exit(code=1)
        return explicit.resolve()
    found = find_project_root()
    if found is None:
        typer.echo("no postcheck project here, run 'postcheck init'", err=True)
        raise typer.Exit(code=1)
    return found


def list_cmd(
    project: Optional[Path] = typer.Option(
        None,
        "--project",
        help=(
            "Filter to the project at this path (must already be registered). "
            "Defaults to the project at cwd."
        ),
        resolve_path=True,
        file_okay=False,
    ),
    limit: int = typer.Option(
        20, "--limit", "-n", help="Maximum number of runs to show.", min=1
    ),
    all_projects: bool = typer.Option(
        False, "--all", help="Show runs across every project in the org."
    ),
) -> None:
    """List recent verification runs."""
    if all_projects and project is not None:
        typer.echo("error: --all and --project are mutually exclusive", err=True)
        raise typer.Exit(code=3)

    project_root = _require_project_root(None)

    project_filter: Path | None
    if all_projects:
        project_filter = None
    elif project is not None:
        project_filter = project
    else:
        project_filter = project_root

    try:
        rows = asyncio.run(
            _list_runs_async(
                project_root, project_filter=project_filter, limit=limit
            )
        )
    except typer.BadParameter as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(code=1) from None

    if not rows:
        typer.echo("(no runs yet)")
        raise typer.Exit(code=0)

    table = _render_table(
        ["ID", "PROJECT", "STARTED", "DURATION", "STATUS", "BUGS"],
        [
            [
                str(r["id"])[:8],
                r["project"],
                _fmt_started(r["started"]),
                _fmt_duration(r["started"], r["finished"]),
                _fmt_status(r["status"]),
                str(r["bugs"]),
            ]
            for r in rows
        ],
    )
    typer.echo(table)


def show_cmd(
    run_id: str = typer.Argument(..., help="Full UUID or unambiguous prefix."),
    json_only: bool = typer.Option(
        False, "--json", help="Dump the raw report JSON instead of Markdown."
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the report to a file instead of stdout.",
        resolve_path=True,
        dir_okay=False,
    ),
    project: Optional[Path] = typer.Option(
        None,
        "--project",
        help="Project root to read the DB from (default: walk up from cwd).",
        resolve_path=True,
        file_okay=False,
    ),
) -> None:
    """Render the persisted report for a single run."""
    project_root = _require_project_root(project)

    try:
        result = asyncio.run(_show_run_async(project_root, run_id))
    except AmbiguousRunIdError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(code=1) from None

    if result is None:
        typer.echo(f"error: no run matches id '{run_id}'", err=True)
        raise typer.Exit(code=1)

    report_payload, _project_name = result

    if json_only:
        text = json.dumps(report_payload, indent=2, default=str)
    else:
        text = _render_show_markdown(report_payload)

    if output is not None:
        output.write_text(
            text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8"
        )
        if not json_only:
            typer.echo(f"report written to {output}")
    else:
        typer.echo(text)


def _render_show_markdown(payload: dict[str, Any]) -> str:
    """Render a stored report payload back to Markdown.

    ``verify`` persists the output of :func:`reporting.reporter.to_json`,
    which has shape ``{"run": meta, "summary": {...}, "bugs": [...]}``.
    Errored / failed runs may instead persist ``{"error": "...", ...}``.
    """
    if not isinstance(payload, dict):
        return "(empty report)"

    # Pre-rendered markdown (forward-compat) wins if present.
    if isinstance(payload.get("markdown"), str):
        return payload["markdown"]

    lines: list[str] = ["# Postcheck verification report", ""]

    meta = payload.get("run") if isinstance(payload.get("run"), dict) else None
    if meta is not None:
        for key in (
            "run_id",
            "project",
            "since",
            "status",
            "started_at",
            "finished_at",
        ):
            if key in meta:
                lines.append(f"- **{key}:** {meta[key]}")
        lines.append("")

    if "error" in payload:
        lines.append(f"**Error:** {payload['error']}")
        if "error_type" in payload:
            lines.append(f"_Type:_ `{payload['error_type']}`")
        lines.append("")

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else None
    if summary is not None:
        lines.append(f"**Bugs:** {summary.get('total', 0)}")
        for key in ("by_severity", "by_probe", "by_route"):
            section = summary.get(key)
            if isinstance(section, dict) and section:
                lines.append("")
                lines.append(f"### {key.replace('_', ' ').title()}")
                for k, v in section.items():
                    lines.append(f"- {k}: {v}")
        lines.append("")

    bugs = payload.get("bugs")
    if isinstance(bugs, list) and bugs:
        lines.append("## Bugs")
        lines.append("")
        for bug in bugs:
            if not isinstance(bug, dict):
                continue
            title = bug.get("title", "(no title)")
            probe = bug.get("probe", "?")
            route = bug.get("route", "?")
            sev = bug.get("severity", "?")
            lines.append(f"- **[{probe}] {route}** — _{sev}_ — {title}")
            detail = bug.get("detail")
            if detail:
                lines.append(f"  - {detail}")
        lines.append("")

    if len(lines) <= 2:
        lines.append("(empty report)")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["list_cmd", "show_cmd"]
