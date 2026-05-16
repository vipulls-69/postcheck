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

from typing import TYPE_CHECKING, Protocol, runtime_checkable

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
    """

    name: ProbeName

    async def attach(self, page: Page) -> None:
        """Subscribe to the relevant ``page.on(...)`` events."""

    def collect_events(self) -> list[ProbeEvent]:
        """Return every event observed so far (drained from the buffer)."""

    async def detach(self) -> None:
        """Unsubscribe; safe to call multiple times."""


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


__all__ = [
    "ProbeEvent",
    "ProbeHandler",
    "ProbeName",
    "get_current_route",
    "get_interaction_index",
    "reset_interaction_context",
    "set_current_route",
    "set_interaction_index",
]
