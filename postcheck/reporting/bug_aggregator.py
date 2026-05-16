"""Flat-list bug aggregator (v0).

Translates the raw :class:`ProbeEvent` stream from the scenario runner
into typed :class:`Bug` records the reporter / API / MCP can consume.

v0 behaviour
------------
* **Flat list**, ordered by (route, then event order within the run).
  No deduplication, no causal grouping — that's v1's
  ``causal_grouping`` in :mod:`reporter`.
* **One bug per event** for kinds that represent defects; informational
  events (e.g. ``storage_write`` successes) are dropped.
* **Best-effort file:line attribution** — we string-match each event
  payload against the diff's changed paths; first hit wins. If a stack
  trace exposes a line number we use it, otherwise we point at the
  symbol's start line, otherwise line 1. The provenance tag on the bug
  (``confidence``) tells consumers how much to trust the link.
* **Confidence tags** follow CLAUDE.md:
  ``deterministic`` for hard signals (runtime exceptions, HTTP failures,
  storage quota errors, scenario failures), ``heuristic`` for
  inference-driven kinds (``ui_no_change``, ``ui_overlay_blocks``,
  console warnings).

The aggregator never raises on a malformed payload — unknown event
kinds are silently dropped so a bad probe event never tanks a whole run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..core.types import (
    Bug,
    BugConfidence,
    BugLocation,
    BugSeverity,
    FileChange,
    Interaction,
    ProbeEvent,
    SymbolChange,
)


# Event kinds we drop on the floor — they're successful operations the
# storage probe records for completeness, not defects.
_INFORMATIONAL_KINDS = frozenset({"storage_write"})


@dataclass(frozen=True, slots=True)
class _BugRecipe:
    severity: BugSeverity
    confidence: BugConfidence
    title_template: str  # ``str.format(**payload)`` is not safe; we build manually


# Maps payload ``kind`` -> how to render it as a Bug. Anything not listed
# here is dropped.
_RECIPES: dict[str, _BugRecipe] = {
    # runtime_probe
    "runtime_error": _BugRecipe("high", "deterministic", "Runtime error"),
    "runtime_warning": _BugRecipe("low", "heuristic", "Console warning"),
    "page_crash": _BugRecipe("critical", "deterministic", "Page crashed"),
    # network_probe
    "network_error": _BugRecipe("high", "deterministic", "Network error"),
    "navigation_failure": _BugRecipe(
        "critical", "deterministic", "Navigation failed"
    ),
    # storage_probe
    "storage_quota_error": _BugRecipe(
        "high", "deterministic", "Storage quota exceeded"
    ),
    "storage_serialization_error": _BugRecipe(
        "medium", "deterministic", "Storage serialization failed"
    ),
    # ui_probe
    "ui_no_change": _BugRecipe(
        "low", "heuristic", "Interaction had no visible effect"
    ),
    "ui_overlay_blocks": _BugRecipe(
        "medium", "heuristic", "Interaction target obscured by overlay"
    ),
}


def aggregate_bugs(
    events: Iterable[ProbeEvent],
    *,
    file_changes: Iterable[FileChange] | None = None,
    symbol_changes: Iterable[SymbolChange] | None = None,
    interactions_by_route: dict[str, list[Interaction]] | None = None,
) -> list[Bug]:
    """Translate ``events`` into a flat ordered list of :class:`Bug`.

    ``file_changes`` and ``symbol_changes`` drive best-effort
    file:line attribution. ``interactions_by_route`` (if supplied)
    populates :attr:`Bug.interaction` so the report can render the
    triggering action.
    """
    file_list = list(file_changes or [])
    symbol_list = list(symbol_changes or [])
    interactions_map = interactions_by_route or {}
    bugs: list[Bug] = []

    # Scenario failures map onto a virtual recipe whose severity depends
    # on the failure phase — we treat navigation/attach as more severe
    # than per-interaction failures.
    for ev in events:
        bug = _event_to_bug(
            ev, file_list, symbol_list, interactions_map
        )
        if bug is not None:
            bugs.append(bug)
    return bugs


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _event_to_bug(
    event: ProbeEvent,
    file_changes: list[FileChange],
    symbol_changes: list[SymbolChange],
    interactions_map: dict[str, list[Interaction]],
) -> Bug | None:
    payload = event.payload or {}
    kind = payload.get("kind") or payload.get("type")
    if not isinstance(kind, str):
        return None
    if kind in _INFORMATIONAL_KINDS:
        return None

    if kind == "scenario_failure":
        return _scenario_bug(event, file_changes, symbol_changes, interactions_map)

    recipe = _RECIPES.get(kind)
    if recipe is None:
        return None

    title = _title_for(kind, payload, recipe.title_template)
    detail = _detail_for(kind, payload)
    location = _suspected_location(payload, file_changes, symbol_changes)
    interaction = _lookup_interaction(event, interactions_map)

    return Bug(
        probe=event.probe,
        route=event.route,
        severity=recipe.severity,
        confidence=recipe.confidence,
        title=title,
        detail=detail,
        interaction=interaction,
        suspected_location=location,
        evidence=dict(payload),
        detected_at=event.timestamp,
    )


def _scenario_bug(
    event: ProbeEvent,
    file_changes: list[FileChange],
    symbol_changes: list[SymbolChange],
    interactions_map: dict[str, list[Interaction]],
) -> Bug:
    payload = event.payload or {}
    phase = str(payload.get("phase", "unknown"))
    error = str(payload.get("error", "scenario step failed"))
    severity: BugSeverity = (
        "critical" if phase.startswith(("attach", "navigate")) else "medium"
    )
    return Bug(
        probe=event.probe,
        route=event.route,
        severity=severity,
        confidence="deterministic",
        title=f"Scenario step failed ({phase})",
        detail=error,
        interaction=_lookup_interaction(event, interactions_map),
        suspected_location=_suspected_location(
            payload, file_changes, symbol_changes
        ),
        evidence=dict(payload),
        detected_at=event.timestamp,
    )


def _title_for(kind: str, payload: dict, template: str) -> str:
    """Render a one-line headline. Pure string ops — no ``.format`` call."""
    if kind == "network_error" or kind == "navigation_failure":
        method = payload.get("method", "GET")
        url = payload.get("url", "?")
        status = payload.get("status")
        if status is not None:
            return f"{template}: {method} {url} → {status}"
        err = payload.get("error", "request failed")
        return f"{template}: {method} {url} ({err})"
    if kind in {"runtime_error", "runtime_warning"}:
        msg = payload.get("message") or payload.get("console_type") or "<no message>"
        return f"{template}: {_truncate(msg, 140)}"
    if kind == "page_crash":
        return template
    if kind == "storage_quota_error":
        storage = payload.get("storage", "storage")
        key = payload.get("key", "?")
        return f"{template} writing {storage}[{key!r}]"
    if kind == "storage_serialization_error":
        return f"{template}: {_truncate(payload.get('error', ''), 140)}"
    if kind in {"ui_no_change", "ui_overlay_blocks"}:
        sel = payload.get("selector") or {}
        sel_value = sel.get("value", "?") if isinstance(sel, dict) else "?"
        return f"{template} ({sel_value})"
    return template


def _detail_for(kind: str, payload: dict) -> str:
    """Multi-line detail block, kept short and useful (≤ ~6 lines)."""
    parts: list[str] = []
    if kind in {"runtime_error", "runtime_warning"}:
        if (msg := payload.get("message")):
            parts.append(str(msg))
        if (stack := payload.get("stack")):
            parts.append(_truncate(str(stack), 800))
        if (loc := payload.get("location")) and isinstance(loc, dict):
            url = loc.get("url")
            line = loc.get("line")
            if url:
                parts.append(f"at {url}:{line}" if line else f"at {url}")
    elif kind in {"network_error", "navigation_failure"}:
        if (err := payload.get("error")):
            parts.append(f"error: {err}")
        if (rt := payload.get("resource_type")):
            parts.append(f"resource_type: {rt}")
        if (cf := payload.get("confidence")):
            parts.append(f"network confidence: {cf}")
    elif kind == "storage_quota_error":
        if (sz := payload.get("attempted_size")) is not None:
            parts.append(f"attempted size: {sz} bytes")
        if (err := payload.get("error")):
            parts.append(str(err))
    elif kind == "storage_serialization_error":
        if (err := payload.get("error")):
            parts.append(str(err))
    elif kind == "ui_no_change":
        parts.append(
            "Target outerHTML and document body fingerprint were "
            "byte-identical before and after the interaction."
        )
    elif kind == "ui_overlay_blocks":
        cover = payload.get("cover_descriptor")
        phase = payload.get("phase", "before")
        parts.append(
            f"Target was occluded ({phase}) by: {cover}"
            if cover
            else f"Target was occluded ({phase}) by another element."
        )
    elif kind == "page_crash":
        parts.append("The renderer process for this page crashed.")
    return "\n".join(p for p in parts if p)


# Greedy file:line extractor: ``foo/bar.tsx:42`` or ``foo/bar.tsx:42:9``.
_FILE_LINE_RE = re.compile(
    r"([\w./\-@]+\.(?:tsx?|jsx?|mjs|cjs|css|html|py)):(\d+)"
)


def _suspected_location(
    payload: dict,
    file_changes: list[FileChange],
    symbol_changes: list[SymbolChange],
) -> BugLocation | None:
    """Best-effort match from event payload to a changed file."""
    blob = _stringify_payload(payload)

    # First pass: extract explicit ``path:line`` and check against the diff.
    for match in _FILE_LINE_RE.finditer(blob):
        candidate_path, line_str = match.group(1), match.group(2)
        try:
            line = max(int(line_str), 1)
        except ValueError:
            continue
        for fc in file_changes:
            if str(fc.path) == candidate_path or str(fc.path).endswith(
                "/" + candidate_path
            ) or candidate_path.endswith("/" + str(fc.path)):
                return BugLocation(file=fc.path, line=line)

    # Second pass: any changed file path mentioned anywhere in the payload.
    for fc in file_changes:
        path_str = str(fc.path)
        if path_str and path_str in blob:
            line = fc.hunks[0].new_start if fc.hunks else 1
            return BugLocation(file=fc.path, line=max(line, 1))

    # Third pass: fall back to the first symbol change, if any.
    if symbol_changes:
        first = symbol_changes[0]
        return BugLocation(
            file=first.file, line=max(first.symbol.start_line, 1)
        )
    return None


def _stringify_payload(payload: dict) -> str:
    """Flatten a payload into a single string for substring matching."""
    pieces: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)
        elif value is not None:
            pieces.append(str(value))

    walk(payload)
    return "\n".join(pieces)


def _lookup_interaction(
    event: ProbeEvent,
    interactions_map: dict[str, list[Interaction]],
) -> Interaction | None:
    if event.interaction_index is None:
        return None
    interactions = interactions_map.get(event.route)
    if not interactions:
        return None
    if 0 <= event.interaction_index < len(interactions):
        return interactions[event.interaction_index]
    return None


def _truncate(value: str, limit: int) -> str:
    value = str(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


__all__ = ["aggregate_bugs"]
