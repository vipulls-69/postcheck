"""Storage probe \u2014 wraps Web Storage and IndexedDB to surface failures (v0).

Approach
--------
We can't hook the in-page storage APIs from outside the browser, so the
probe injects a small JS shim via :meth:`Page.add_init_script` *before*
navigation. The shim wraps:

* ``localStorage.setItem`` / ``removeItem``
* ``sessionStorage.setItem`` / ``removeItem``
* ``JSON.stringify`` (so that circular-reference :class:`TypeError` thrown
  *before* ``setItem`` is even called still gets attributed to a
  serialization issue rather than disappearing into ``runtime_probe``)
* ``indexedDB.open`` (capturing the request's ``error`` / ``success``
  events)

Each call appends a record to ``window.__postcheck_storage_events``.
:meth:`flush` does a single ``page.evaluate`` to drain and clear that
array; :meth:`collect_events` then returns the parsed Python events.
``flush`` is the async hook the scenario runner awaits before each sync
``collect_events`` call.

Emitted ``payload.kind``
------------------------
* ``storage_write`` \u2014 every successful or non-quota-failed mutation
  (``setItem`` / ``removeItem`` / ``indexedDB.open``). The bug aggregator
  decides what's a bug; v0 doesn't flag plain writes.
* ``storage_quota_error`` \u2014 ``QuotaExceededError`` from Web Storage.
* ``storage_serialization_error`` \u2014 ``TypeError`` from ``JSON.stringify``
  with a message matching circular / cyclic / "converting" (the three
  shapes browsers use for the same condition).

v0 limitations
--------------
* Typo-in-key detection is out of scope \u2014 it requires diff context the
  v0 impact mapper does not propagate this deep.
* ``IndexedDB`` per-transaction errors aren't traced; only ``open()``
  failures are.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from ..core.types import ProbeEvent
from .shared import get_current_route, get_interaction_index

if TYPE_CHECKING:
    from playwright.async_api import Page


_INIT_SCRIPT = r"""
(() => {
  if (window.__postcheck_storage_installed) return;
  window.__postcheck_storage_installed = true;
  window.__postcheck_storage_events = [];

  const push = (e) => {
    e.timestamp = Date.now();
    try { window.__postcheck_storage_events.push(e); } catch (_) {}
  };

  const safeStr = (v) => {
    try { return String(v); } catch (_) { return '<unstringifiable>'; }
  };

  const wrapStorage = (storage, name) => {
    if (!storage) return;
    let origSet, origRemove;
    try { origSet = storage.setItem.bind(storage); } catch (_) { return; }
    try { origRemove = storage.removeItem.bind(storage); } catch (_) { return; }

    storage.setItem = function (key, value) {
      const strValue = safeStr(value);
      try {
        origSet(key, strValue);
        push({
          kind: 'storage_write',
          storage: name,
          operation: 'setItem',
          key: safeStr(key),
          success: true,
          size: strValue.length,
        });
      } catch (err) {
        const isQuota =
          err && (err.name === 'QuotaExceededError' ||
                  err.name === 'NS_ERROR_DOM_QUOTA_REACHED' ||
                  (err.code !== undefined && err.code === 22));
        push({
          kind: isQuota ? 'storage_quota_error' : 'storage_write',
          storage: name,
          operation: 'setItem',
          key: safeStr(key),
          success: false,
          error: safeStr(err),
          attempted_size: strValue.length,
        });
        // Re-throw so the app sees the same behaviour it would without us.
        throw err;
      }
    };

    storage.removeItem = function (key) {
      try {
        origRemove(key);
        push({
          kind: 'storage_write',
          storage: name,
          operation: 'removeItem',
          key: safeStr(key),
          success: true,
        });
      } catch (err) {
        push({
          kind: 'storage_write',
          storage: name,
          operation: 'removeItem',
          key: safeStr(key),
          success: false,
          error: safeStr(err),
        });
        throw err;
      }
    };
  };

  try { wrapStorage(window.localStorage, 'localStorage'); } catch (_) {}
  try { wrapStorage(window.sessionStorage, 'sessionStorage'); } catch (_) {}

  // JSON.stringify wrapper \u2014 catches circular refs that throw before setItem.
  if (typeof JSON !== 'undefined' && typeof JSON.stringify === 'function') {
    const origStringify = JSON.stringify;
    JSON.stringify = function (...args) {
      try {
        return origStringify.apply(this, args);
      } catch (err) {
        const msg = safeStr(err);
        if (
          err && err.name === 'TypeError' &&
          /circular|cyclic|converting circular structure/i.test(msg)
        ) {
          push({
            kind: 'storage_serialization_error',
            storage: 'json',
            operation: 'stringify',
            key: null,
            success: false,
            error: msg,
          });
        }
        throw err;
      }
    };
  }

  // IndexedDB.open wrapper.
  if (window.indexedDB && typeof window.indexedDB.open === 'function') {
    const origOpen = window.indexedDB.open.bind(window.indexedDB);
    window.indexedDB.open = function (name, version) {
      const req = origOpen(name, version);
      try {
        req.addEventListener('error', () => {
          const err = req.error ? safeStr(req.error) : 'open error';
          push({
            kind: 'storage_write',
            storage: 'indexedDB',
            operation: 'open',
            key: safeStr(name),
            success: false,
            error: err,
          });
        });
        req.addEventListener('success', () => {
          push({
            kind: 'storage_write',
            storage: 'indexedDB',
            operation: 'open',
            key: safeStr(name),
            success: true,
          });
        });
      } catch (_) {}
      return req;
    };
  }
})();
"""


_DRAIN_SCRIPT = (
    "() => { "
    "  const e = window.__postcheck_storage_events || []; "
    "  window.__postcheck_storage_events = []; "
    "  return e; "
    "}"
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_VALID_KINDS = frozenset(
    {
        "storage_write",
        "storage_quota_error",
        "storage_serialization_error",
    }
)


class StorageProbe:
    """Records Web Storage / IndexedDB mutations + serialization errors."""

    name = "storage"

    def __init__(
        self,
        *,
        interaction_provider: Callable[[], int | None] | None = None,
        route_provider: Callable[[], str] | None = None,
    ) -> None:
        self._interaction_provider = interaction_provider or get_interaction_index
        self._route_provider = route_provider or get_current_route
        self._buffer: list[ProbeEvent] = []
        self._page: Page | None = None
        # We can't ``page.remove_init_script``; instead we mark the probe
        # detached so subsequent flush()/collect_events become no-ops.
        self._detached = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def attach(self, page: Page) -> None:
        if self._page is not None:
            return
        self._page = page
        self._detached = False
        # Install on every new document for the lifetime of the page.
        await page.add_init_script(_INIT_SCRIPT)

    async def flush(self) -> None:
        """Drain ``window.__postcheck_storage_events`` via ``page.evaluate``."""
        if self._page is None or self._detached:
            return
        try:
            raw = await self._page.evaluate(_DRAIN_SCRIPT)
        except Exception:
            # Pre-navigation, or page closed mid-flush: nothing to drain.
            return
        if not isinstance(raw, list):
            return
        for record in raw:
            if not isinstance(record, dict):
                continue
            event = self._build_event(record)
            if event is not None:
                self._buffer.append(event)

    def collect_events(self) -> list[ProbeEvent]:
        out, self._buffer = self._buffer, []
        return out

    async def detach(self) -> None:
        # No public API to remove an init script, so we just mark detached.
        self._detached = True
        self._page = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_event(self, record: dict[str, Any]) -> ProbeEvent | None:
        kind = record.get("kind")
        if kind not in _VALID_KINDS:
            return None
        payload = {
            "kind": kind,
            "storage": record.get("storage"),
            "operation": record.get("operation"),
            "key": record.get("key"),
            "success": record.get("success"),
            "error": record.get("error"),
            "size": record.get("size"),
            "attempted_size": record.get("attempted_size"),
            "in_page_timestamp_ms": record.get("timestamp"),
            "captured_at": _utcnow_iso(),
        }
        # Strip Nones to keep payloads tidy and JSON-serialisable in DB.
        payload = {k: v for k, v in payload.items() if v is not None}
        return ProbeEvent(
            probe="storage",
            route=self._route_provider() or "",
            interaction_index=self._interaction_provider(),
            payload=payload,
        )


__all__ = ["StorageProbe"]
