"""Integration test for ``run_scenario`` against a real Chromium + http server.

Gated behind ``POSTCHECK_RUN_BROWSER_TESTS=1`` because Playwright 1.59 +
Python 3.14 on macOS arm64 currently SIGKILLs the headless shell on launch
(see ``tests/README.md``). Set the env var on a working environment
(e.g. Python 3.13 or a Linux runner) to exercise the real browser path.
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import AsyncIterator

import pytest

from postcheck.browser.scenario_runner import ScenarioRunConfig, run_scenario
from postcheck.browser.target_locator import LocatedTarget, locate
from postcheck.core.types import (
    AffectedRoute,
    ProbeEvent,
    Selector,
)


_RUN_BROWSER = os.environ.get("POSTCHECK_RUN_BROWSER_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not _RUN_BROWSER,
    reason=(
        "Set POSTCHECK_RUN_BROWSER_TESTS=1 to exercise the real-browser "
        "scenario runner — see tests/README.md."
    ),
)


_HTML = """<!doctype html>
<html><body>
  <h1>Scenario fixture</h1>
  <button data-testid="save">Save</button>
  <button data-testid="cancel">Cancel</button>
  <input data-testid="email" type="email" />
  <script>
    const log = [];
    document.querySelector('[data-testid="save"]')
      .addEventListener('click', () => log.push('save-click'));
    document.querySelector('[data-testid="cancel"]')
      .addEventListener('click', () => log.push('cancel-click'));
    window.__log = log;
  </script>
</body></html>
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_: object) -> None:  # pragma: no cover
        pass


@asynccontextmanager
async def _serve(directory: Path) -> AsyncIterator[str]:
    handler = type(
        "Handler",
        (_QuietHandler,),
        {"directory": str(directory)},
    )
    # Python 3.7+ SimpleHTTPRequestHandler accepts ``directory`` via init,
    # so use a small subclass that bakes it in.
    def factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleHTTPRequestHandler(*args, directory=str(directory), **kwargs)

    server = HTTPServer(("127.0.0.1", 0), factory)  # type: ignore[arg-type]
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _RecordingProbe:
    """Subscribes to ``page.on('console')`` so we can verify probe events flow."""

    name = "runtime"

    def __init__(self) -> None:
        self._events: list[ProbeEvent] = []
        self._page = None

    async def attach(self, page) -> None:
        self._page = page

        def _on_console(msg) -> None:  # type: ignore[no-untyped-def]
            self._events.append(
                ProbeEvent(
                    probe="runtime",
                    route="",
                    payload={"text": msg.text, "type": msg.type},
                )
            )

        page.on("console", _on_console)

    def collect_events(self) -> list[ProbeEvent]:
        out, self._events = self._events, []
        return out

    async def detach(self) -> None:
        # Playwright doesn't expose page.off easily; rely on context teardown.
        self._page = None


async def test_full_scenario_against_static_html(tmp_path: Path) -> None:
    from playwright.async_api import async_playwright

    (tmp_path / "index.html").write_text(_HTML, encoding="utf-8")

    async with _serve(tmp_path) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()

                # Navigate once to bind selectors via target_locator.
                await page.goto(f"{base_url}/index.html", wait_until="load")
                affected = AffectedRoute(
                    route="/index.html",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="save"),
                        Selector(strategy="test_id", value="cancel"),
                    ],
                )
                located = await locate(page, affected)
                assert located.failures == []
                assert len(located.targets) == 2

                probe = _RecordingProbe()
                cfg = ScenarioRunConfig(
                    route="/index.html",
                    url=f"{base_url}/index.html",
                    settle_ms=20,
                )
                events = await run_scenario(page, located.targets, [probe], config=cfg)

                # Both interactions ran in order — the script logged each click.
                log = await page.evaluate("window.__log")
                assert log == ["save-click", "cancel-click"]

                # The runner returned events tagged with the right route.
                for ev in events:
                    assert ev.route == "/index.html"

                # No scenario_failure events — happy path.
                assert all(ev.probe != "scenario" for ev in events)
            finally:
                await browser.close()
