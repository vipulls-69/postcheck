"""``postcheck doctor`` — sanity check the local setup."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import socket
import sys
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer

from ..utils import find_project_root

MIN_PYTHON = (3, 11)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _mark(ok: bool) -> str:
    if _color_enabled():
        return typer.style("✓", fg=typer.colors.GREEN) if ok else typer.style(
            "✗", fg=typer.colors.RED
        )
    return "OK " if ok else "FAIL"


def _check_python() -> Check:
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    return Check(
        name=f"Python {v.major}.{v.minor}.{v.micro}",
        ok=ok,
        detail="" if ok else f"requires >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
    )


def _check_git() -> Check:
    path = shutil.which("git")
    return Check(
        name="git available",
        ok=path is not None,
        detail=path or "not found on PATH",
    )


def _check_playwright() -> Check:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError as exc:
        return Check(name="playwright installed", ok=False, detail=str(exc))

    async def _probe() -> tuple[bool, str]:
        from playwright.async_api import async_playwright as _ap

        try:
            async with _ap() as p:
                browser = await p.chromium.launch(headless=True)
                version = browser.version
                await browser.close()
            return True, f"chromium {version}"
        except Exception as exc:  # noqa: BLE001
            return False, (
                f"{type(exc).__name__}: {exc} "
                "(try: python -m playwright install chromium)"
            )

    try:
        ok, detail = asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001 - defensive
        return Check(name="playwright chromium", ok=False, detail=str(exc))
    return Check(name="playwright chromium launchable", ok=ok, detail=detail)


def _check_project_dir(project_root: Path | None) -> Check:
    if project_root is None:
        return Check(
            name=".postcheck/ in cwd or ancestor",
            ok=False,
            detail="run 'postcheck init' in your project",
        )
    pc_dir = project_root / ".postcheck"
    if not pc_dir.is_dir():
        return Check(
            name=".postcheck/ in cwd or ancestor",
            ok=False,
            detail=f"{pc_dir} missing (run 'postcheck init')",
        )
    return Check(
        name=".postcheck/ in cwd or ancestor",
        ok=True,
        detail=str(project_root),
    )


def _check_db(project_root: Path | None) -> Check:
    if project_root is None:
        return Check(name="DB readable", ok=False, detail="no project")
    db = project_root / ".postcheck" / "postcheck.db"
    if not db.is_file():
        return Check(
            name="DB readable", ok=False, detail=f"{db} missing (run 'postcheck init')"
        )
    try:
        conn = sqlite3.connect(db)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check(name="DB readable", ok=False, detail=f"{type(exc).__name__}: {exc}")
    required = {"organization", "project", "run", "bug"}
    missing = required - tables
    if missing:
        return Check(
            name="DB readable",
            ok=False,
            detail=f"missing tables: {', '.join(sorted(missing))}",
        )
    return Check(name="DB readable", ok=True, detail=str(db))


def _check_dev_server(project_root: Path | None) -> Check:
    base_url: str | None = None
    if project_root is not None:
        cfg_path = project_root / ".postcheck" / "config.json"
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                base_url = cfg.get("base_url") or cfg.get("baseUrl")
            except (json.JSONDecodeError, OSError) as exc:
                return Check(
                    name="dev server reachable",
                    ok=False,
                    detail=f"could not parse config.json: {exc}",
                )
    if base_url is None:
        return Check(
            name="dev server reachable",
            ok=False,
            detail="no base_url configured (skipping)",
        )

    # Use a short-timeout HTTP GET. Accept any HTTP status as "reachable".
    try:
        req = urllib.request.Request(base_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return Check(
                name="dev server reachable",
                ok=True,
                detail=f"HEAD {base_url} -> {resp.status}",
            )
    except urllib.error.HTTPError as exc:
        # 4xx/5xx still means *something* is listening.
        return Check(
            name="dev server reachable",
            ok=True,
            detail=f"HEAD {base_url} -> {exc.code}",
        )
    except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
        return Check(
            name="dev server reachable",
            ok=False,
            detail=f"{base_url} unreachable ({exc})",
        )


def doctor(
    project: Optional[Path] = typer.Option(
        None,
        "--project",
        help="Project root to check (default: walk up from cwd).",
        resolve_path=True,
        file_okay=False,
    ),
) -> None:
    """Run setup sanity checks and exit 0 if everything is green, 1 otherwise."""
    project_root = (
        project.resolve() if project is not None else find_project_root()
    )

    checks: list[Check] = [
        _check_python(),
        Check(name=f"Platform {platform.system()} {platform.release()}", ok=True),
        _check_git(),
        _check_playwright(),
        _check_project_dir(project_root),
        _check_db(project_root),
        _check_dev_server(project_root),
    ]

    width = max(len(c.name) for c in checks)
    for c in checks:
        line = f"{_mark(c.ok)}  {c.name.ljust(width)}"
        if c.detail:
            line += f"  — {c.detail}"
        typer.echo(line)

    failed = [c for c in checks if not c.ok]
    if failed:
        typer.echo(
            f"\n{len(failed)} of {len(checks)} checks failed.", err=True
        )
        raise typer.Exit(code=1)
    typer.echo(f"\nAll {len(checks)} checks passed.")


__all__ = ["doctor"]
