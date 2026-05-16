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
