"""Integration test for ``NetworkProbe`` against real Chromium + http servers.

Gated behind ``POSTCHECK_RUN_BROWSER_TESTS=1`` because Playwright 1.59 +
Python 3.14 on macOS arm64 SIGKILLs the headless shell on launch (see
``tests/README.md``). Set the env var on Python 3.13 / a Linux runner to
exercise the real browser.

The test serves two HTTP origins on loopback:

* ``main`` — hosts the page; ``/api/error`` returns 500, ``/api/ok``
  returns 200, ``/__vite_ping`` returns 500 (must be ignored), and the
  page wires a button to fetch each of those plus a cross-origin URL.
* ``other`` — a separate origin whose ``/api/x`` returns 500 (must be
  recorded with ``medium`` confidence under default settings).

Verifies the three-layer filter end-to-end on the wire.
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import AsyncIterator

import pytest

from postcheck.browser.scenario_runner import ScenarioRunConfig, run_scenario
from postcheck.browser.target_locator import locate
from postcheck.core.config import NetworkSettings
from postcheck.core.types import AffectedRoute, Selector
from postcheck.probes.network_probe import NetworkProbe


_RUN_BROWSER = os.environ.get("POSTCHECK_RUN_BROWSER_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not _RUN_BROWSER,
    reason=(
        "Set POSTCHECK_RUN_BROWSER_TESTS=1 to exercise the real-browser "
        "network probe — see tests/README.md."
    ),
)


def _html(other_origin: str) -> str:
    return f"""<!doctype html>
<html><body>
  <h1>Network probe fixture</h1>
  <button data-testid="hit-error">500</button>
  <button data-testid="hit-ok">200</button>
  <button data-testid="hit-hmr">HMR</button>
  <button data-testid="hit-other">cross</button>
  <script>
    const make = (url) => fetch(url).catch(() => {{}});
    document.querySelector('[data-testid="hit-error"]')
      .addEventListener('click', () => make('/api/error'));
    document.querySelector('[data-testid="hit-ok"]')
      .addEventListener('click', () => make('/api/ok'));
    document.querySelector('[data-testid="hit-hmr"]')
      .addEventListener('click', () => make('/__vite_ping?t=1'));
    document.querySelector('[data-testid="hit-other"]')
      .addEventListener('click', () => make('{other_origin}/api/x'));
  </script>
</body></html>
"""


def _make_main_handler(html_body: str):
    class _MainHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # pragma: no cover
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")

        def do_GET(self):  # noqa: N802
            if self.path == "/" or self.path == "/index.html":
                body = html_body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/ok":
                self.send_response(200)
                self.end_headers()
            elif self.path == "/api/error":
                self.send_response(500)
                self.end_headers()
            elif self.path.startswith("/__vite_ping"):
                self.send_response(500)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

    return _MainHandler


class _OtherHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # pragma: no cover
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/api/x":
            self.send_response(500)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


@asynccontextmanager
async def _serve(handler_cls) -> AsyncIterator[str]:
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_network_probe_three_layer_filter(tmp_path: Path) -> None:
    from playwright.async_api import async_playwright

    async with _serve(_OtherHandler) as other_origin:
        main_handler = _make_main_handler(_html(other_origin))
        async with _serve(main_handler) as base_url:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                try:
                    context = await browser.new_context()
                    page = await context.new_page()

                    await page.goto(f"{base_url}/", wait_until="load")
                    affected = AffectedRoute(
                        route="/",
                        reason="direct",
                        confidence="high",
                        suspected_selectors=[
                            Selector(strategy="test_id", value="hit-error"),
                            Selector(strategy="test_id", value="hit-ok"),
                            Selector(strategy="test_id", value="hit-hmr"),
                            Selector(strategy="test_id", value="hit-other"),
                        ],
                    )
                    located = await locate(page, affected)
                    assert located.failures == []

                    probe = NetworkProbe(NetworkSettings())  # defaults
                    cfg = ScenarioRunConfig(
                        route="/", url=f"{base_url}/", settle_ms=100
                    )
                    events = await run_scenario(
                        page, located.targets, [probe], config=cfg
                    )
                finally:
                    await browser.close()

    net_events = [e for e in events if e.probe == "network"]
    by_url = {e.payload["url"]: e for e in net_events}

    # 500 same-origin → high.
    same_origin_err = next(
        (e for u, e in by_url.items() if u.endswith("/api/error")), None
    )
    assert same_origin_err is not None, f"missing same-origin 500; saw {by_url!r}"
    assert same_origin_err.payload["kind"] == "network_error"
    assert same_origin_err.payload["status"] == 500
    assert same_origin_err.payload["confidence"] == "high"
    assert same_origin_err.interaction_index == 0

    # 200 → no event.
    assert not any(u.endswith("/api/ok") for u in by_url), by_url

    # HMR-style URL → ignored entirely.
    assert not any("__vite_ping" in u for u in by_url), by_url

    # Cross-origin 500 → medium under defaults.
    cross_err = next(
        (e for u, e in by_url.items() if other_origin in u), None
    )
    assert cross_err is not None, f"missing cross-origin 500; saw {by_url!r}"
    assert cross_err.payload["confidence"] == "medium"
    assert cross_err.interaction_index == 3


# ---------------------------------------------------------------------------
# ISSUE A regression coverage: ``request.failure`` strings get classified
# into distinct ``payload.kind`` values (network_aborted / network_timeout
# / network_dns_error / network_connection_error) instead of all
# collapsing into ``network_error``.
# ---------------------------------------------------------------------------


_ABORT_HTML = """<!doctype html>
<html><body>
  <button data-testid="go-abort">abort</button>
  <button data-testid="go-timeout">timeout</button>
  <script>
    window.__results = {};
    document.querySelector('[data-testid="go-abort"]').addEventListener('click', () => {
      const c = new AbortController();
      const p = fetch('/slow', {signal: c.signal}).catch((e) => { window.__results.abort = String(e); });
      // Abort on the next microtask so the request is in flight.
      setTimeout(() => c.abort(), 10);
      return p;
    });
    document.querySelector('[data-testid="go-timeout"]').addEventListener('click', () => {
      // AbortSignal.timeout(ms) — fires "TimeoutError" via AbortError name.
      return fetch('/slow', {signal: AbortSignal.timeout(50)})
        .catch((e) => { window.__results.timeout = String(e); });
    });
  </script>
</body></html>
"""


def _slow_handler():
    import time

    class _Slow(BaseHTTPRequestHandler):
        def log_message(self, *_):  # pragma: no cover
            pass

        def do_GET(self):  # noqa: N802
            if self.path == "/" or self.path == "/index.html":
                body = _ABORT_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/slow":
                # Hold the connection so the abort/timeout fires first.
                try:
                    time.sleep(2)
                    self.send_response(200)
                    self.end_headers()
                except Exception:
                    pass
                return
            self.send_response(404)
            self.end_headers()

    return _Slow


async def _run_abort_fixture(testid: str) -> list:
    from playwright.async_api import async_playwright

    async with _serve(_slow_handler()) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()
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
                assert located.failures == []

                probe = NetworkProbe(NetworkSettings())
                cfg = ScenarioRunConfig(
                    route="/",
                    url=f"{base_url}/",
                    settle_ms=400,  # leave room for the abort/timeout to fire
                )
                events = await run_scenario(
                    page, located.targets, [probe], config=cfg
                )
                return [e for e in events if e.probe == "network"]
            finally:
                await browser.close()


async def test_aborted_fetch_emits_network_aborted() -> None:
    net = await _run_abort_fixture("go-abort")
    aborts = [e for e in net if e.payload.get("kind") == "network_aborted"]
    assert aborts, [e.payload for e in net]
    assert "/slow" in aborts[0].payload["url"]
    # The classifier read ``net::ERR_ABORTED`` from request.failure.
    assert "ABORTED" in (aborts[0].payload.get("error") or "")


async def test_timeout_emits_network_timeout() -> None:
    net = await _run_abort_fixture("go-timeout")
    # AbortSignal.timeout() also surfaces through Chromium as
    # ``net::ERR_ABORTED`` in some versions (the request was aborted by
    # the signal). Accept either ``network_timeout`` (when the underlying
    # net error string contains TIMED_OUT) or ``network_aborted``; assert
    # at minimum that we did *not* collapse to the generic kind and that
    # the failed request was captured.
    failed = [
        e for e in net
        if e.payload.get("phase") == "requestfailed"
        and "/slow" in e.payload.get("url", "")
    ]
    assert failed, [e.payload for e in net]
    kinds = {e.payload.get("kind") for e in failed}
    assert kinds & {"network_timeout", "network_aborted"}, kinds
