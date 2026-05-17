"""``postcheck config`` — manage layered configuration (v0).

Subcommands:

  get    — print the effective config (optionally with provenance / by key)
  set    — write a key to project or global config (with --global)
  unset  — remove a key from project or global config
  edit   — open $EDITOR on the project or global config file

Layer hierarchy (lowest → highest precedence):

    DEFAULT  →  GLOBAL  →  PROJECT  →  ENV (POSTCHECK_*)  →  CLI FLAG

GLOBAL config:  ~/.config/postcheck/config.json  (XDG_CONFIG_HOME aware)
PROJECT config: <project>/.postcheck/config.json
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from ...core.config import (
    ConfigLayer,
    Settings,
    all_keys,
    allowed_layers_for,
    assign_nested,
    coerce_value_for_key,
    get_global_config_path,
    get_project_config_path,
    load_config_with_provenance,
    remove_nested,
)
from ...core.errors import ConfigError, PostcheckError
from ..utils import find_project_root


# ---------------------------------------------------------------------------
# Layer help text (printed by `postcheck config --help`)
# ---------------------------------------------------------------------------

LAYER_HELP = """\
Manage layered postcheck configuration.

Layer hierarchy (highest precedence last):

    DEFAULT  →  GLOBAL  →  PROJECT  →  ENV (POSTCHECK_*)  →  CLI FLAG

Files:
  global   ~/.config/postcheck/config.json   (XDG_CONFIG_HOME aware)
  project  <project>/.postcheck/config.json

Examples:
  postcheck config get                       # effective config as JSON
  postcheck config get --show                # with provenance table
  postcheck config get launch_mode           # one key only
  postcheck config set launch_mode launch --global
  postcheck config set base_url http://localhost:3000
  postcheck config set network.ignore_patterns '["**/*.hot-update.*"]'
  postcheck config unset base_url
  postcheck config edit --global
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_nested(data: dict[str, Any], key: str) -> Any:
    parts = key.split(".")
    cur: Any = data
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(key)
        cur = cur[p]
    return cur


def _read_or_empty(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON in {path}: {exc.msg} (line {exc.lineno}, col {exc.colno})",
            context={"path": str(path)},
        ) from exc


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _suggest_close(key: str) -> str:
    matches = difflib.get_close_matches(key, all_keys(), n=3, cutoff=0.5)
    if matches:
        return f" Did you mean: {', '.join(matches)}?"
    keys = ", ".join(all_keys()[:15])
    return f" Valid keys include: {keys}, ..."


def _require_known_key(key: str) -> None:
    try:
        allowed_layers_for(key)
    except KeyError:
        typer.echo(f"error: unknown key '{key}'.{_suggest_close(key)}", err=True)
        raise typer.Exit(code=3)


def _project_config_path_or_error() -> Path:
    root = find_project_root()
    if root is None:
        typer.echo(
            "error: no .postcheck/ directory found above the current dir. "
            "Run `postcheck init` first, or pass --global.",
            err=True,
        )
        raise typer.Exit(code=3)
    return get_project_config_path(root)


# ---------------------------------------------------------------------------
# `postcheck config get`
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _render_provenance_table(
    settings: Settings, provenance: dict[str, ConfigLayer]
) -> str:
    dumped = settings.model_dump(mode="json")
    rows: list[tuple[str, str, str]] = []
    for key in sorted(provenance.keys()):
        try:
            value = _get_nested(dumped, key)
        except KeyError:
            continue
        rows.append((key, _format_value(value), provenance[key].value))
    # Redact secrets in the table.
    redacted_keys = {"database_url", "redis_url", "anthropic_api_key"}
    rows = [
        (k, "<redacted>" if k in redacted_keys and v not in ("None", "") else v, src)
        for (k, v, src) in rows
    ]
    if not rows:
        return ""
    w_key = max(len("KEY"), max(len(r[0]) for r in rows))
    w_val = max(len("VALUE"), max(len(r[1]) for r in rows))
    w_src = max(len("SOURCE"), max(len(r[2]) for r in rows))
    header = f"{'KEY'.ljust(w_key)}  {'VALUE'.ljust(w_val)}  {'SOURCE'.ljust(w_src)}"
    sep = "-" * len(header)
    lines = [header, sep]
    for k, v, src in rows:
        lines.append(f"{k.ljust(w_key)}  {v.ljust(w_val)}  {src.ljust(w_src)}")
    return "\n".join(lines)


def get_cmd(
    key: Optional[str] = typer.Argument(
        None, help="Specific dotted key to print (e.g. network.timeout_ms)."
    ),
    show: bool = typer.Option(
        False, "--show", help="Print effective config with provenance table."
    ),
    global_only: bool = typer.Option(
        False, "--global", help="Ignore project layer (defaults + global only)."
    ),
    project_only: bool = typer.Option(
        False,
        "--project",
        help="Ignore global layer (defaults + project only).",
    ),
) -> None:
    """Print effective config or a single key's value."""
    if global_only and project_only:
        typer.echo("error: --global and --project are mutually exclusive.", err=True)
        raise typer.Exit(code=3)

    project_root: Path | None = None if global_only else find_project_root()
    global_path: Path | None = None
    if project_only:
        # Point loader at a non-existent path so global is skipped.
        global_path = Path("/dev/null/.never.json")
    settings, provenance = load_config_with_provenance(
        project_root, cli_overrides=None, global_path=global_path
    )

    if key is not None:
        _require_known_key(key)
        try:
            value = _get_nested(settings.model_dump(mode="json"), key)
        except KeyError:
            typer.echo(f"error: unknown key '{key}'.{_suggest_close(key)}", err=True)
            raise typer.Exit(code=3)
        layer = provenance.get(key, ConfigLayer.DEFAULT)
        if show:
            typer.echo(f"{key} = {_format_value(value)}  ({layer.value})")
        else:
            typer.echo(_format_value(value))
        return

    if show:
        typer.echo(_render_provenance_table(settings, provenance))
        return

    dumped = settings.model_dump(mode="json")
    # Strip env-only secrets from JSON dump for clarity.
    for k in ("database_url", "redis_url", "anthropic_api_key"):
        dumped.pop(k, None)
    typer.echo(json.dumps(dumped, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# `postcheck config set`
# ---------------------------------------------------------------------------


def _layer_of(global_flag: bool) -> ConfigLayer:
    return ConfigLayer.GLOBAL if global_flag else ConfigLayer.PROJECT


def _target_path(global_flag: bool) -> Path:
    if global_flag:
        return get_global_config_path()
    return _project_config_path_or_error()


def _validate_layer_for_key(key: str, layer: ConfigLayer) -> None:
    allowed = allowed_layers_for(key)
    if layer not in allowed:
        other = "project" if layer is ConfigLayer.GLOBAL else "global"
        typer.echo(
            f"error: '{key}' cannot be set at the {layer.value} layer.\n"
            f"  allowed layers: {', '.join(a.value for a in allowed)}\n"
            f"  hint: try without --global (project) or with --global, "
            f"whichever matches. Currently this key belongs to {other}.",
            err=True,
        )
        raise typer.Exit(code=3)


def _revalidate_merged(layer: ConfigLayer, candidate_payload: dict[str, Any]) -> None:
    """Make sure the candidate file contents lead to a valid merged Settings."""
    project_root = find_project_root()
    # Temporary in-memory simulation: we just re-call load_config_with_provenance,
    # which already reads files from disk. We've already written nothing yet, so
    # we simulate by reading the existing other layer + injecting `candidate_payload`.
    try:
        if layer is ConfigLayer.GLOBAL:
            # Build merged: defaults < candidate(global) < project(disk) < env
            # The loader handles project + env on its own; substitute the global
            # via the `global_path` parameter pointing at a temp file would work,
            # but we can pre-validate by constructing Settings directly. For
            # simplicity, the wider validation happens on the next load. Here we
            # at least sanity-check the candidate against Settings construction.
            from ...core.config import _filter_by_layer  # type: ignore[attr-defined]

            filtered = _filter_by_layer(
                candidate_payload, ConfigLayer.GLOBAL, source="candidate"
            )
            # Try a full Settings build with just filtered fields layered on defaults.
            Settings.model_validate({**Settings().model_dump(), **filtered})
        else:
            from ...core.config import _filter_by_layer  # type: ignore[attr-defined]

            filtered = _filter_by_layer(
                candidate_payload, ConfigLayer.PROJECT, source="candidate"
            )
            Settings.model_validate({**Settings().model_dump(), **filtered})
    except Exception as exc:  # noqa: BLE001 — bubble as user-friendly config error
        typer.echo(f"error: resulting config is invalid: {exc}", err=True)
        raise typer.Exit(code=3)


def set_cmd(
    key: str = typer.Argument(..., help="Dotted key (e.g. network.timeout_ms)."),
    value: str = typer.Argument(
        ..., help="New value. Lists/dicts must be JSON-encoded."
    ),
    global_flag: bool = typer.Option(
        False, "--global", help="Write to the global config (default: project)."
    ),
) -> None:
    """Set <key> = <value> in the project or global config file."""
    _require_known_key(key)
    layer = _layer_of(global_flag)
    _validate_layer_for_key(key, layer)

    try:
        coerced = coerce_value_for_key(key, value)
    except KeyError:
        typer.echo(f"error: unknown key '{key}'.{_suggest_close(key)}", err=True)
        raise typer.Exit(code=3)
    except ConfigError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(code=3)

    path = _target_path(global_flag)
    current = _read_or_empty(path)
    candidate = assign_nested(current, key, coerced)
    _revalidate_merged(layer, candidate)
    _write_json(path, candidate)
    typer.echo(
        f"Set {key} = {_format_value(coerced)} in {layer.value} config ({path})"
    )


# ---------------------------------------------------------------------------
# `postcheck config unset`
# ---------------------------------------------------------------------------


def unset_cmd(
    key: str = typer.Argument(..., help="Dotted key to remove."),
    global_flag: bool = typer.Option(
        False, "--global", help="Operate on the global config (default: project)."
    ),
) -> None:
    """Remove <key> from the project or global config file."""
    _require_known_key(key)
    layer = _layer_of(global_flag)

    path = _target_path(global_flag)
    current = _read_or_empty(path)
    new, removed = remove_nested(current, key)
    if not removed:
        typer.echo(f"no change: {key} was not set in {layer.value} config ({path})")
        return
    _write_json(path, new)

    # Report new effective value.
    project_root = find_project_root()
    settings, provenance = load_config_with_provenance(project_root)
    try:
        new_value = _get_nested(settings.model_dump(mode="json"), key)
        new_layer = provenance.get(key, ConfigLayer.DEFAULT).value
        typer.echo(
            f"Unset; {key} now resolves to {_format_value(new_value)} from {new_layer}"
        )
    except KeyError:
        typer.echo(f"Unset {key}.")


# ---------------------------------------------------------------------------
# `postcheck config edit`
# ---------------------------------------------------------------------------


def _pick_editor() -> list[str]:
    for env_var in ("VISUAL", "EDITOR"):
        cmd = os.environ.get(env_var)
        if cmd:
            return cmd.split()
    if sys.platform == "win32":
        return ["notepad.exe"]
    for candidate in ("nano", "vim", "vi"):
        path = shutil.which(candidate)
        if path:
            return [path]
    return ["vi"]


def edit_cmd(
    global_flag: bool = typer.Option(
        False, "--global", help="Edit the global config (default: project)."
    ),
) -> None:
    """Open $EDITOR on the project or global config file."""
    path = _target_path(global_flag)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}\n", encoding="utf-8")

    while True:
        editor = _pick_editor()
        try:
            rv = subprocess.call([*editor, str(path)])
        except FileNotFoundError as exc:
            typer.echo(f"error: could not launch editor: {exc}", err=True)
            raise typer.Exit(code=3)
        if rv != 0:
            typer.echo(
                f"editor exited with status {rv}; aborting without reload.",
                err=True,
            )
            raise typer.Exit(code=3)

        try:
            payload = _read_or_empty(path)
            # Validate by attempting a full load.
            load_config_with_provenance(find_project_root())
            # Also: warn if any field is at the wrong layer.
            from ...core.config import _filter_by_layer  # type: ignore[attr-defined]

            layer = _layer_of(global_flag)
            _filter_by_layer(payload, layer, source=str(path))
            typer.echo(f"Config updated ({path})")
            return
        except PostcheckError as exc:
            typer.echo(f"error: {exc.message}", err=True)
            if not typer.confirm("Reopen editor to fix?", default=True):
                raise typer.Exit(code=3)
            continue


# ---------------------------------------------------------------------------
# Typer sub-app — registered by cli/main.py
# ---------------------------------------------------------------------------


def build_app() -> typer.Typer:
    app = typer.Typer(help=LAYER_HELP, no_args_is_help=True)
    app.command("get", help="Print effective config or a single key.")(get_cmd)
    app.command("set", help="Write a key/value to project or global config.")(set_cmd)
    app.command("unset", help="Remove a key from project or global config.")(unset_cmd)
    app.command("edit", help="Open $EDITOR on project or global config.")(edit_cmd)
    return app


__all__ = [
    "LAYER_HELP",
    "build_app",
    "edit_cmd",
    "get_cmd",
    "set_cmd",
    "unset_cmd",
]
