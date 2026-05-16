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
from ..shared import get_current_route, get_interaction_index

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser.target_locator import LocatedTarget


# JS evaluated against the target element. Returns ``null`` if the element
# vanished between locator resolution and snapshot time. Truncates body text
# to 4 KB so the comparison stays cheap on large pages — collisions inside
# 4 KB *and* identical body HTML length *and* identical target outerHTML are
# vanishingly unlikely to be a real change we care about.
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
  const bodyText = body ? (body.innerText || '').slice(0, 4096) : '';
  const bodyHtmlLen = body ? body.innerHTML.length : 0;
  return {
    outerHTML: el.outerHTML,
    visible: visible,
    covered: covered,
    cover_descriptor: coverDescriptor,
    body_text: bodyText,
    body_html_length: bodyHtmlLen,
  };
}
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def attach(self, page: Page) -> None:
        self._page = page

    def collect_events(self) -> list[ProbeEvent]:
        out, self._buffer = self._buffer, []
        return out

    async def detach(self) -> None:
        self._page = None
        self._snapshots.clear()

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

        if (
            before.get("outerHTML") == after.get("outerHTML")
            and before.get("body_text") == after.get("body_text")
            and before.get("body_html_length") == after.get("body_html_length")
        ):
            self._emit(
                kind="ui_no_change",
                target=target,
                target_outer_html_unchanged=True,
                body_text_length=len(after.get("body_text") or ""),
                body_html_length=after.get("body_html_length"),
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


__all__ = ["DomAssertionsProbe"]
