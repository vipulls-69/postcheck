"""Entrypoint for a single verification run (v0).

``run_verification(opts)`` is the pure-core orchestrator: stateless from
the caller's view, returns a :class:`VerifyResult`. Wrappers (CLI, MCP,
API + jobs) handle persistence; the core never touches the database.

Flow
----
1. Load :class:`Settings` for the project root.
2. Diff the working tree against ``opts.since`` -> ``FileChange[]``.
   If no files changed, return a clean result without spinning up a
   browser or even running adapter detection.
3. Compute symbol-level changes from the diff (tree-sitter).
4. Detect a route adapter (or honour ``settings.adapter`` override).
5. Map symbol changes -> ``AffectedRoute[]`` (v0: direct only).
6. If no routes are affected, return a clean result.
7. Build the :class:`ExecutionPlan` via :mod:`planner` (v0: trivial).
8. Attach to the user's Chrome over CDP. ``CDPAttachError`` is the one
   exception we deliberately let bubble up — its message embeds the
   per-OS relaunch command and a wrapped/coerced bug would hide that.
9. For each route in the plan: open a fresh page, locate targets,
   instantiate the configured probes, run the scenario, drain events.
10. Aggregate every event into :class:`Bug` objects.
11. Persist Markdown + JSON reports under
    ``<project_root>/.postcheck/runs/<run_id>/``.
12. Return :class:`VerifyResult` with bugs, report paths, timing, and
    status.

Errors are bug-shaped, not crash-shaped
---------------------------------------
Per-route processing errors (``page.new_page`` failure, locator crash,
scenario explosion, etc.) are caught and emitted as a single
``probe="scenario"`` :class:`Bug` with ``confidence="deterministic"`` so
the run can finish and report the rest. ``CDPAttachError`` is the only
exception that bubbles.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..analysis.ast_symbol_diff import compute_symbol_diff
from ..analysis.diff_analyzer import diff_against
from ..analysis.impact_mapper import map_impact
from ..analysis.route_resolver.registry import detect_adapter
from ..browser import get_browser
from ..browser.scenario_runner import ScenarioRunConfig, run_scenario
from ..browser.target_locator import locate
from ..probes.network_probe import NetworkProbe
from ..probes.runtime_probe import RuntimeProbe
from ..probes.storage_probe import StorageProbe
from ..probes.ui_probe import DomAssertionsProbe
from ..reporting import aggregate_bugs, write_report
from .config import Settings, load_settings
from .errors import CDPAttachError
from .planner import ExecutionPlan, RoutePlan, plan
from .types import (
    AffectedRoute,
    Bug,
    LocationFailure,
    ProbeEvent,
    ProbeName,
    RunStatus,
    VerifyOptions,
    VerifyResult,
)

if TYPE_CHECKING:
    from ..browser.cdp_attach import CDPSession
    from ..probes.shared import ProbeHandler


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_verification(opts: VerifyOptions) -> VerifyResult:
    """Run one verification end-to-end and return its result.

    See module docstring for the flow. ``CDPAttachError`` propagates to
    the caller; everything else is captured in ``result.bugs`` or
    ``result.error``.
    """
    started = _utcnow()
    run_id = str(uuid.uuid4())

    settings = load_settings(opts.project_root)
    exclude_globs = opts.exclude_globs or list(settings.exclude_globs)

    # Steps 2-5: static analysis. Pre-attach errors are typed PostcheckError
    # subclasses; we let them surface via ``result.error`` rather than
    # wrapping them as bugs (a missing git repo isn't a "bug in the code").
    try:
        file_changes = await diff_against(
            opts.project_root,
            since=opts.since,
            exclude_globs=exclude_globs,
        )
    except Exception as exc:
        return _failure_result(run_id, started, exc)

    if not file_changes:
        return _clean_result(
            run_id=run_id,
            started=started,
            settings=settings,
            opts=opts,
            file_changes=[],
            symbol_changes=[],
            affected_routes=[],
            note="no file changes detected",
        )

    try:
        symbol_changes = await compute_symbol_diff(file_changes, opts.project_root)
        adapter = await detect_adapter(opts.project_root, override=settings.adapter)
        affected = await map_impact(symbol_changes, adapter, opts.project_root)
    except Exception as exc:
        return _failure_result(
            run_id, started, exc, file_changes=file_changes
        )

    if opts.routes:
        wanted = set(opts.routes)
        affected = [a for a in affected if a.route in wanted]

    if not affected:
        return _clean_result(
            run_id=run_id,
            started=started,
            settings=settings,
            opts=opts,
            file_changes=file_changes,
            symbol_changes=symbol_changes,
            affected_routes=[],
            note="no affected routes",
        )

    if opts.dry_run:
        return _clean_result(
            run_id=run_id,
            started=started,
            settings=settings,
            opts=opts,
            file_changes=file_changes,
            symbol_changes=symbol_changes,
            affected_routes=affected,
            note="dry run",
        )

    execution = plan(affected)

    # Step 8: acquire a browser session. ``get_browser`` dispatches to CDP
    # attach (default) or Playwright launch based on ``settings.launch_mode``.
    # ``CDPAttachError`` bubbles intentionally — its message embeds the per-OS
    # relaunch command and a wrapped/coerced bug would hide that.
    session = await get_browser(
        settings,
        cdp_endpoint=opts.cdp_endpoint or None,
        storage_state_path=opts.storage_state_path,
    )

    events: list[ProbeEvent] = []
    pre_aggregate_bugs: list[Bug] = []
    try:
        for route_plan in execution.routes:
            route_events, route_bugs = await _run_one_route(
                route_plan,
                session=session,
                settings=settings,
                base_url=settings.base_url,
            )
            events.extend(route_events)
            pre_aggregate_bugs.extend(route_bugs)
    finally:
        try:
            await session.close()
        except Exception:  # pragma: no cover - defensive
            pass

    # Step 10: aggregate. Pre-aggregate bugs (location failures, orchestrator
    # errors) come first so the report leads with execution-level issues.
    bugs: list[Bug] = []
    bugs.extend(pre_aggregate_bugs)
    bugs.extend(
        aggregate_bugs(
            events,
            file_changes=file_changes,
            symbol_changes=symbol_changes,
        )
    )

    finished = _utcnow()
    metadata = _metadata(
        run_id=run_id,
        opts=opts,
        adapter_name=adapter.name,
        affected_routes=affected,
        status="completed",
        started=started,
        finished=finished,
    )
    output_dir = opts.project_root / ".postcheck"
    paths = await asyncio.to_thread(write_report, bugs, metadata, output_dir)

    return VerifyResult(
        status="completed",
        run_id=run_id,
        started_at=started,
        finished_at=finished,
        file_changes=file_changes,
        symbol_changes=symbol_changes,
        affected_routes=affected,
        events=events,
        bugs=bugs,
        report_paths={k: str(v) for k, v in paths.items()},
    )


# ---------------------------------------------------------------------------
# Per-route execution
# ---------------------------------------------------------------------------


async def _run_one_route(
    route_plan: RoutePlan,
    *,
    session: "CDPSession",
    settings: Settings,
    base_url: str,
) -> tuple[list[ProbeEvent], list[Bug]]:
    affected = route_plan.route
    route = affected.route
    url = _join_url(base_url, route)
    events: list[ProbeEvent] = []
    bugs: list[Bug] = []

    try:
        page = await session.context.new_page()
    except CDPAttachError:
        raise
    except Exception as exc:
        bugs.append(_orchestrator_bug(route, "open_page", exc))
        return events, bugs

    try:
        # Pre-flight navigation so ``locate()`` runs against the rendered
        # page rather than ``about:blank``. SPAs (React Router, Vue Router,
        # etc.) only mount the route's DOM after navigation; without this,
        # every selector resolves to count=0 and every route reports a
        # spurious ``no_selector_matched`` bug.
        #
        # ``scenario_runner`` will navigate again under its own probe
        # attachment; that re-navigation is what the probes actually
        # observe. The pre-flight is best-effort — failures are swallowed
        # so the scenario-runner phase can still emit a typed nav event.
        try:
            await page.goto(
                url,
                wait_until="networkidle",
                timeout=settings.timeout_ms,
            )
        except Exception:
            pass

        located = await locate(page, affected)
        for failure in located.failures:
            bugs.append(_location_failure_bug(failure))

        if not located.targets:
            return events, bugs

        probes = _build_probes(route_plan.probes, settings)
        cfg = ScenarioRunConfig(
            route=route,
            url=url,
            timeout_ms=settings.timeout_ms,
        )
        events = await run_scenario(page, located.targets, probes, config=cfg)
    except CDPAttachError:
        raise
    except Exception as exc:
        bugs.append(_orchestrator_bug(route, "scenario", exc))
    finally:
        try:
            await page.close()
        except Exception:  # pragma: no cover - defensive
            pass

    return events, bugs


def _build_probes(
    probe_names: list[ProbeName], settings: Settings
) -> list["ProbeHandler"]:
    out: list[ProbeHandler] = []
    for name in probe_names:
        if name == "runtime":
            out.append(RuntimeProbe())
        elif name == "network":
            out.append(NetworkProbe(settings=settings.network))
        elif name == "storage":
            out.append(StorageProbe())
        elif name == "ui":
            out.append(DomAssertionsProbe())
        # Unknown probe names are silently skipped — the planner is the
        # source of truth and v0 only ever emits the four supported names.
    return out


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def _clean_result(
    *,
    run_id: str,
    started: datetime,
    settings: Settings,
    opts: VerifyOptions,
    file_changes: list,
    symbol_changes: list,
    affected_routes: list,
    note: str,
) -> VerifyResult:
    finished = _utcnow()
    metadata = _metadata(
        run_id=run_id,
        opts=opts,
        adapter_name=None,
        affected_routes=affected_routes,
        status="completed",
        started=started,
        finished=finished,
        note=note,
    )
    output_dir = opts.project_root / ".postcheck"
    try:
        paths = write_report([], metadata, output_dir)
        report_paths = {k: str(v) for k, v in paths.items()}
    except OSError:
        # Non-writable project root (e.g. CI sandbox): swallow — the result
        # is still valid, callers just don't get on-disk reports.
        report_paths = {}
    return VerifyResult(
        status="completed",
        run_id=run_id,
        started_at=started,
        finished_at=finished,
        file_changes=file_changes,
        symbol_changes=symbol_changes,
        affected_routes=affected_routes,
        events=[],
        bugs=[],
        report_paths=report_paths,
    )


def _failure_result(
    run_id: str,
    started: datetime,
    exc: BaseException,
    *,
    file_changes: list | None = None,
) -> VerifyResult:
    return VerifyResult(
        status="failed",
        run_id=run_id,
        started_at=started,
        finished_at=_utcnow(),
        file_changes=list(file_changes or []),
        symbol_changes=[],
        affected_routes=[],
        events=[],
        bugs=[],
        report_paths={},
        error=f"{type(exc).__name__}: {exc}",
    )


def _metadata(
    *,
    run_id: str,
    opts: VerifyOptions,
    adapter_name: str | None,
    affected_routes: list[AffectedRoute],
    status: RunStatus,
    started: datetime,
    finished: datetime,
    note: str | None = None,
) -> dict:
    md: dict = {
        "run_id": run_id,
        "project": str(opts.project_root),
        "since": opts.since,
        "status": status,
        "started_at": started,
        "finished_at": finished,
        "affected_routes": [a.route for a in affected_routes],
    }
    if adapter_name is not None:
        md["adapter"] = adapter_name
    if note is not None:
        md["note"] = note
    return md


def _location_failure_bug(failure: LocationFailure) -> Bug:
    return Bug(
        probe="ui",
        route=failure.route,
        severity="medium",
        confidence="deterministic",
        title=(
            f"No DOM target matched the changed symbols on {failure.route}"
        ),
        detail=failure.detail,
        evidence={
            "reason": failure.reason,
            "attempted": [s.model_dump(mode="json") for s in failure.attempted],
            "changed_symbols": [
                s.model_dump(mode="json") for s in failure.changed_symbols
            ],
        },
    )


def _orchestrator_bug(
    route: str, phase: str, exc: BaseException
) -> Bug:
    return Bug(
        probe="scenario",
        route=route,
        severity="high",
        confidence="deterministic",
        title=f"Orchestrator failure during {phase}",
        detail=f"{type(exc).__name__}: {exc}",
        evidence={
            "phase": phase,
            "error_type": type(exc).__name__,
            "error": str(exc),
        },
    )


def _join_url(base_url: str, route: str) -> str:
    base = base_url.rstrip("/")
    if not route:
        return base or "/"
    if route.startswith(("http://", "https://")):
        return route
    if not route.startswith("/"):
        route = "/" + route
    return base + route


__all__ = ["run_verification"]
