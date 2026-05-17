"""Runtime exception / console / crash probe (v0).

Subscribes to three Playwright page events:

* ``pageerror`` — uncaught JS exceptions and unhandled promise rejections.
  Emits :class:`ProbeEvent` ``payload.kind = "runtime_error"`` with the
  message and (when present) the JS stack trace.
* ``console`` — JS console output, filtered to ``error`` and ``warning``
  levels only. Emits ``"runtime_console_error"`` for ``console.error``
  (and failing ``console.assert``) and ``"runtime_console_warning"`` for
  ``console.warn``; carries the source location reported by the renderer.
  These are *intentionally* distinct from ``runtime_error`` — a logged
  console.error is not the same defect as an uncaught throw, and the
  bug aggregator ranks them separately.
* ``crash`` — the renderer process died. Emits ``"page_crash"``. Treated as
  a single, terminal event.

Each emitted event is correlated with the interaction that triggered it by
reading :func:`postcheck.probes.shared.get_interaction_index` *at capture
time* — page events fire asynchronously and may surface after the runner
has moved on, so stamping at delivery is more accurate than the runner's
post-hoc index assignment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from ..core.types import ProbeEvent
from .shared import get_current_route, get_interaction_index

if TYPE_CHECKING:
    from playwright.async_api import ConsoleMessage, Error, Page


# Console levels we care about. Playwright surfaces one of:
# ``log | debug | info | error | warning | dir | dirxml | table | trace |
# clear | startGroup | startGroupCollapsed | endGroup | assert | profile |
# profileEnd | count | timeEnd``. Only ``error`` and ``warning`` are
# actionable for v0; ``assert`` is treated as a console error too because
# that's what a failing ``console.assert`` actually is. These kinds are
# deliberately distinct from ``runtime_error`` (uncaught throw, via
# ``pageerror``) so the aggregator can rank a logged-and-handled error
# below an actual uncaught exception.
_CONSOLE_KINDS: dict[str, str] = {
    "error": "runtime_console_error",
    "warning": "runtime_console_warning",
    "assert": "runtime_console_error",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeProbe:
    """Captures uncaught exceptions, console errors/warnings, and crashes."""

    name = "runtime"

    def __init__(
        self,
        *,
        interaction_provider: Callable[[], int | None] | None = None,
        route_provider: Callable[[], str] | None = None,
    ) -> None:
        # Allow tests / advanced callers to inject correlation sources.
        # Defaults read the module-level scenario context.
        self._interaction_provider = interaction_provider or get_interaction_index
        self._route_provider = route_provider or get_current_route
        self._buffer: list[ProbeEvent] = []
        self._page: Page | None = None
        self._listeners: list[tuple[str, Callable[..., Any]]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def attach(self, page: Page) -> None:
        if self._page is not None:
            return  # idempotent — re-attach is a no-op
        self._page = page

        def on_pageerror(err: Error) -> None:
            self._capture_pageerror(err)

        def on_console(msg: ConsoleMessage) -> None:
            self._capture_console(msg)

        def on_crash(_p: Page) -> None:
            self._capture_crash()

        page.on("pageerror", on_pageerror)
        page.on("console", on_console)
        page.on("crash", on_crash)
        self._listeners = [
            ("pageerror", on_pageerror),
            ("console", on_console),
            ("crash", on_crash),
        ]

    def collect_events(self) -> list[ProbeEvent]:
        out, self._buffer = self._buffer, []
        return out

    async def detach(self) -> None:
        if self._page is None:
            return
        for event, cb in self._listeners:
            try:
                self._page.remove_listener(event, cb)
            except Exception:  # pragma: no cover - listener already gone
                pass
        self._listeners = []
        self._page = None

    # ------------------------------------------------------------------
    # Capture helpers
    # ------------------------------------------------------------------

    def _emit(self, payload: dict[str, Any]) -> None:
        self._buffer.append(
            ProbeEvent(
                probe="runtime",
                route=self._route_provider() or "",
                interaction_index=self._interaction_provider(),
                payload=payload,
            )
        )

    def _capture_pageerror(self, err: Error) -> None:
        # ``Error`` is Playwright's wrapper around the JS error object. It
        # always has ``message``; ``stack`` and ``name`` may be missing on
        # exotic throws (e.g. ``throw "string"``).
        self._emit(
            {
                "kind": "runtime_error",
                "source": "pageerror",
                "message": _safe_attr(err, "message", default=""),
                "name": _safe_attr(err, "name", default=None),
                "stack": _safe_attr(err, "stack", default=None),
                "captured_at": _utcnow_iso(),
            }
        )

    def _capture_console(self, msg: ConsoleMessage) -> None:
        msg_type = _safe_attr(msg, "type", default="log")
        kind = _CONSOLE_KINDS.get(msg_type)
        if kind is None:
            return  # filtered out — not actionable
        loc = _safe_attr(msg, "location", default=None)
        if isinstance(loc, dict):
            location: dict[str, Any] | None = {
                "url": loc.get("url"),
                "line": loc.get("lineNumber"),
                "column": loc.get("columnNumber"),
            }
        else:
            location = None
        self._emit(
            {
                "kind": kind,
                "source": "console",
                "console_type": msg_type,
                "message": _safe_attr(msg, "text", default=""),
                "location": location,
                "captured_at": _utcnow_iso(),
            }
        )

    def _capture_crash(self) -> None:
        self._emit(
            {
                "kind": "page_crash",
                "source": "crash",
                "message": "renderer process crashed",
                "captured_at": _utcnow_iso(),
            }
        )


def _safe_attr(obj: Any, name: str, *, default: Any) -> Any:
    """Read ``obj.name`` defensively — Playwright objects sometimes proxy."""
    try:
        return getattr(obj, name, default)
    except Exception:  # pragma: no cover
        return default


__all__ = ["RuntimeProbe"]
