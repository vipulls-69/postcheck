"""Integration test for ``StorageProbe`` against real Chromium.

Gated behind ``POSTCHECK_RUN_BROWSER_TESTS=1`` because Playwright 1.59 +
Python 3.14 on macOS arm64 SIGKILLs the headless shell on launch (see
``tests/README.md``). Set the env var on Python 3.13 / a Linux runner to
exercise the real browser.

Three buttons:

* ``ok``   \u2014 plain ``localStorage.setItem('user', 'alice')`` \u2192 storage_write.
* ``quota`` \u2014 writes a string in a loop until the browser raises
  ``QuotaExceededError`` \u2192 storage_quota_error.
* ``circular`` \u2014 ``localStorage.setItem('c', JSON.stringify(circular))``
  whose ``JSON.stringify`` throws ``TypeError: Converting circular
  structure to JSON`` \u2192 storage_serialization_error.
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import AsyncIterator

import pytest

from postcheck.browser.scenario_runner import ScenarioRunConfig, run_scenario
from postcheck.browser.target_locator import locate
from postcheck.core.types import AffectedRoute, Selector
from postcheck.probes.storage_probe import StorageProbe


_RUN_BROWSER = os.environ.get("POSTCHECK_RUN_BROWSER_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not _RUN_BROWSER,
    reason=(
        "Set POSTCHECK_RUN_BROWSER_TESTS=1 to exercise the real-browser "
        "storage probe \u2014 see tests/README.md."
    ),
)


_HTML = """<!doctype html>
<html><body>
  <button data-testid="ok">ok</button>
  <button data-testid="quota">quota</button>
  <button data-testid="circular">circular</button>
  <script>
    document.querySelector('[data-testid="ok"]').addEventListener('click', () => {
      try { localStorage.setItem('user', 'alice'); } catch (e) {}
    });
    document.querySelector('[data-testid="quota"]').addEventListener('click', () => {
      try {
        let s = 'x';
        for (let i = 0; i < 25; i++) s += s;  // ~33MB \u2014 well over the 5MB cap
        localStorage.setItem('blob', s);
      } catch (e) { /* swallow so runtime probe doesn't also flag it */ }
    });
    document.querySelector('[data-testid="circular"]').addEventListener('click', () => {
      try {
        const a = {}; a.self = a;
        localStorage.setItem('c', JSON.stringify(a));
      } catch (e) { /* swallow */ }
    });
  </script>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # pragma: no cover
        pass

    def do_GET(self):  # noqa: N802
        body = _HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@asynccontextmanager
async def _serve() -> AsyncIterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_storage_probe_captures_write_quota_and_serialization() -> None:
    from playwright.async_api import async_playwright

    async with _serve() as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await (await browser.new_context()).new_page()
                probe = StorageProbe()

                affected = AffectedRoute(
                    route="/",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="ok"),
                        Selector(strategy="test_id", value="quota"),
                        Selector(strategy="test_id", value="circular"),
                    ],
                )
                # Probe must be attached *before* navigation so add_init_script
                # applies. The runner attaches before goto; simulate by
                # going to about:blank, attaching, then running.
                await probe.attach(page)
                located = await locate(page, affected)
                assert located.failures == []

                cfg = ScenarioRunConfig(
                    route="/", url=f"{base_url}/", settle_ms=200
                )
                events = await run_scenario(
                    page, located.targets, [probe], config=cfg
                )
            finally:
                await browser.close()

    storage = [e for e in events if e.probe == "storage"]
    kinds = {e.payload["kind"] for e in storage}
    assert "storage_write" in kinds, storage
    assert "storage_quota_error" in kinds, storage
    assert "storage_serialization_error" in kinds, storage

    # ok event correlates with the first interaction.
    ok = next(
        e for e in storage
        if e.payload["kind"] == "storage_write"
        and e.payload.get("key") == "user"
    )
    assert ok.interaction_index == 0
    assert ok.payload["success"] is True


# ---------------------------------------------------------------------------
# ISSUE B regression coverage: IndexedDB instrumentation. The inject
# script wraps ``indexedDB.open`` + the resulting DB's
# ``transaction()/objectStore()/put/add/delete`` chain. Failures fan out
# into ``storage_idb_*`` kinds; successful writes stay as the
# informational ``storage_write`` kind (so the bug aggregator drops them).
# ---------------------------------------------------------------------------


_IDB_WRITE_HTML = """<!doctype html>
<html><body>
  <button data-testid="idb-write">write</button>
  <script>
    document.querySelector('[data-testid="idb-write"]').addEventListener('click', () => {
      const req = indexedDB.open('pcdb_ok', 1);
      req.onupgradeneeded = (ev) => {
        ev.target.result.createObjectStore('items', {keyPath: 'id'});
      };
      req.onsuccess = (ev) => {
        const db = ev.target.result;
        const tx = db.transaction('items', 'readwrite');
        const store = tx.objectStore('items');
        store.put({id: 'k1', value: 'hello'});
      };
    });
  </script>
</body></html>
"""


_IDB_VERSION_ERR_HTML = """<!doctype html>
<html><body>
  <button data-testid="idb-bad-upgrade">bad upgrade</button>
  <script>
    document.querySelector('[data-testid="idb-bad-upgrade"]').addEventListener('click', () => {
      // First open v1 so the DB exists, then immediately open v2 with
      // an onupgradeneeded handler that throws — IDB aborts the upgrade
      // transaction and surfaces the failure on the open request.
      const r1 = indexedDB.open('pcdb_bad', 1);
      r1.onsuccess = () => {
        r1.result.close();
        const r2 = indexedDB.open('pcdb_bad', 2);
        r2.onupgradeneeded = () => { throw new Error('upgrade-failed'); };
        r2.onerror = () => {};  // swallow so runtime_probe doesn't double-flag
      };
    });
  </script>
</body></html>
"""


def _serve_html_factory(html: str):
    """Return a serving context that hosts ``html`` at /index.html."""

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *_):  # pragma: no cover
            pass

        def do_GET(self):  # noqa: N802
            if self.path in ("/", "/index.html"):
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    @asynccontextmanager
    async def ctx() -> AsyncIterator[str]:
        server = HTTPServer(("127.0.0.1", 0), _H)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    return ctx


async def _drive_idb(html: str, testid: str) -> list:
    from playwright.async_api import async_playwright

    serve_ctx = _serve_html_factory(html)
    async with serve_ctx() as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await (await browser.new_context()).new_page()
                probe = StorageProbe()
                # add_init_script must be installed before navigation.
                await probe.attach(page)
                await page.goto(f"{base_url}/", wait_until="load")

                affected = AffectedRoute(
                    route="/",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value=testid),
                    ],
                )
                located = await locate(page, affected)
                assert located.failures == [], located.failures

                cfg = ScenarioRunConfig(
                    route="/",
                    url=f"{base_url}/",
                    settle_ms=600,
                )
                events = await run_scenario(
                    page, located.targets, [probe], config=cfg
                )
                return [e for e in events if e.probe == "storage"]
            finally:
                await browser.close()


async def test_idb_write_succeeds_no_bug() -> None:
    """A normal IDB put surfaces as ``storage_write`` (informational) —
    the aggregator drops it; it must not become a bug.
    """
    from postcheck.reporting.bug_aggregator import aggregate_bugs

    storage = await _drive_idb(_IDB_WRITE_HTML, "idb-write")
    # The put succeeded — at least one storage_write event tagged IDB.
    idb_writes = [
        e for e in storage
        if e.payload.get("kind") == "storage_write"
        and e.payload.get("storage") == "indexedDB"
    ]
    assert idb_writes, [e.payload for e in storage]
    # And no IDB error events.
    assert not any(
        e.payload.get("kind", "").startswith("storage_idb_")
        for e in storage
    ), [e.payload for e in storage]
    # Aggregator drops the informational events — no bug.
    bugs = aggregate_bugs(storage)
    assert bugs == [], [b.title for b in bugs]


async def test_idb_version_error_emits_event() -> None:
    """An ``onupgradeneeded`` handler that throws aborts the upgrade
    transaction; the probe categorises the resulting open failure as
    ``storage_idb_version_error``.
    """
    storage = await _drive_idb(_IDB_VERSION_ERR_HTML, "idb-bad-upgrade")
    version_errors = [
        e for e in storage
        if e.payload.get("kind") == "storage_idb_version_error"
    ]
    assert version_errors, [e.payload for e in storage]
    ev = version_errors[0]
    assert ev.payload.get("storage") == "indexedDB"
    assert ev.payload.get("database") == "pcdb_bad"


async def test_idb_blocked_skipped_without_two_contexts() -> None:
    """Reproducing ``onblocked`` requires one context holding an old
    version while another tries to upgrade. The probe supports it (the
    inject script forwards the ``blocked`` event to ``idb_blocked``),
    but exercising it end-to-end needs two browser contexts whose
    lifetimes overlap precisely — racy in CI. We document the gap here
    and exercise the ``idb_blocked`` \u2192 ``storage_idb_blocked`` mapping
    via a unit test in :mod:`tests.unit.test_storage_probe` instead.
    """
    pytest.skip(
        "Requires two overlapping browser contexts; mapping covered by unit test."
    )
