"""``postcheck init [path]`` — bootstrap a project for postcheck (v0)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

import typer

from ...analysis.route_resolver.registry import detect_adapter
from ...core.errors import AdapterDetectionError
from ...db.repository import create_project, get_or_create_default_org, get_project_by_path
from ...db.session import create_engine, database_path, init_db, session_factory

DEFAULT_BASE_URL = "http://localhost:5173"
DEFAULT_TIMEOUT_MS = 30_000


def _default_config(*, base_url: str | None) -> dict[str, Any]:
    """Sensible defaults written to ``.postcheck/config.json`` on init."""
    cfg: dict[str, Any] = {
        "launch_mode": "launch",
        "launch_headless": True,
        "adapter": "auto",
        "timeout_ms": DEFAULT_TIMEOUT_MS,
    }
    if base_url is not None:
        cfg["base_url"] = base_url
    return cfg


async def _bootstrap(
    project_root: Path, base_url: str | None
) -> tuple[Path, str | None, str]:
    """Run the DB + adapter-detection side of init.

    Returns ``(db_path, detected_adapter_name, project_name)``.
    """
    db_path = await init_db(project_root)

    # Try detection — never raise; init should succeed even on unknown shapes.
    detected: str | None = None
    try:
        adapter = await detect_adapter(project_root, override="auto")
        detected = adapter.name
    except AdapterDetectionError:
        detected = None

    project_name = project_root.name or str(project_root)

    engine = create_engine(project_root)
    try:
        async with session_factory(engine)() as session:
            org = await get_or_create_default_org(session)
            existing = await get_project_by_path(
                session, org_id=org.id, local_path=project_root
            )
            if existing is None:
                config_overrides: dict[str, Any] = {}
                if base_url is not None:
                    config_overrides["base_url"] = base_url
                await create_project(
                    session,
                    org_id=org.id,
                    name=project_name,
                    local_path=project_root,
                    default_adapter=detected,
                    config_overrides=config_overrides,
                )
    finally:
        await engine.dispose()

    return db_path, detected, project_name


def init(
    path: Optional[Path] = typer.Argument(
        None,
        help="Project root to initialise (defaults to the current directory).",
        exists=False,
        file_okay=False,
        resolve_path=True,
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Accept all defaults without prompting."
    ),
) -> None:
    """Create ``.postcheck/`` config and DB; seed the default project row.

    Exit codes:
      0 — initialised
      3 — config error (path missing, already initialised)
    """
    project_root = (path or Path.cwd()).resolve()

    if not project_root.exists():
        typer.echo(f"error: path does not exist: {project_root}", err=True)
        raise typer.Exit(code=3)
    if not project_root.is_dir():
        typer.echo(f"error: not a directory: {project_root}", err=True)
        raise typer.Exit(code=3)

    postcheck_dir = project_root / ".postcheck"
    if postcheck_dir.exists():
        typer.echo(
            "error: project already initialised "
            f"({postcheck_dir} exists). Delete it to start over.",
            err=True,
        )
        raise typer.Exit(code=3)

    if yes:
        base_url: str | None = DEFAULT_BASE_URL
    else:
        prompted = typer.prompt(
            "Base URL for the dev server",
            default=DEFAULT_BASE_URL,
            show_default=True,
        )
        base_url = prompted.strip() or None

    postcheck_dir.mkdir(parents=True, exist_ok=False)
    config_path = postcheck_dir / "config.json"
    config_path.write_text(
        json.dumps(_default_config(base_url=base_url), indent=2) + "\n",
        encoding="utf-8",
    )

    db_path, detected, project_name = asyncio.run(
        _bootstrap(project_root, base_url)
    )

    adapter_label = detected if detected is not None else "auto-detect at run time"
    typer.echo("postcheck initialised.")
    typer.echo(f"  project:  {project_name}")
    typer.echo(f"  path:     {project_root}")
    typer.echo(f"  adapter:  {adapter_label}")
    typer.echo(f"  config:   {config_path}")
    typer.echo(f"  database: {db_path}")
    if detected is None:
        typer.echo(
            "  note:     no adapter matched this project shape; "
            "set `adapter` in .postcheck/config.json or rely on auto-detect.",
        )
    # ensure db_path local var stays referenced for ruff
    _ = database_path(project_root)


__all__ = ["init"]
