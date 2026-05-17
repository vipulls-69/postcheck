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

  // IndexedDB wrapper. The IDB API is event-based, not promise-based, so
  // we intercept the request prototype's open(), then — once a DB is
  // available — wrap its transaction()/objectStore()/put/add/delete chain
  // so individual write failures (QuotaExceededError, DataCloneError)
  // surface as their own events. Each wrapped object carries a
  // ``__postcheck_wrapped`` marker so we never double-wrap.
  const wrapStoreMethod = (store, methodName, dbName, storeName) => {
    let orig;
    try { orig = store[methodName].bind(store); } catch (_) { return; }
    store[methodName] = function (...args) {
      // Best-effort key extraction: put/add take (value, key?), delete/get
      // take (key). The cursor over args picks the one that's likely the
      // key string; falls back to '<key>' if nothing scalar is present.
      let keyHint = null;
      try {
        if (methodName === 'delete' || methodName === 'get') {
          keyHint = args[0];
        } else if (args.length >= 2) {
          keyHint = args[1];
        } else if (args[0] && typeof args[0] === 'object' && 'id' in args[0]) {
          keyHint = args[0].id;
        }
      } catch (_) {}
      let req;
      try {
        req = orig(...args);
      } catch (err) {
        push({
          kind: 'idb_write',
          database: dbName,
          store: storeName,
          operation: methodName,
          key: safeStr(keyHint),
          success: false,
          error: safeStr(err),
          errorName: err && err.name,
        });
        throw err;
      }
      try {
        req.addEventListener('success', () => {
          push({
            kind: 'idb_write',
            database: dbName,
            store: storeName,
            operation: methodName,
            key: safeStr(keyHint != null ? keyHint : (req.result && req.result.toString())),
            success: true,
          });
        });
        req.addEventListener('error', (ev) => {
          const err = req.error;
          push({
            kind: 'idb_write',
            database: dbName,
            store: storeName,
            operation: methodName,
            key: safeStr(keyHint),
            success: false,
            error: safeStr(err),
            errorName: err && err.name,
          });
          // Don't preventDefault — leave the app's own onerror semantics alone.
        });
      } catch (_) {}
      return req;
    };
  };

  const wrapDb = (db, dbName) => {
    if (!db || db.__postcheck_wrapped) return;
    try { db.__postcheck_wrapped = true; } catch (_) { return; }
    let origTxn;
    try { origTxn = db.transaction.bind(db); } catch (_) { return; }
    db.transaction = function (...txnArgs) {
      const txn = origTxn(...txnArgs);
      try {
        const origObjectStore = txn.objectStore.bind(txn);
        txn.objectStore = function (storeName) {
          const store = origObjectStore(storeName);
          if (!store.__postcheck_wrapped) {
            try { store.__postcheck_wrapped = true; } catch (_) {}
            wrapStoreMethod(store, 'put', dbName, storeName);
            wrapStoreMethod(store, 'add', dbName, storeName);
            wrapStoreMethod(store, 'delete', dbName, storeName);
          }
          return store;
        };
      } catch (_) {}
      return txn;
    };
  };

  if (window.indexedDB && typeof window.indexedDB.open === 'function') {
    const origOpen = window.indexedDB.open.bind(window.indexedDB);
    window.indexedDB.open = function (name, version) {
      const req = origOpen(name, version);
      let upgradeAttempted = false;
      try {
        req.addEventListener('upgradeneeded', () => {
          upgradeAttempted = true;
          // Wrap the DB before the app's own upgrade handler runs, so any
          // store mutations during upgrade are also instrumented.
          try { wrapDb(req.result, safeStr(name)); } catch (_) {}
        });
        req.addEventListener('blocked', () => {
          push({
            kind: 'idb_blocked',
            database: safeStr(name),
            version: version != null ? version : null,
          });
        });
        req.addEventListener('error', () => {
          const err = req.error;
          push({
            kind: 'idb_error',
            database: safeStr(name),
            source: 'open',
            phase: upgradeAttempted ? 'upgradeneeded' : 'open',
            error: err ? safeStr(err) : 'open error',
            errorName: err && err.name,
          });
        });
        req.addEventListener('success', () => {
          push({
            kind: 'idb_open',
            database: safeStr(name),
            operation: 'open',
            success: true,
          });
          try { wrapDb(req.result, safeStr(name)); } catch (_) {}
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
        "storage_idb_version_error",
        "storage_idb_quota_error",
        "storage_idb_blocked",
        "storage_idb_serialization_error",
    }
)

# In-page records use one of these ``kind`` strings; the builder below
# maps them onto the canonical ``ProbeEvent`` kinds above.
_IDB_RAW_KINDS = frozenset({"idb_open", "idb_write", "idb_error", "idb_blocked"})


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
        if kind in _IDB_RAW_KINDS:
            return self._build_idb_event(record)
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

    # ------------------------------------------------------------------
    # IndexedDB categorisation
    # ------------------------------------------------------------------

    def _build_idb_event(self, record: dict[str, Any]) -> ProbeEvent | None:
        """Map an in-page IDB record onto a canonical storage kind.

        Successful ``idb_open`` / ``idb_write`` become ``storage_write``
        (the informational kind the bug aggregator drops). Failures fan
        out by ``errorName`` and ``phase``:

        * upgrade-context error or ``VersionError`` / ``AbortError`` →
          ``storage_idb_version_error``
        * ``QuotaExceededError`` → ``storage_idb_quota_error``
        * ``DataCloneError`` → ``storage_idb_serialization_error``
        * blocked event → ``storage_idb_blocked``
        * anything else → ``storage_idb_version_error`` as a catch-all so
          we don't silently drop a real IDB failure (fail open).
        """
        raw = record.get("kind")
        success = bool(record.get("success"))
        error_name = record.get("errorName")
        phase = record.get("phase")

        if raw == "idb_blocked":
            canonical = "storage_idb_blocked"
        elif raw in ("idb_open", "idb_write") and success:
            canonical = "storage_write"
        elif raw == "idb_error" or (
            raw in ("idb_open", "idb_write") and not success
        ):
            if error_name == "QuotaExceededError":
                canonical = "storage_idb_quota_error"
            elif error_name == "DataCloneError":
                canonical = "storage_idb_serialization_error"
            elif error_name in ("VersionError", "AbortError") or phase == (
                "upgradeneeded"
            ):
                canonical = "storage_idb_version_error"
            else:
                # Unknown IDB failure — surface it under version_error
                # rather than dropping. The error string carries the
                # detail; aggregator renders ``errorName`` in the title.
                canonical = "storage_idb_version_error"
        else:
            return None

        payload = {
            "kind": canonical,
            "storage": "indexedDB",
            "operation": record.get("operation") or raw.removeprefix("idb_"),
            "database": record.get("database"),
            "store": record.get("store"),
            "key": record.get("key"),
            "success": success,
            "error": record.get("error"),
            "error_name": error_name,
            "phase": phase,
            "in_page_timestamp_ms": record.get("timestamp"),
            "captured_at": _utcnow_iso(),
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        return ProbeEvent(
            probe="storage",
            route=self._route_provider() or "",
            interaction_index=self._interaction_provider(),
            payload=payload,
        )


__all__ = ["StorageProbe"]
