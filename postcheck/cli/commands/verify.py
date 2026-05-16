"""``postcheck verify`` — run one verification and persist it (v0).

Exit codes (CLAUDE.md "CLI conventions"):
  0 — clean (verifier ran, no bugs)
  1 — bugs found
  2 — verifier failed or errored
  3 — config/usage error (no project)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

import typer

from ...core.errors import PostcheckError
from ...core.orchestrator import run_verification
from ...core.types import Bug as CoreBug
from ...core.types import VerifyOptions, VerifyResult
from ...db.repository import (
    create_project,
    create_run,
    finalize_run,
    get_or_create_default_org,
    get_project_by_path,
    persist_bugs,
)
from ...db.session import create_engine, session_factory
from ...reporting.reporter import to_json as report_to_json
from ...reporting.reporter import to_markdown as report_to_markdown
from ..utils import find_project_root

DEFAULT_SINCE = "HEAD~1"


# ---------------------------------------------------------------------------
# Color handling — respect NO_COLOR env var (https://no-color.org/).
# ---------------------------------------------------------------------------


def _colors_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


_SEVERITY_COLORS = {
    "critical": typer.colors.BRIGHT_RED,
    "high": typer.colors.RED,
    "medium": typer.colors.YELLOW,
    "low": typer.colors.CYAN,
    "info": typer.colors.BLUE,
}


def _severity_label(severity: str) -> str:
    if not _colors_enabled():
        return severity
    color = _SEVERITY_COLORS.get(severity)
    if color is None:
        return severity
    return typer.style(severity, fg=color, bold=severity in {"critical", "high"})


# ---------------------------------------------------------------------------
# Status mapping: core VerifyResult.status → db RunStatus
# ---------------------------------------------------------------------------


def _map_status(verify_status: str) -> str:
    if verify_status == "completed":
        return "succeeded"
    if verify_status == "failed":
        return "failed"
    return "errored"


def _bug_to_row(bug: CoreBug) -> dict[str, Any]:
    interaction_summary = ""
    if bug.interaction is not None:
        target = bug.interaction.target_label or ""
        interaction_summary = f"{bug.interaction.kind} {target}".strip()
    error_message = bug.title
    if bug.detail:
        error_message = f"{bug.title}: {bug.detail}"
    suspected_file: str | None = None
    suspected_line: int | None = None
    if bug.suspected_location is not None:
        suspected_file = str(bug.suspected_location.file)
        suspected_line = bug.suspected_location.line
    return {
        "probe": bug.probe,
        "route": bug.route,
        "interaction_summary": interaction_summary,
        "error_message": error_message,
        "suspected_file": suspected_file,
        "suspected_line": suspected_line,
        "confidence": bug.confidence,
        "raw_event": bug.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------
# Async driver
# ---------------------------------------------------------------------------


async def _run_async(
    project_root: Path,
    *,
    since: str | None,
    json_only: bool,
) -> tuple[VerifyResult | None, str, str, dict[str, Any]]:
    """Run the verification end-to-end and persist a Run row.

    Returns ``(verify_result_or_none, db_status, run_id_str, report_payload)``.
    """
    engine = create_engine(project_root)
    try:
        async with session_factory(engine)() as session:
            org = await get_or_create_default_org(session)

            project = await get_project_by_path(
                session, org_id=org.id, local_path=project_root
            )
            if project is None:
                project = await create_project(
                    session,
                    org_id=org.id,
                    name=project_root.name or str(project_root),
                    local_path=project_root,
                )

            run_row = await create_run(
                session,
                org_id=org.id,
                project_id=project.id,
                since_ref=since,
            )
            run_id_str = str(run_row.id)
            org_id = org.id
            project_id = project.id

        if not json_only:
            typer.echo(f"postcheck verify — run {run_id_str}")
            typer.echo(f"  project: {project_root}")
            typer.echo(f"  since:   {since or '(working tree)'}")
            typer.echo("  running...")

        opts = VerifyOptions(project_root=project_root, since=since)

        verify_result: VerifyResult | None = None
        db_status = "running"
        report_payload: dict[str, Any] = {}
        try:
            verify_result = await run_verification(opts)
            db_status = _map_status(verify_result.status)
            report_payload = report_to_json(
                verify_result.bugs,
                _result_metadata(verify_result, project_root, since),
            )
            if verify_result.error:
                report_payload["error"] = verify_result.error
        except PostcheckError as exc:
            db_status = "failed"
            report_payload = {
                "error": exc.message,
                "error_type": type(exc).__name__,
                "context": exc.context,
            }
        except Exception as exc:  # pragma: no cover - defensive
            db_status = "errored"
            report_payload = {
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }

        async with session_factory(engine)() as session:
            await finalize_run(
                session,
                org_id=org_id,
                run_id=run_row.id,
                status=db_status,  # type: ignore[arg-type]
                total_bugs=len(verify_result.bugs) if verify_result else 0,
                report_json=report_payload,
            )
            if verify_result is not None and verify_result.bugs:
                await persist_bugs(
                    session,
                    org_id=org_id,
                    run_id=run_row.id,
                    bugs=[_bug_to_row(b) for b in verify_result.bugs],
                )

        _ = project_id
        return verify_result, db_status, run_id_str, report_payload
    finally:
        await engine.dispose()


def _result_metadata(
    result: VerifyResult, project_root: Path, since: str | None
) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "project": str(project_root),
        "since": since,
        "status": result.status,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat() if result.finished_at else None,
        "affected_routes": [a.route for a in result.affected_routes],
    }


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def _print_markdown_report(
    result: VerifyResult,
    project_root: Path,
    since: str | None,
) -> None:
    md = report_to_markdown(result.bugs, _result_metadata(result, project_root, since))
    typer.echo(md)
    if result.bugs and _colors_enabled():
        typer.echo(
            "Severity legend: "
            + ", ".join(
                _severity_label(s)
                for s in ("critical", "high", "medium", "low", "info")
            )
        )


def _write_output(
    output: Path,
    result: VerifyResult | None,
    project_root: Path,
    since: str | None,
    report_payload: dict[str, Any],
) -> None:
    if result is None:
        output.write_text(
            "# postcheck verify\n\nVerifier errored.\n\n"
            f"```\n{report_payload.get('error', '')}\n```\n",
            encoding="utf-8",
        )
    else:
        md = report_to_markdown(
            result.bugs, _result_metadata(result, project_root, since)
        )
        output.write_text(md, encoding="utf-8")
    json_path = (
        output.with_suffix(output.suffix + ".json")
        if output.suffix
        else output.with_suffix(".json")
    )
    json_path.write_text(
        json.dumps(report_payload, indent=2, default=str), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def verify(
    since: Optional[str] = typer.Option(
        DEFAULT_SINCE,
        "--since",
        help="Git ref to diff against (default: HEAD~1).",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write Markdown report to PATH (JSON is written to PATH.json).",
        resolve_path=True,
        dir_okay=False,
    ),
    json_only: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON to stdout instead of Markdown.",
    ),
    project: Optional[Path] = typer.Option(
        None,
        "--project",
        help="Project root (default: walk up from cwd looking for .postcheck/).",
        resolve_path=True,
        file_okay=False,
    ),
) -> None:
    """Run a verification and persist the run."""
    if project is not None:
        if not (project / ".postcheck").is_dir():
            typer.echo(
                f"error: {project} is not a postcheck project "
                "(no .postcheck/ found). Run 'postcheck init' first.",
                err=True,
            )
            raise typer.Exit(code=1)
        project_root = project.resolve()
    else:
        found = find_project_root()
        if found is None:
            typer.echo(
                "no postcheck project here, run 'postcheck init'", err=True
            )
            raise typer.Exit(code=1)
        project_root = found

    result, db_status, run_id, report_payload = asyncio.run(
        _run_async(project_root, since=since, json_only=json_only)
    )

    if output is not None:
        _write_output(output, result, project_root, since, report_payload)
        if not json_only:
            typer.echo(f"report written to {output}")

    if json_only:
        typer.echo(json.dumps(report_payload, indent=2, default=str))
    elif output is None:
        if result is None:
            typer.echo("verifier errored:")
            typer.echo(report_payload.get("error", "(no error message)"))
            if "traceback" in report_payload:
                typer.echo(report_payload["traceback"])
        else:
            if result.status == "completed" and not result.file_changes:
                typer.echo(f"No changes since {since}, nothing to verify.")
            elif (
                result.status == "completed"
                and not result.affected_routes
                and not result.bugs
            ):
                typer.echo("No affected routes — nothing to exercise.")
            else:
                _print_markdown_report(result, project_root, since)
                if result.status != "completed":
                    typer.echo(
                        f"verifier finished with status '{result.status}': "
                        f"{result.error or '(no error message)'}",
                        err=True,
                    )

    _ = run_id
    if db_status in {"failed", "errored"}:
        raise typer.Exit(code=2)
    if result is not None and result.bugs:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


__all__ = ["verify"]
