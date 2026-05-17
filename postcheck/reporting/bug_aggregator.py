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
from pathlib import Path
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
from ..probes.shared import (
    format_network_message,
    format_runtime_message,
    format_storage_message,
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
    # ``console.error`` / ``console.warn`` — split from ``runtime_error``
    # because a logged-and-handled error is meaningfully less severe
    # than an uncaught throw (which crashes the surrounding call frame).
    "runtime_console_error": _BugRecipe(
        "medium", "heuristic", "Console error"
    ),
    "runtime_console_warning": _BugRecipe(
        "low", "heuristic", "Console warning"
    ),
    "page_crash": _BugRecipe("critical", "deterministic", "Page crashed"),
    # network_probe
    "network_error": _BugRecipe("high", "deterministic", "Network error"),
    "navigation_failure": _BugRecipe(
        "critical", "deterministic", "Navigation failed"
    ),
    # ``net::ERR_*`` splits — see ``_classify_failure`` in network_probe.
    # A ``network_aborted`` comes out of Chromium's ``ERR_ABORTED`` —
    # the request *definitely* failed; only the cause is ambiguous
    # (user navigation, AbortController, devtools cancel). Hence
    # ``deterministic``. ``network_unresolved_at_detach`` is the
    # heuristic backstop synthesised by the probe on teardown for
    # requests that never produced any terminal event in the scenario
    # window — most often a slow server, *not* an abort.
    "network_aborted": _BugRecipe(
        "low", "deterministic", "Network request aborted"
    ),
    "network_unresolved_at_detach": _BugRecipe(
        "low", "heuristic", "Network request did not resolve"
    ),
    "network_timeout": _BugRecipe(
        "high", "deterministic", "Network request timed out"
    ),
    "network_dns_error": _BugRecipe(
        "high", "deterministic", "DNS resolution failed"
    ),
    "network_connection_error": _BugRecipe(
        "high", "deterministic", "Network connection failed"
    ),
    # storage_probe
    "storage_quota_error": _BugRecipe(
        "high", "deterministic", "Storage quota exceeded"
    ),
    "storage_serialization_error": _BugRecipe(
        "medium", "deterministic", "Storage serialization failed"
    ),
    # IndexedDB — see ``storage_probe`` for the full kind list.
    "storage_idb_version_error": _BugRecipe(
        "high", "deterministic", "IndexedDB upgrade failed"
    ),
    "storage_idb_quota_error": _BugRecipe(
        "high", "deterministic", "IndexedDB quota exceeded"
    ),
    "storage_idb_blocked": _BugRecipe(
        "medium", "heuristic", "IndexedDB open blocked"
    ),
    "storage_idb_serialization_error": _BugRecipe(
        "medium", "deterministic", "IndexedDB value not cloneable"
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
    route_files: dict[str, list[Path]] | None = None,
) -> list[Bug]:
    """Translate ``events`` into a flat ordered list of :class:`Bug`.

    ``file_changes`` and ``symbol_changes`` drive best-effort
    file:line attribution. ``interactions_by_route`` (if supplied)
    populates :attr:`Bug.interaction` so the report can render the
    triggering action. ``route_files`` (route -> changed files
    belonging to that route, as returned by the adapter's
    ``files_for_route`` intersected with the diff) scopes
    ``suspected_location`` to files that actually load on the bug's
    route — without it, a bug fired on ``/network`` could be attributed
    to a changed handler that only renders on ``/interactions``.
    """
    file_list = list(file_changes or [])
    symbol_list = list(symbol_changes or [])
    interactions_map = interactions_by_route or {}
    route_files_map = route_files or {}
    bugs: list[Bug] = []

    # Scenario failures map onto a virtual recipe whose severity depends
    # on the failure phase — we treat navigation/attach as more severe
    # than per-interaction failures.
    for ev in events:
        bug = _event_to_bug(
            ev, file_list, symbol_list, interactions_map, route_files_map
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
    route_files_map: dict[str, list[Path]],
) -> Bug | None:
    payload = event.payload or {}
    kind = payload.get("kind") or payload.get("type")
    if not isinstance(kind, str):
        return None
    if kind in _INFORMATIONAL_KINDS:
        return None

    if kind == "scenario_failure":
        return _scenario_bug(
            event, file_changes, symbol_changes, interactions_map, route_files_map
        )

    recipe = _RECIPES.get(kind)
    if recipe is None:
        return None

    title = _title_for(kind, payload, recipe.title_template)
    detail = _detail_for(kind, payload)
    location = _suspected_location(
        payload,
        _scoped_files(file_changes, route_files_map.get(event.route)),
        symbol_changes,
        route_scoped=event.route in route_files_map,
    )
    interaction = _lookup_interaction(event, interactions_map)

    # Confidence override: ``ui_no_change`` ships at the probe-supplied
    # confidence when present. The UI probe sets ``deterministic`` when
    # the adapter predicted a visible effect and the route window stayed
    # identical (a real defect), and ``heuristic`` when the adapter
    # could not predict the handler's intent. The recipe default
    # (``heuristic``) is used when the payload omits the override.
    confidence: BugConfidence = recipe.confidence
    if kind == "ui_no_change":
        override = payload.get("confidence")
        if override in ("deterministic", "heuristic", "llm_judged"):
            confidence = override  # type: ignore[assignment]

    return Bug(
        probe=event.probe,
        route=event.route,
        severity=recipe.severity,
        confidence=confidence,
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
    route_files_map: dict[str, list[Path]],
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
            payload,
            _scoped_files(file_changes, route_files_map.get(event.route)),
            symbol_changes,
            route_scoped=event.route in route_files_map,
        ),
        evidence=dict(payload),
        detected_at=event.timestamp,
    )


def _title_for(kind: str, payload: dict, template: str) -> str:
    """Render a one-line headline. Pure string ops — no ``.format`` call.

    Runtime / network / storage kinds delegate to the shared
    ``format_*_message`` helpers in :mod:`postcheck.probes.shared`. That
    keeps formatting in one place — when a probe adds a new event kind,
    a missing case in the helper trips the formatter's
    ``"<kind> fired"`` fallback rather than silently rendering an empty
    title (which is the regression this consolidation guards against).
    """
    if kind in {
        "runtime_error",
        "runtime_warning",
        "runtime_console_error",
        "runtime_console_warning",
        "page_crash",
    }:
        return format_runtime_message(kind, payload)
    if kind in {
        "network_error",
        "navigation_failure",
        "network_aborted",
        "network_timeout",
        "network_dns_error",
        "network_connection_error",
    }:
        return format_network_message(kind, payload)
    if kind in {
        "storage_quota_error",
        "storage_serialization_error",
        "storage_idb_version_error",
        "storage_idb_quota_error",
        "storage_idb_blocked",
        "storage_idb_serialization_error",
    }:
        return format_storage_message(kind, payload)
    if kind in {"ui_no_change", "ui_overlay_blocks"}:
        sel = payload.get("selector") or {}
        sel_value = sel.get("value", "?") if isinstance(sel, dict) else "?"
        return f"{template} ({sel_value})"
    return template


def _detail_for(kind: str, payload: dict) -> str:
    """Multi-line detail block, kept short and useful (≤ ~6 lines)."""
    parts: list[str] = []
    if kind in {
        "runtime_error",
        "runtime_warning",
        "runtime_console_error",
        "runtime_console_warning",
    }:
        if (msg := payload.get("message")):
            parts.append(str(msg))
        if (stack := payload.get("stack")):
            parts.append(_truncate(str(stack), 800))
        if (loc := payload.get("location")) and isinstance(loc, dict):
            url = loc.get("url")
            line = loc.get("line")
            if url:
                parts.append(f"at {url}:{line}" if line else f"at {url}")
    elif kind in {
        "network_error",
        "navigation_failure",
        "network_aborted",
        "network_timeout",
        "network_dns_error",
        "network_connection_error",
    }:
        if (err := payload.get("error")):
            parts.append(f"error: {err}")
        if (rt := payload.get("resource_type")):
            parts.append(f"resource_type: {rt}")
        if (cf := payload.get("confidence")):
            parts.append(f"network confidence: {cf}")
    elif kind in {"storage_quota_error", "storage_idb_quota_error"}:
        if (sz := payload.get("attempted_size")) is not None:
            parts.append(f"attempted size: {sz} bytes")
        if (err := payload.get("error")):
            parts.append(str(err))
    elif kind in {
        "storage_serialization_error",
        "storage_idb_version_error",
        "storage_idb_blocked",
        "storage_idb_serialization_error",
    }:
        if (db := payload.get("database")):
            parts.append(f"database: {db}")
        if (store := payload.get("store")):
            parts.append(f"store: {store}")
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


def _scoped_files(
    file_changes: list[FileChange], allowed: list[Path] | None
) -> list[FileChange]:
    """Return the subset of ``file_changes`` whose path is in ``allowed``.

    ``allowed=None`` means "no route scoping known" — caller gets the
    full list back so behaviour matches the pre-route-scope code path
    (used by unit tests that don't supply ``route_files``). ``allowed``
    paths may be project-relative or absolute; we compare by ``str``
    suffix to handle both.
    """
    if allowed is None:
        return file_changes
    if not allowed:
        return []
    allowed_strs = {str(p) for p in allowed}
    out: list[FileChange] = []
    for fc in file_changes:
        fc_str = str(fc.path)
        if fc_str in allowed_strs or any(
            fc_str.endswith(a) or a.endswith(fc_str) for a in allowed_strs
        ):
            out.append(fc)
    return out


def _suspected_location(
    payload: dict,
    file_changes: list[FileChange],
    symbol_changes: list[SymbolChange],
    *,
    route_scoped: bool = False,
) -> BugLocation | None:
    """Best-effort match from event payload to a changed file.

    When ``route_scoped`` is True, ``file_changes`` has already been
    filtered to files belonging to the bug's route. In that mode we
    suppress the global symbol-change fallback — guessing a file from
    another route is *anti-helpful* (the user is being told to look at
    code that doesn't even run on the failing route). Better to return
    None and let the report say "no suspected location" than to point
    at the wrong file with high apparent confidence.
    """
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

    # Third pass: only when we have NO route scoping signal at all.
    # If the caller supplied route_files but this route's changed-file
    # set is empty or didn't match, we'd rather emit None than point
    # the user at unrelated code.
    if symbol_changes and not route_scoped:
        first = symbol_changes[0]
        return BugLocation(
            file=first.file, line=max(first.symbol.start_line, 1)
        )

    # Fourth pass (route-scoped): if there ARE route-scoped changed
    # files but nothing in the payload referenced any of them, point
    # at the first one's first hunk. This is still a guess, but it's
    # at minimum guaranteed to be a file the user changed and that
    # runs on the failing route — both necessary conditions to be a
    # plausible cause.
    if route_scoped and file_changes:
        fc = file_changes[0]
        line = fc.hunks[0].new_start if fc.hunks else 1
        return BugLocation(file=fc.path, line=max(line, 1))
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
