"""Markdown + JSON reporters (v0).

The reporter is the single rendering layer for verification output. The
CLI prints :func:`to_markdown` to stdout, the API and MCP server return
:func:`to_json` over the wire, and :func:`write_report` persists both
under ``.postcheck/runs/<run_id>/`` for later inspection.

The schemas are deliberately small and stable — the wrappers (CLI, MCP,
API) hand whatever ``run_metadata`` they have to the reporter and trust
it to render. Only ``run_id`` is treated specially (used to build the
output path); every other key flows through unchanged.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..core.types import Bug, ProbeName

# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def to_markdown(
    bugs: Iterable[Bug], run_metadata: Mapping[str, Any] | None = None
) -> str:
    """Render ``bugs`` as a human-readable Markdown report."""
    metadata = dict(run_metadata or {})
    bug_list = list(bugs)
    lines: list[str] = []
    lines.append(_render_header(metadata, bug_list))
    lines.append("")

    if not bug_list:
        lines.append("**Status:** ✓ Clean — no bugs detected.")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    summary = _summarize(bug_list)
    lines.append(f"**Status:** ✗ {summary['total']} bug(s) detected.")
    lines.append("")
    lines.append("| Severity | Count |")
    lines.append("| --- | ---: |")
    for sev, count in _ordered_severity(summary["by_severity"]):
        lines.append(f"| {sev} | {count} |")
    lines.append("")
    lines.append("| Probe | Count |")
    lines.append("| --- | ---: |")
    for probe, count in sorted(summary["by_probe"].items()):
        lines.append(f"| {probe} | {count} |")
    lines.append("")

    for route, route_bugs in _group_by_route(bug_list):
        lines.append(f"## Route `{route or '(unscoped)'}`")
        lines.append("")
        for probe, probe_bugs in _group_by_probe(route_bugs):
            lines.append(f"### {probe} probe ({len(probe_bugs)})")
            lines.append("")
            for bug in probe_bugs:
                lines.extend(_render_bug(bug))
                lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def to_json(
    bugs: Iterable[Bug], run_metadata: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Render ``bugs`` as a JSON-serialisable dict."""
    metadata = dict(run_metadata or {})
    bug_list = list(bugs)
    summary = _summarize(bug_list)
    return {
        "run": _json_safe(metadata),
        "summary": {
            "total": summary["total"],
            "by_probe": dict(sorted(summary["by_probe"].items())),
            "by_severity": dict(_ordered_severity(summary["by_severity"])),
            "by_route": dict(sorted(summary["by_route"].items())),
        },
        "bugs": [bug.model_dump(mode="json") for bug in bug_list],
    }


def write_report(
    bugs: Iterable[Bug],
    run_metadata: Mapping[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    """Persist the Markdown + JSON reports under ``<output_dir>/runs/<run_id>/``.

    ``output_dir`` is the root ``.postcheck`` directory (or any other
    location callers want). The function creates ``runs/<run_id>/``
    beneath it (parents OK), writes ``report.md`` and ``report.json``,
    and returns their paths.
    """
    metadata = dict(run_metadata or {})
    run_id = _safe_run_id(metadata.get("run_id"))
    target = Path(output_dir) / "runs" / run_id
    target.mkdir(parents=True, exist_ok=True)

    md_path = target / "report.md"
    json_path = target / "report.json"

    bug_list = list(bugs)
    md_path.write_text(to_markdown(bug_list, metadata), encoding="utf-8")
    json_path.write_text(
        json.dumps(to_json(bug_list, metadata), indent=2, default=_json_default),
        encoding="utf-8",
    )
    return {"markdown": md_path, "json": json_path}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


def _summarize(bugs: list[Bug]) -> dict[str, Any]:
    by_probe: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_route: dict[str, int] = {}
    for bug in bugs:
        by_probe[bug.probe] = by_probe.get(bug.probe, 0) + 1
        by_severity[bug.severity] = by_severity.get(bug.severity, 0) + 1
        by_route[bug.route] = by_route.get(bug.route, 0) + 1
    return {
        "total": len(bugs),
        "by_probe": by_probe,
        "by_severity": by_severity,
        "by_route": by_route,
    }


def _ordered_severity(counts: dict[str, int]) -> list[tuple[str, int]]:
    seen = set(counts)
    ordered = [(s, counts[s]) for s in _SEVERITY_ORDER if s in seen]
    # Append any unknown severities deterministically at the end.
    extras = sorted(s for s in seen if s not in _SEVERITY_ORDER)
    ordered.extend((s, counts[s]) for s in extras)
    return ordered


def _group_by_route(bugs: list[Bug]) -> list[tuple[str, list[Bug]]]:
    routes: dict[str, list[Bug]] = {}
    for bug in bugs:
        routes.setdefault(bug.route, []).append(bug)
    return sorted(routes.items(), key=lambda kv: kv[0])


def _group_by_probe(
    bugs: list[Bug],
) -> list[tuple[ProbeName, list[Bug]]]:
    probes: dict[ProbeName, list[Bug]] = {}
    for bug in bugs:
        probes.setdefault(bug.probe, []).append(bug)
    # Sort by probe name for stable rendering.
    return sorted(probes.items(), key=lambda kv: kv[0])


def _render_header(
    metadata: Mapping[str, Any], bugs: list[Bug]
) -> str:
    title = "# Postcheck verification report"
    bits: list[str] = [title, ""]
    if (run_id := metadata.get("run_id")):
        bits.append(f"- **Run ID:** `{run_id}`")
    if (project := metadata.get("project")):
        bits.append(f"- **Project:** {project}")
    if (started := metadata.get("started_at")):
        bits.append(f"- **Started:** {_fmt_datetime(started)}")
    if (finished := metadata.get("finished_at")):
        bits.append(f"- **Finished:** {_fmt_datetime(finished)}")
    if (status := metadata.get("status")):
        bits.append(f"- **Status:** {status}")
    bits.append(f"- **Bugs:** {len(bugs)}")
    return "\n".join(bits)


def _render_bug(bug: Bug) -> list[str]:
    out: list[str] = []
    out.append(
        f"- **[{bug.severity}]** {bug.title}  "
        f"_(confidence: {bug.confidence})_"
    )
    if bug.suspected_location is not None:
        loc = bug.suspected_location
        out.append(f"  - suspected: `{loc.file}:{loc.line}`")
    if bug.interaction is not None:
        sel = bug.interaction.selector
        sel_str = (
            f"{sel.strategy}={sel.value!r}" if sel is not None else "(none)"
        )
        out.append(
            f"  - interaction: `{bug.interaction.kind}` on {sel_str}"
        )
    if bug.detail:
        for line in bug.detail.splitlines():
            # Skip whitespace-only lines outright — rendering them as
            # ``"  -"`` produces orphan bullet rows in the Markdown
            # output, which the no-empty-lines regression test in
            # ``tests/unit/test_reporting/test_reporter.py`` forbids.
            stripped = line.strip()
            if not stripped:
                continue
            out.append(f"  - {stripped}")
    return out


_RUN_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_run_id(value: Any) -> str:
    """Make ``run_id`` safe to use as a directory name; default to ``unknown``."""
    if value is None:
        return "unknown"
    sanitised = _RUN_ID_SAFE.sub("_", str(value))
    # Strip leading dots so ``..`` and ``.`` can't escape the runs directory.
    sanitised = sanitised.lstrip(".")
    return sanitised or "unknown"


def _fmt_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _json_safe(obj: Any) -> Any:
    """Recursively coerce ``Path`` / ``datetime`` to JSON-friendly types."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


__all__ = ["to_json", "to_markdown", "write_report"]
