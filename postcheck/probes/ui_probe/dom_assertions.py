"""DOM-assertions UI probe (v0).

Runs around each interaction the scenario runner performs:

* ``before_interaction(index, target)`` — snapshot the target's
  ``outerHTML``, its visibility, and what element actually sits at the
  centre of its bounding box (``document.elementFromPoint``). If the
  target is already covered by an overlay we emit ``ui_overlay_blocks``
  immediately so the bug isn't masked by the click subsequently failing
  with "element intercepts pointer events" (which would surface only as a
  generic ``scenario_failure``).
* ``after_interaction(index, target)`` — re-snapshot and compare:

  * If the target's ``outerHTML`` is byte-identical *and* the document
    body's text content + HTML length are unchanged, the click did
    nothing visible -> ``ui_no_change``.
  * If the target was clear before but is now covered -> late
    ``ui_overlay_blocks`` (a modal opened on top, etc.).

v0 limitations
--------------
* CLAUDE.md item #3 ("does the rendered text match the predicted text")
  is opt-in: the v0 adapters don't propagate text predictions, so this
  check is skipped. The hook is not wired in v0; v1 adapters will fill
  it in without reshaping the probe.
* No MutationObserver — body-fingerprint diffing is sufficient for v0
  scenarios. v1 may upgrade to mutation-record capture for richer
  attribution.
* Visual / pixel-level diffs live in ``visual_judge`` (v1 only).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from ...core.types import ProbeEvent
from ..shared import (
    drain_ui_interaction_failures,
    get_current_route,
    get_interaction_index,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser.target_locator import LocatedTarget


# JS evaluated against the target element. Returns ``null`` if the element
# vanished between locator resolution and snapshot time.
#
# The snapshot has two layers:
#
# * **Target layer** (``outerHTML`` / ``visible`` / ``covered`` /
#   ``cover_descriptor``) — used only for ``ui_overlay_blocks`` detection.
#   We intentionally do *not* feed target ``outerHTML`` into the
#   "did anything change?" decision: many real handlers update a sibling
#   element (``setMsg(...)`` writing to a ``<p data-testid="*-msg">``),
#   which would leave the clicked button's HTML identical and produce a
#   false "no visible effect" bug.
# * **Route-window layer** (``text_length`` / ``child_count`` /
#   ``text_hash``) — three cheap signals on ``document.body``. If *any*
#   one differs between before/after, the interaction had a visible
#   effect and we emit nothing. ``text_hash`` is a 32-bit FNV-1a of the
#   first 16 KB of ``innerText``; combined with the integer length and
#   child count, the collision probability for a real mutation that
#   leaves all three identical is negligible.
_SNAPSHOT_JS = r"""
(el) => {
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  const visible = rect.width > 0 && rect.height > 0;
  let covered = false;
  let coverDescriptor = null;
  if (visible) {
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    if (cx >= 0 && cy >= 0 && cx <= vw && cy <= vh) {
      const top = document.elementFromPoint(cx, cy);
      if (top && top !== el && !el.contains(top) && !top.contains(el)) {
        covered = true;
        const cls = (top.className && typeof top.className === 'string')
          ? top.className.split(/\s+/).filter(Boolean)
              .map((c) => '.' + c).join('')
          : '';
        const id = top.id ? '#' + top.id : '';
        coverDescriptor =
          (top.tagName ? top.tagName.toLowerCase() : 'unknown') + id + cls;
      }
    }
  }
  const body = document.body;
  const text = body ? (body.innerText || '') : '';
  // FNV-1a 32-bit over the first 16 KB of visible text.
  const sample = text.slice(0, 16384);
  let h = 0x811c9dc5 >>> 0;
  for (let i = 0; i < sample.length; i++) {
    h ^= sample.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  const childCount = body ? body.getElementsByTagName('*').length : 0;
  return {
    outerHTML: el.outerHTML,
    visible: visible,
    covered: covered,
    cover_descriptor: coverDescriptor,
    text_length: text.length,
    child_count: childCount,
    text_hash: h,
    // Legacy fields retained for backwards compatibility with stub-based
    // unit tests that pre-date the route-window snapshot redesign.
    body_text: text.slice(0, 4096),
    body_html_length: body ? body.innerHTML.length : 0,
  };
}
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Route-window diff. Uses the three new metrics when present (real
# browser path); falls back to the legacy ``body_text`` /
# ``body_html_length`` pair so unit-test stubs that pre-date the
# redesign keep working without modification.
_NEW_METRICS = ("text_length", "child_count", "text_hash")
_LEGACY_METRICS = ("body_text", "body_html_length")


def _window_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if any(k in after and k in before for k in _NEW_METRICS):
        return any(before.get(k) != after.get(k) for k in _NEW_METRICS)
    return any(before.get(k) != after.get(k) for k in _LEGACY_METRICS)


class DomAssertionsProbe:
    """``ProbeHandler`` that diffs DOM state around each interaction."""

    name = "ui"

    def __init__(
        self,
        *,
        snapshot_timeout_ms: int = 2_000,
        interaction_provider: Callable[[], int | None] | None = None,
        route_provider: Callable[[], str] | None = None,
    ) -> None:
        self._snapshot_timeout_ms = snapshot_timeout_ms
        self._interaction_provider = interaction_provider or get_interaction_index
        self._route_provider = route_provider or get_current_route
        self._page: Page | None = None
        self._buffer: list[ProbeEvent] = []
        # interaction_index -> snapshot dict
        self._snapshots: dict[int, dict[str, Any]] = {}
        # (interaction_index, kind) pairs already emitted — used to
        # dedupe between ``before_interaction`` overlay detection and a
        # follow-up ``ui_overlay_blocks`` queued by the scenario runner.
        self._emitted: set[tuple[int | None, str]] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def attach(self, page: Page) -> None:
        self._page = page

    def collect_events(self) -> list[ProbeEvent]:
        # Drain any per-interaction failures the scenario runner queued
        # since the last call (locator timeouts, hidden / detached
        # elements, overlay-blocked clicks) and emit them as ``probe=ui``
        # events with the precise kind. Overlay collisions with our own
        # ``before_interaction`` snapshot are deduped via ``_emitted``.
        for fail in drain_ui_interaction_failures():
            key = (fail.index, fail.kind)
            if key in self._emitted:
                continue
            self._buffer.append(
                ProbeEvent(
                    probe="ui",
                    route=fail.route or self._route_provider() or "",
                    interaction_index=fail.index,
                    payload={
                        "kind": fail.kind,
                        "selector": fail.selector,
                        "error": fail.error,
                        "captured_at": _utcnow_iso(),
                    },
                )
            )
            self._emitted.add(key)
        out, self._buffer = self._buffer, []
        return out

    async def detach(self) -> None:
        self._page = None
        self._snapshots.clear()
        self._emitted.clear()

    # ------------------------------------------------------------------
    # Interaction hooks (called by scenario_runner)
    # ------------------------------------------------------------------

    async def before_interaction(
        self, index: int, target: "LocatedTarget"
    ) -> None:
        snap = await self._snapshot(target)
        if snap is None:
            return
        self._snapshots[index] = snap
        if snap.get("covered"):
            self._emit(
                kind="ui_overlay_blocks",
                target=target,
                phase="before",
                cover_descriptor=snap.get("cover_descriptor"),
            )

    async def after_interaction(
        self, index: int, target: "LocatedTarget"
    ) -> None:
        before = self._snapshots.pop(index, None)
        if before is None:
            return
        after = await self._snapshot(target)
        if after is None:
            # Element disappeared after the interaction — that *is* a
            # change, so explicitly do not emit ui_no_change.
            return

        # Route-window diff — see ``_SNAPSHOT_JS`` docstring for why these
        # three signals (and *not* target outerHTML) drive the decision.
        # Falls back to the legacy ``body_text`` / ``body_html_length``
        # pair when the new fields are absent (unit-test stubs).
        window_changed = _window_changed(before, after)

        if not window_changed:
            expected = target.selector.expected_visible_effect
            # ``False`` => adapter is confident the handler has no
            # user-visible effect (analytics, fire-and-forget fetch,
            # etc.). Suppress the finding entirely; surfacing it would
            # be pure noise.
            if expected is not False:
                confidence = "deterministic" if expected is True else "heuristic"
                self._emit(
                    kind="ui_no_change",
                    target=target,
                    target_outer_html_unchanged=(
                        before.get("outerHTML") == after.get("outerHTML")
                    ),
                    text_length=after.get("text_length", after.get("body_html_length")),
                    child_count=after.get("child_count"),
                    confidence=confidence,
                    expected_visible_effect=expected,
                )

        if after.get("covered") and not before.get("covered"):
            self._emit(
                kind="ui_overlay_blocks",
                target=target,
                phase="after",
                cover_descriptor=after.get("cover_descriptor"),
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _snapshot(
        self, target: "LocatedTarget"
    ) -> dict[str, Any] | None:
        if self._page is None:
            return None
        try:
            raw = await target.locator.evaluate(
                _SNAPSHOT_JS, timeout=self._snapshot_timeout_ms
            )
        except Exception:
            # Element may have been detached; treat as "no snapshot" so
            # we neither emit ui_no_change nor crash the scenario.
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def _emit(
        self,
        *,
        kind: str,
        target: "LocatedTarget",
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "kind": kind,
            "selector": target.selector.model_dump(mode="json"),
            "captured_at": _utcnow_iso(),
        }
        for k, v in extra.items():
            if v is not None:
                payload[k] = v
        self._buffer.append(
            ProbeEvent(
                probe="ui",
                route=self._route_provider() or "",
                interaction_index=self._interaction_provider(),
                payload=payload,
            )
        )
        self._emitted.add((self._interaction_provider(), kind))


__all__ = ["DomAssertionsProbe"]
