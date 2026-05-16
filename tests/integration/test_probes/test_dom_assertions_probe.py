"""Integration test for ``DomAssertionsProbe`` against real Chromium.

Gated behind ``POSTCHECK_RUN_BROWSER_TESTS=1`` (Playwright 1.59 + Python
3.14 on macOS arm64 SIGKILLs the headless shell on launch — see
``tests/README.md``). Set the env var on Python 3.13 / a Linux runner to
exercise the real browser.

Three buttons:

* ``increment``   - click increments a count rendered elsewhere in the
  body -> body text changes -> NO ``ui_no_change``.
* ``noop``        - click handler does nothing -> body unchanged ->
  ``ui_no_change`` emitted.
* ``covered``     - permanently covered by a fixed full-viewport overlay
  -> ``ui_overlay_blocks`` emitted in the ``before`` phase.
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
from postcheck.probes.ui_probe import DomAssertionsProbe


_RUN_BROWSER = os.environ.get("POSTCHECK_RUN_BROWSER_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not _RUN_BROWSER,
    reason=(
        "Set POSTCHECK_RUN_BROWSER_TESTS=1 to exercise the real-browser "
        "DOM assertions probe — see tests/README.md."
    ),
)


_HTML = """<!doctype html>
<html><body>
  <div id="count">0</div>
  <button data-testid="increment">+</button>
  <button data-testid="noop">noop</button>
  <button data-testid="covered" style="position:absolute;top:200px;left:0;width:100px;height:30px;">hidden</button>
  <div id="overlay" style="
        position:fixed;top:0;left:0;width:100vw;height:100vh;
        background:rgba(0,0,0,0.4);z-index:9999;"></div>
  <script>
    let n = 0;
    document.querySelector('[data-testid="increment"]').addEventListener('click', () => {
      n += 1;
      document.querySelector('#count').textContent = String(n);
    });
    document.querySelector('[data-testid="noop"]').addEventListener('click', () => {});
    document.querySelector('[data-testid="covered"]').addEventListener('click', () => {
      // never fires — overlay intercepts
      n = 9999;
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


async def test_dom_assertions_probe_no_change_and_overlay() -> None:
    from playwright.async_api import async_playwright

    async with _serve() as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await (await browser.new_context()).new_page()
                affected = AffectedRoute(
                    route="/",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="increment"),
                        Selector(strategy="test_id", value="noop"),
                        Selector(strategy="test_id", value="covered"),
                    ],
                )
                await page.goto(f"{base_url}/", wait_until="load")
                located = await locate(page, affected)
                assert located.failures == []

                probe = DomAssertionsProbe()
                # Tighter interaction timeout so the covered click fails
                # quickly with "intercepts pointer events".
                cfg = ScenarioRunConfig(
                    route="/",
                    url=f"{base_url}/",
                    settle_ms=150,
                    interaction_timeout_ms=2_500,
                )
                events = await run_scenario(
                    page, located.targets, [probe], config=cfg
                )
            finally:
                await browser.close()

    ui = [e for e in events if e.probe == "ui"]
    by_kind: dict[str, list] = {}
    for e in ui:
        by_kind.setdefault(e.payload["kind"], []).append(e)

    # increment changes the count -> no ui_no_change for index 0.
    no_change_indices = {
        e.interaction_index for e in by_kind.get("ui_no_change", [])
    }
    assert 0 not in no_change_indices, ui

    # noop click -> ui_no_change at index 1.
    assert 1 in no_change_indices, ui

    # covered button -> ui_overlay_blocks (before phase) at index 2.
    overlays = by_kind.get("ui_overlay_blocks", [])
    assert any(
        e.interaction_index == 2 and e.payload.get("phase") == "before"
        for e in overlays
    ), overlays
