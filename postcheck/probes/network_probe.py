"""Network probe — request/response/requestfailed observation (v0).

Implements the three-layer filter from CLAUDE.md:

1. **Resource type whitelist** — only ``xhr | fetch | websocket | eventsource
   | document`` (configurable) are tracked. Images, fonts, stylesheets, etc.
   are dropped before correlation, keeping the event stream signal-rich.
2. **URL patterns** — ``ignore_patterns`` drop entirely (HMR sockets,
   common telemetry domains); ``focus_patterns`` boost confidence for the
   user's first-party endpoints.
3. **Origin classification** — same-origin failures are reported with
   ``high`` confidence, cross-origin with ``treat_cross_origin_as``
   (default ``medium``). A focus-pattern hit always wins (``high``).

Special-cased: a failure (4xx/5xx response or ``requestfailed``) on a
``document`` resource is emitted as ``payload.kind = "navigation_failure"``
rather than ``"network_error"`` because it's a different bug category
(the page itself didn't load) and downstream the bug aggregator renders
it differently.

Each emitted :class:`ProbeEvent` is correlated with the interaction that
*started* the request — captured at ``request`` time, not at ``response``
time — so a 500 that resolves long after the click still points at the
right culprit.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from ..core.config import NetworkSettings
from ..core.types import ImpactConfidence, ProbeEvent
from .shared import get_current_route, get_interaction_index

if TYPE_CHECKING:
    from playwright.async_api import Page, Request, Response


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _origin(url: str) -> str:
    """Return ``scheme://host:port`` (no path/query) or ``""`` on parse failure."""
    if not url:
        return ""
    try:
        p = urlparse(url)
    except Exception:  # pragma: no cover
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


class NetworkProbe:
    """Wraps the three Playwright network events with the v0 filter chain."""

    name = "network"

    def __init__(
        self,
        settings: NetworkSettings | None = None,
        *,
        interaction_provider: Callable[[], int | None] | None = None,
        route_provider: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings or NetworkSettings()
        self._ignore = [
            re.compile(p) for p in self._settings.ignore_patterns
        ]
        self._focus = [
            re.compile(p) for p in self._settings.focus_patterns
        ]
        self._resource_types = set(self._settings.resource_types)
        self._cross_origin_policy = self._settings.treat_cross_origin_as

        self._interaction_provider = interaction_provider or get_interaction_index
        self._route_provider = route_provider or get_current_route

        self._buffer: list[ProbeEvent] = []
        self._page: Page | None = None
        self._listeners: list[tuple[str, Callable[..., Any]]] = []
        # Tracked requests: keyed by id(request) since Playwright reuses the
        # same Python object across request/response/requestfailed events.
        self._tracked: dict[int, dict[str, Any]] = {}
        self._counter = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def attach(self, page: Page) -> None:
        if self._page is not None:
            return
        self._page = page

        def on_request(req: Request) -> None:
            self._on_request(req)

        def on_response(resp: Response) -> None:
            self._on_response(resp)

        def on_requestfailed(req: Request) -> None:
            self._on_requestfailed(req)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_requestfailed)
        self._listeners = [
            ("request", on_request),
            ("response", on_response),
            ("requestfailed", on_requestfailed),
        ]

    def collect_events(self) -> list[ProbeEvent]:
        out, self._buffer = self._buffer, []
        return out

    async def detach(self) -> None:
        if self._page is None:
            return
        for ev, cb in self._listeners:
            try:
                self._page.remove_listener(ev, cb)
            except Exception:  # pragma: no cover
                pass
        self._listeners = []
        self._tracked.clear()
        self._page = None

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _classify(
        self, url: str, resource_type: str, is_navigation: bool
    ) -> ImpactConfidence | None:
        """Return the confidence to record, or ``None`` to drop the request.

        Documents are always tracked regardless of the resource-type
        whitelist — a document failure *is* the navigation failure we care
        about and the user shouldn't be able to silence it via the filter.
        """
        # Layer 1 — resource type. Documents bypass the gate (they are
        # always interesting).
        if resource_type not in self._resource_types and not is_navigation:
            return None

        # Layer 2a — ignore.
        if any(p.search(url) for p in self._ignore):
            return None

        # Layer 2b — focus boost.
        in_focus = any(p.search(url) for p in self._focus)

        # Navigation requests are by definition same-origin (they define
        # the origin we compare against).
        if is_navigation:
            return "high"

        same_origin = self._is_same_origin(url)

        if in_focus:
            # A focus-pattern hit always wins, regardless of origin.
            return "high"
        if same_origin:
            return "high"

        # Cross-origin, no focus match — apply policy.
        policy = self._cross_origin_policy
        if policy == "ignore":
            return None
        # ``CrossOriginPolicy`` shares its values with ``ImpactConfidence``
        # for the recordable cases (``high | medium | low``).
        return policy  # type: ignore[return-value]

    def _is_same_origin(self, url: str) -> bool:
        page_url = self._page.url if self._page is not None else ""
        page_origin = _origin(page_url)
        if not page_origin:
            # Before the first real navigation we conservatively treat
            # everything as same-origin so we don't downgrade confidence
            # for the document load itself.
            return True
        return _origin(url) == page_origin

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_request(self, req: Request) -> None:
        url = getattr(req, "url", "") or ""
        resource_type = getattr(req, "resource_type", "") or ""
        is_navigation = resource_type == "document"
        confidence = self._classify(url, resource_type, is_navigation)
        if confidence is None:
            return
        self._counter += 1
        rid = f"req-{self._counter}"
        self._tracked[id(req)] = {
            "request_id": rid,
            "url": url,
            "method": getattr(req, "method", "") or "",
            "resource_type": resource_type,
            "is_navigation": is_navigation,
            "confidence": confidence,
            # Capture-at-request-time so a slow 500 still points at the
            # click that triggered it.
            "interaction_index": self._interaction_provider(),
            "started_at": _utcnow_iso(),
        }

    def _on_response(self, resp: Response) -> None:
        req = getattr(resp, "request", None)
        if req is None:
            return
        meta = self._tracked.pop(id(req), None)
        if meta is None:
            return
        status = getattr(resp, "status", 0) or 0
        if status < 400:
            return
        kind = "navigation_failure" if meta["is_navigation"] else "network_error"
        self._emit(
            kind,
            meta,
            status=status,
            error=None,
            phase="response",
        )

    def _on_requestfailed(self, req: Request) -> None:
        meta = self._tracked.pop(id(req), None)
        if meta is None:
            return
        # Playwright exposes ``request.failure`` as a string like
        # ``"net::ERR_FAILED"`` (or ``None`` if not yet known).
        failure = getattr(req, "failure", None)
        if failure is None:
            error = "request failed"
        elif isinstance(failure, str):
            error = failure
        else:
            # Some bindings return a dict ``{"errorText": "..."}``.
            error = (
                failure.get("errorText")
                if isinstance(failure, dict)
                else str(failure)
            ) or "request failed"
        kind = "navigation_failure" if meta["is_navigation"] else "network_error"
        self._emit(
            kind,
            meta,
            status=None,
            error=error,
            phase="requestfailed",
        )

    def _emit(
        self,
        kind: str,
        meta: dict[str, Any],
        *,
        status: int | None,
        error: str | None,
        phase: str,
    ) -> None:
        self._buffer.append(
            ProbeEvent(
                probe="network",
                route=self._route_provider() or "",
                interaction_index=meta["interaction_index"],
                payload={
                    "kind": kind,
                    "url": meta["url"],
                    "method": meta["method"],
                    "resource_type": meta["resource_type"],
                    "status": status,
                    "error": error,
                    "request_id": meta["request_id"],
                    "confidence": meta["confidence"],
                    "phase": phase,
                    "started_at": meta["started_at"],
                    "captured_at": _utcnow_iso(),
                },
            )
        )


__all__ = ["NetworkProbe"]
