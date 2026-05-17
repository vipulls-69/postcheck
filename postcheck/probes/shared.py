"""Shared probe types, the :class:`ProbeHandler` protocol, and the
process-wide *interaction context* (v0).

A *probe* is the only thing in the core allowed to subscribe to a Playwright
page lifecycle. Probes are framework-agnostic — see CLAUDE.md core principle
#2 ("Probes don't know about frameworks"). They expose a tiny lifecycle
contract:

* :meth:`ProbeHandler.attach` — register Playwright listeners (must be
  awaited *before* the runner navigates so first-byte events aren't lost).
* :meth:`ProbeHandler.collect_events` — return everything seen so far as a
  list of :class:`ProbeEvent`. Called by the scenario runner after each
  interaction settles.
* :meth:`ProbeHandler.detach` — release listeners; idempotent.

The protocol is :func:`runtime_checkable` so tests can validate stubs
without a base class.

Interaction context
-------------------

Page events fire asynchronously: a ``console.error`` triggered by clicking
"Save" may not be delivered to the Python listener until well after the
runner has moved on. So that probes can correlate each captured event with
the interaction that *triggered* it (rather than the interaction the runner
happens to be on when the event surfaces), the scenario runner maintains a
small process-wide context exposed through
:func:`get_interaction_index` / :func:`set_interaction_index`. Probes read
the index at *capture time* and store it on the emitted
:class:`ProbeEvent`. The runner's downstream stamping then leaves any
probe-set index alone.

For v0 we run scenarios sequentially, so a single module-level holder is
correct. v1 (subagent fan-out) will swap the holder for a
:class:`contextvars.ContextVar` without changing this API.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..core.types import ProbeEvent, ProbeName

if TYPE_CHECKING:
    from playwright.async_api import Page


@runtime_checkable
class ProbeHandler(Protocol):
    """Lifecycle contract for everything that observes a scenario.

    Probes that need to do async work to bring their buffer up to date
    (e.g. ``page.evaluate`` to drain an in-page array) may additionally
    implement an ``async def flush(self) -> None`` method; the scenario
    runner awaits it before each :meth:`collect_events` call. The hook is
    optional — runtime / network probes maintain their buffers via Python
    callbacks and don't need it.

    Flush timing is **load-bearing** for between-interaction recovery:
    when the scenario runner reloads the page between interactions (see
    ``ScenarioSettings.recovery_mode``), the storage probe's in-page
    ``window.__postcheck_storage_events`` array is wiped by the fresh
    document. The runner therefore calls :func:`flush_handlers` once more
    immediately before any ``page.reload()`` so late writes survive into
    Python-side buffers before the reload destroys the in-page state.
    """

    name: ProbeName

    async def attach(self, page: Page) -> None:
        """Subscribe to the relevant ``page.on(...)`` events."""

    def collect_events(self) -> list[ProbeEvent]:
        """Return every event observed so far (drained from the buffer)."""

    async def detach(self) -> None:
        """Unsubscribe; safe to call multiple times."""


async def flush_handlers(handlers: list["ProbeHandler"]) -> None:
    """Invoke the optional ``flush()`` hook on every probe that defines one.

    Centralised so the scenario runner can call it both as part of
    ``_drain`` (the normal per-interaction collection) **and** as a
    standalone safety step right before ``page.reload()`` (so storage
    probe events that hit ``window.__postcheck_storage_events`` after
    the post-interaction drain still survive into Python-side buffers).
    Failures are swallowed — a probe whose buffer can't be pulled (e.g.
    page closed mid-flush) must not tank the scenario.
    """
    for h in handlers:
        flush = getattr(h, "flush", None)
        if flush is None:
            continue
        try:
            await flush()
        except Exception:  # pragma: no cover - defensive
            pass


# ---------------------------------------------------------------------------
# Interaction context (v0: module-level holder)
# ---------------------------------------------------------------------------

_current_interaction_index: int | None = None
_current_route: str = ""


def get_interaction_index() -> int | None:
    """Return the index of the interaction the runner is currently driving.

    Returns ``None`` outside of any interaction (i.e. during initial
    navigation, post-interaction settle windows, or outside a scenario).
    """
    return _current_interaction_index


def get_current_route() -> str:
    """Return the route string the active scenario was started with."""
    return _current_route


def set_interaction_index(index: int | None) -> None:
    """Update the current interaction index. Called by the scenario runner."""
    global _current_interaction_index
    _current_interaction_index = index


def set_current_route(route: str) -> None:
    """Update the current route. Called by the scenario runner on entry."""
    global _current_route
    _current_route = route


def reset_interaction_context() -> None:
    """Clear the context — called by the runner on scenario exit."""
    set_interaction_index(None)
    set_current_route("")
    _pending_ui_failures.clear()


# ---------------------------------------------------------------------------
# UI interaction-failure queue
# ---------------------------------------------------------------------------
#
# When the scenario runner catches a per-interaction exception (locator
# timeout, element hidden, element detached), it should *not* be reported
# as ``scenario_failure`` — those mean "the harness broke". Instead the
# failure is queued here and drained by the UI probe (if attached) inside
# ``collect_events``, yielding a ``probe="ui"`` event with a precise kind
# (``ui_locator_timeout`` / ``ui_element_hidden`` / ``ui_element_detached``
# / ``ui_overlay_blocks``). If no UI probe is attached the runner falls
# back to ``scenario_failure`` so the bug isn't silently dropped.
#
# v0: single-process, sequential scenarios — a module-level list is fine.
# v1 (subagent fan-out): swap to ``ContextVar[list]`` without API change.


@dataclass(slots=True)
class InteractionFailure:
    """A per-interaction failure picked up by the UI probe."""

    index: int
    route: str
    selector: dict[str, Any]
    kind: str  # one of ``UiEventKind`` values
    error: str


_pending_ui_failures: list[InteractionFailure] = []


def queue_ui_interaction_failure(failure: InteractionFailure) -> None:
    """Record a per-interaction failure for the UI probe to drain."""
    _pending_ui_failures.append(failure)


def drain_ui_interaction_failures() -> list[InteractionFailure]:
    """Return and clear every queued interaction failure."""
    out = list(_pending_ui_failures)
    _pending_ui_failures.clear()
    return out


# ---------------------------------------------------------------------------
# Message formatting helpers
# ---------------------------------------------------------------------------
#
# Pure functions that translate a probe ``payload`` (a.k.a. the bug
# ``evidence`` dict) into a single-line human-readable summary suitable
# for ``Bug.title``. Centralised here so a new ``payload["kind"]`` can't
# regress to producing an empty-string title — every kind a probe emits
# **must** appear in one of these helpers, and ``test_message_formatting``
# locks the contract in.
#
# Each helper guarantees a non-empty, non-whitespace return value. When
# the source fields are missing the helper falls back to a stable
# ``"<kind> fired"`` placeholder so the report never renders a blank
# line after the ``[probe] `` prefix.


def _truncate(value: object, limit: int) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_loc(loc: object) -> str:
    if not isinstance(loc, dict):
        return ""
    url = loc.get("url")
    line = loc.get("line")
    if url and line:
        return f"{url}:{line}"
    if url:
        return str(url)
    return ""


def format_runtime_message(kind: str, evidence: dict[str, Any]) -> str:
    """Format a runtime-probe event payload as a one-line summary.

    Pure function — ``evidence`` in, ``str`` out. Always returns a
    non-empty string so the bug aggregator can drop the result straight
    into ``Bug.title`` without further checks.
    """
    if kind == "page_crash":
        return "Page crashed"
    msg = evidence.get("message")
    if isinstance(msg, str):
        msg = msg.strip()
    if kind == "runtime_error":
        return f"Runtime error: {_truncate(msg or '<no message>', 140)}"
    if kind == "runtime_warning":
        return f"Console warning: {_truncate(msg or '<no message>', 140)}"
    if kind == "runtime_console_error":
        if msg:
            return f"console.error: {_truncate(msg, 200)}"
        loc = _format_loc(evidence.get("location"))
        return f"runtime_console_error fired at {loc}" if loc else (
            "runtime_console_error fired (no message, no location)"
        )
    if kind == "runtime_console_warning":
        if msg:
            return f"console.warn: {_truncate(msg, 200)}"
        loc = _format_loc(evidence.get("location"))
        return f"runtime_console_warning fired at {loc}" if loc else (
            "runtime_console_warning fired (no message, no location)"
        )
    return f"runtime event ({kind})"


def format_network_message(kind: str, evidence: dict[str, Any]) -> str:
    """Format a network-probe event payload as a one-line summary."""
    method = evidence.get("method") or "GET"
    url = evidence.get("url") or "?"
    status = evidence.get("status")
    error = evidence.get("error")
    if kind in ("network_error", "navigation_failure"):
        prefix = "Network error" if kind == "network_error" else "Navigation failed"
        if status is not None:
            return f"{prefix}: {method} {url} → {status}"
        return f"{prefix}: {method} {url} ({error or 'request failed'})"
    if kind == "network_unresolved_at_detach":
        return f"{method} {url} → did not resolve within scenario window"
    label = {
        "network_aborted": "Network request aborted",
        "network_timeout": "Network request timed out",
        "network_dns_error": "DNS resolution failed",
        "network_connection_error": "Network connection failed",
    }.get(kind)
    if label is not None:
        tail = f" ({error})" if error else ""
        return f"{label}: {method} {url}{tail}"
    return f"network event ({kind}): {method} {url}"


def format_storage_message(kind: str, evidence: dict[str, Any]) -> str:
    """Format a storage-probe event payload as a one-line summary."""
    storage = evidence.get("storage") or "storage"
    key = evidence.get("key")
    error = evidence.get("error")
    if kind == "storage_quota_error":
        key_repr = repr(key) if key is not None else "?"
        return f"Storage quota exceeded writing {storage}[{key_repr}]"
    if kind == "storage_serialization_error":
        return f"Storage serialization failed: {_truncate(error or '<unknown error>', 140)}"
    if kind in (
        "storage_idb_version_error",
        "storage_idb_quota_error",
        "storage_idb_blocked",
        "storage_idb_serialization_error",
    ):
        label = {
            "storage_idb_version_error": "IndexedDB upgrade failed",
            "storage_idb_quota_error": "IndexedDB quota exceeded",
            "storage_idb_blocked": "IndexedDB open blocked",
            "storage_idb_serialization_error": "IndexedDB value not cloneable",
        }[kind]
        db = evidence.get("database") or "?"
        store = evidence.get("store")
        # Version info — surfaces ``old``/``new`` when the in-page IDB
        # wrapper recorded the upgrade-blocked event.
        old_v = evidence.get("old_version")
        new_v = evidence.get("new_version")
        tail_parts: list[str] = [f"db={db}"]
        if store:
            tail_parts.append(f"store={store}")
        if old_v is not None or new_v is not None:
            tail_parts.append(f"version {old_v}→{new_v}")
        err_name = evidence.get("error_name")
        if err_name:
            tail_parts.append(str(err_name))
        return f"{label} ({', '.join(tail_parts)})"
    return f"storage event ({kind})"


__all__ = [
    "InteractionFailure",
    "ProbeEvent",
    "ProbeHandler",
    "ProbeName",
    "drain_ui_interaction_failures",
    "flush_handlers",
    "format_network_message",
    "format_runtime_message",
    "format_storage_message",
    "get_current_route",
    "get_interaction_index",
    "queue_ui_interaction_failure",
    "reset_interaction_context",
    "set_current_route",
    "set_interaction_index",
]
