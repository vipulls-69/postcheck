"""Top-level Typer app (v0).

Exit codes:
  0 — success, no bugs
  1 — success with bugs (verify only) or doctor / lookup miss
  2 — verifier failed (browser crash, dev server unreachable, ...)
  3 — invalid usage or configuration error

Configuration precedence (highest first):
  1. Command-line flags
  2. Environment variables prefixed ``POSTCHECK_``
     (nested keys via ``__``, e.g. ``POSTCHECK_NETWORK__IGNORE_PATTERNS``)
  3. ``.postcheck/config.json`` in the project root
  4. Built-in defaults

Logging is silent on stderr by default. Pass ``--verbose`` for human-readable
DEBUG output. Structured JSON logs are always written to
``.postcheck/postcheck.log`` when a project root is found; secrets matched
by ``REDACTION_PATTERNS`` are scrubbed before write.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Optional

import typer

from .. import __version__ as _pkg_version
from ..core.errors import PostcheckError
from ..core.logging import configure_logging
from .commands import doctor as doctor_cmd
from .commands import init as init_cmd
from .commands import projects as projects_cmd
from .commands import runs as runs_cmd
from .commands import verify as verify_cmd
from .utils import find_project_root

app = typer.Typer(
    name="postcheck",
    help=(
        "Postcheck — post-completion verification CLI.\n\n"
        "Exit codes: 0=clean, 1=bugs found, 2=verifier failed, 3=usage/config error.\n"
        "Config precedence: flags > env (POSTCHECK_*) > .postcheck/config.json > defaults.\n"
        "Logs: .postcheck/postcheck.log (JSON, secrets redacted). Pass --verbose for stderr logs."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"postcheck {_pkg_version}")
        raise typer.Exit(code=0)


@app.callback()
def _root(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Path to a postcheck config.json overriding the project's default.",
        exists=False,
        dir_okay=False,
        resolve_path=True,
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Verbose output (full tracebacks, debug logs)."
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """Root callback — stores global options and configures logging."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["verbose"] = verbose

    # Best-effort log setup. If we can find a project root, tee JSON to its
    # .postcheck/postcheck.log; otherwise, only stderr (silent unless verbose).
    project_root = find_project_root()
    try:
        configure_logging(project_root, verbose=verbose)
    except OSError:
        # Couldn't create log dir (e.g. read-only fs). Fall back to stderr-only.
        configure_logging(None, verbose=verbose)


# ---------------------------------------------------------------------------
# Subcommand registration
# ---------------------------------------------------------------------------

app.command("init", help="Initialise a project for postcheck.")(init_cmd.init)
app.command("verify", help="Run a verification pass and persist the run.")(
    verify_cmd.verify
)
app.command(
    "doctor", help="Run setup sanity checks and report a checklist."
)(doctor_cmd.doctor)


runs_app = typer.Typer(help="Inspect past verification runs.")
app.add_typer(runs_app, name="runs")
runs_app.command("list", help="List recent verification runs.")(runs_cmd.list_cmd)
runs_app.command("show", help="Show a single run's report.")(runs_cmd.show_cmd)


projects_app = typer.Typer(help="Manage projects tracked by postcheck.")
app.add_typer(projects_app, name="projects")
projects_app.command("list", help="List registered projects.")(projects_cmd.list_cmd)
projects_app.command("add", help="Register a project record.")(projects_cmd.add_cmd)
projects_app.command(
    "remove", help="Remove a project record (cascades runs + bugs)."
)(projects_cmd.remove_cmd)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _render_postcheck_error(err: PostcheckError) -> None:
    """Print a PostcheckError in human-friendly form (NOT JSON)."""
    data = err.to_dict()
    typer.echo(f"error [{data['code']}]: {data['message']}", err=True)
    context = data.get("context") or {}
    if context:
        typer.echo("context:", err=True)
        for k, v in context.items():
            if isinstance(v, dict):
                typer.echo(f"  {k}:", err=True)
                for kk, vv in v.items():
                    typer.echo(f"    {kk}: {vv}", err=True)
            else:
                typer.echo(f"  {k}: {v}", err=True)


def _is_verbose() -> bool:
    return "--verbose" in sys.argv or "-v" in sys.argv


def _exit_code_for(err: PostcheckError) -> int:
    # ConfigError is a usage problem; everything else is a verifier failure.
    if err.code == "config_error":
        return 3
    return 2


def _write_error_log(exc: BaseException) -> Path | None:
    """Dump an unexpected traceback to ``.postcheck/last_error.log`` if we can."""
    root = find_project_root()
    if root is None:
        return None
    try:
        log_dir = root / ".postcheck"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "last_error.log"
        path.write_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8",
        )
        return path
    except OSError:
        return None


def main() -> None:
    """Console-script entry point.

    Catches :class:`PostcheckError` and renders it cleanly. Unexpected
    exceptions print a one-line summary by default plus a traceback file at
    ``.postcheck/last_error.log``; ``--verbose`` prints the full traceback.
    """
    verbose = _is_verbose()
    try:
        rv = app(standalone_mode=False)
    except PostcheckError as exc:
        _render_postcheck_error(exc)
        raise SystemExit(_exit_code_for(exc)) from None
    except (typer.Abort, KeyboardInterrupt):
        typer.echo("aborted", err=True)
        raise SystemExit(130) from None
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — top-level catch by design
        log_path = _write_error_log(exc)
        if verbose:
            traceback.print_exc()
        else:
            msg = f"unexpected error: {type(exc).__name__}: {exc}"
            typer.echo(msg, err=True)
            if log_path is not None:
                typer.echo(
                    f"full traceback written to {log_path}; rerun with --verbose for details",
                    err=True,
                )
            else:
                typer.echo("rerun with --verbose for details", err=True)
        raise SystemExit(2) from None

    if isinstance(rv, int):
        raise SystemExit(rv)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
