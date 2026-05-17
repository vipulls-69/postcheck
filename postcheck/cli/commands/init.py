"""``postcheck init [path]`` — bootstrap a project for postcheck (v0).

The written ``.postcheck/config.json`` only contains values that differ from
global + built-in defaults. Global preferences live in
``~/.config/postcheck/config.json`` (see ``postcheck config --help``).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

import typer

from ...analysis.route_resolver.registry import detect_adapter
from ...core.config import (
    ConfigLayer,
    Settings,
    allowed_layers_for,
    get_global_config_path,
    load_config_with_provenance,
)
from ...core.errors import AdapterDetectionError
from ...db.repository import create_project, get_or_create_default_org, get_project_by_path
from ...db.session import create_engine, database_path, init_db, session_factory

DEFAULT_BASE_URL = "http://localhost:5173"

README_TEXT = """\
# .postcheck/

This directory holds project-local postcheck state:

- `config.json` — **project-specific overrides only.** Global preferences live
  in `~/.config/postcheck/config.json` (or `%APPDATA%\\postcheck\\config.json`
  on Windows). See `postcheck config --help`.
- `postcheck.db` — SQLite database of verification runs.
- `postcheck.log` — JSON structured log (secrets redacted).
- `runs/` — per-run reports (markdown + JSON).

To see the full effective configuration with provenance, run:

    postcheck config get --show
"""


def _effective_global_settings() -> tuple[Settings, Path | None]:
    """Load global + default settings (no project). Returns (settings, path-if-exists)."""
    settings, _ = load_config_with_provenance(project_root=None)
    g_path = get_global_config_path()
    return settings, (g_path if g_path.is_file() else None)


def _build_project_config(
    *, base_url: str | None, detected: str | None, global_settings: Settings
) -> dict[str, Any]:
    """Return a config dict containing only project-layer overrides.

    Skips fields already satisfied by global (or defaults) — keeps project
    files minimal and forward-compatible.
    """
    cfg: dict[str, Any] = {}
    if base_url is not None and base_url != global_settings.base_url:
        cfg["base_url"] = base_url

    chosen_adapter: str | None = detected
    # If global has a default_adapter_override and detection agrees, skip writing.
    if chosen_adapter and chosen_adapter == global_settings.default_adapter_override:
        chosen_adapter = None
    if chosen_adapter and chosen_adapter != "auto":
        if ConfigLayer.PROJECT in allowed_layers_for("adapter"):
            cfg["adapter"] = chosen_adapter
    return cfg


async def _bootstrap(
    project_root: Path, base_url: str | None, global_settings: Settings
) -> tuple[Path, str | None, str, dict[str, Any]]:
    db_path = await init_db(project_root)

    detected: str | None = None
    try:
        adapter = await detect_adapter(project_root, override="auto")
        detected = adapter.name
    except AdapterDetectionError:
        detected = None

    project_name = project_root.name or str(project_root)
    config_payload = _build_project_config(
        base_url=base_url, detected=detected, global_settings=global_settings
    )

    engine = create_engine(project_root)
    try:
        async with session_factory(engine)() as session:
            org = await get_or_create_default_org(session)
            existing = await get_project_by_path(
                session, org_id=org.id, local_path=project_root
            )
            if existing is None:
                await create_project(
                    session,
                    org_id=org.id,
                    name=project_name,
                    local_path=project_root,
                    default_adapter=detected,
                    config_overrides=(
                        {"base_url": config_payload["base_url"]}
                        if "base_url" in config_payload
                        else {}
                    ),
                )
    finally:
        await engine.dispose()

    return db_path, detected, project_name, config_payload


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

    global_settings, g_path = _effective_global_settings()

    default_base_url = (
        global_settings.base_url
        if global_settings.base_url != "http://localhost:3000"
        else DEFAULT_BASE_URL
    )
    if yes:
        base_url: str | None = default_base_url
    else:
        prompted = typer.prompt(
            "Base URL for the dev server",
            default=default_base_url,
            show_default=True,
        )
        base_url = prompted.strip() or None

    postcheck_dir.mkdir(parents=True, exist_ok=False)

    db_path, detected, project_name, config_payload = asyncio.run(
        _bootstrap(project_root, base_url, global_settings)
    )

    config_path = postcheck_dir / "config.json"
    config_path.write_text(
        json.dumps(config_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (postcheck_dir / "README.md").write_text(README_TEXT, encoding="utf-8")

    adapter_label = detected if detected is not None else "auto-detect at run time"
    typer.echo("postcheck initialised.")
    typer.echo(f"  project:  {project_name}")
    typer.echo(f"  path:     {project_root}")
    typer.echo(f"  adapter:  {adapter_label}")
    typer.echo(f"  config:   {config_path}")
    typer.echo(f"  database: {db_path}")
    if g_path is not None:
        typer.echo(f"  global config: found at {g_path}")
    else:
        typer.echo("  global config: not set — using built-in defaults")
    if not config_payload:
        typer.echo(
            "  note:     no project-specific overrides written "
            "(global + defaults are sufficient)."
        )
    if detected is None:
        typer.echo(
            "  note:     no adapter matched this project shape; "
            "set `adapter` in .postcheck/config.json or rely on auto-detect.",
        )
    _ = database_path(project_root)


__all__ = ["init"]
