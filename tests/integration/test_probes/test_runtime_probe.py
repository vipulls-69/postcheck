"""Integration test for ``RuntimeProbe`` against a real Chromium.

Gated behind ``POSTCHECK_RUN_BROWSER_TESTS=1`` because Playwright 1.59 +
Python 3.14 on macOS arm64 currently SIGKILLs the headless shell on launch
(see ``tests/README.md``). Set the env var on a working environment
(Python 3.13 or a Linux runner) to exercise the real browser path.

Verifies end-to-end that:

1. A click on a button whose handler ``throw``\s surfaces as a
   ``runtime_error`` event.
2. The event is correlated with the interaction that triggered it
   (``interaction_index == 0``) thanks to the shared interaction context.
3. A ``console.error`` from a separate handler also surfaces, classified
   as ``runtime_error`` and tagged with the right interaction.
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
from postcheck.browser.target_locator import locate
from postcheck.core.types import AffectedRoute, Selector
from postcheck.probes.runtime_probe import RuntimeProbe


_RUN_BROWSER = os.environ.get("POSTCHECK_RUN_BROWSER_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not _RUN_BROWSER,
    reason=(
        "Set POSTCHECK_RUN_BROWSER_TESTS=1 to exercise the real-browser "
        "runtime probe — see tests/README.md."
    ),
)


_HTML = """<!doctype html>
<html><body>
  <h1>Runtime probe fixture</h1>
  <button data-testid="explode">Explode</button>
  <button data-testid="warn">Warn</button>
  <script>
    document.querySelector('[data-testid="explode"]')
      .addEventListener('click', () => {
        throw new TypeError('boom from click');
      });
    document.querySelector('[data-testid="warn"]')
      .addEventListener('click', () => {
        console.error('console error from click');
      });
  </script>
</body></html>
"""


@asynccontextmanager
async def _serve(directory: Path) -> AsyncIterator[str]:
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


async def test_runtime_probe_captures_click_triggered_error(tmp_path: Path) -> None:
    from playwright.async_api import async_playwright

    (tmp_path / "index.html").write_text(_HTML, encoding="utf-8")

    async with _serve(tmp_path) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()

                # Bind selectors first.
                await page.goto(f"{base_url}/index.html", wait_until="load")
                affected = AffectedRoute(
                    route="/index.html",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="explode"),
                        Selector(strategy="test_id", value="warn"),
                    ],
                )
                located = await locate(page, affected)
                assert located.failures == []

                probe = RuntimeProbe()
                cfg = ScenarioRunConfig(
                    route="/index.html",
                    url=f"{base_url}/index.html",
                    settle_ms=50,
                )
                events = await run_scenario(page, located.targets, [probe], config=cfg)
            finally:
                await browser.close()

    runtime_events = [e for e in events if e.probe == "runtime"]
    by_kind: dict[str, list] = {}
    for e in runtime_events:
        by_kind.setdefault(e.payload["kind"], []).append(e)

    # The throwing click produced a pageerror.
    errors = by_kind.get("runtime_error", [])
    assert errors, f"no runtime_error captured; got {runtime_events!r}"
    pageerror = next(
        (e for e in errors if e.payload.get("source") == "pageerror"), None
    )
    assert pageerror is not None
    assert "boom from click" in pageerror.payload["message"]
    assert pageerror.interaction_index == 0  # the "explode" click
    assert pageerror.route == "/index.html"

    # The second click logged a console error — now split into its own
    # ``runtime_console_error`` kind so it ranks below uncaught throws.
    console_errs = by_kind.get("runtime_console_error", [])
    assert console_errs, f"no console error captured; got {runtime_events!r}"
    assert any(
        "console error from click" in e.payload["message"] for e in console_errs
    )
    assert any(e.interaction_index == 1 for e in console_errs)


# ---------------------------------------------------------------------------
# ISSUE C regression coverage: ``console.error`` / ``console.warn`` get
# their own distinct kinds (``runtime_console_error`` /
# ``runtime_console_warning``) so the bug aggregator ranks them below
# uncaught ``pageerror`` throws. Driven via ``page.evaluate`` so we don't
# depend on click instrumentation.
# ---------------------------------------------------------------------------


async def test_console_error_from_handler_caught(tmp_path: Path) -> None:
    """A direct ``console.error`` call from page code surfaces as a
    ``ProbeEvent`` with ``payload.kind == 'runtime_console_error'`` \u2014
    *not* ``runtime_error`` (which is reserved for uncaught throws).
    """
    from playwright.async_api import async_playwright

    (tmp_path / "blank.html").write_text(
        "<!doctype html><html><body></body></html>", encoding="utf-8"
    )

    async with _serve(tmp_path) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()

                probe = RuntimeProbe()
                await probe.attach(page)
                await page.goto(f"{base_url}/blank.html", wait_until="load")
                # ``console.error`` from app code \u2014 the canonical test.
                await page.evaluate("console.error('hello from evaluate')")
                # Console events fire on the next microtask; a tiny eval
                # round-trip is enough to flush them to Python.
                await page.evaluate("1")

                events = probe.collect_events()
            finally:
                await browser.close()

    console_errs = [
        e for e in events
        if e.payload.get("kind") == "runtime_console_error"
    ]
    assert console_errs, [e.payload for e in events]
    assert any(
        "hello from evaluate" in e.payload.get("message", "")
        for e in console_errs
    )
    # And critically: NOT classified as the uncaught-throw bucket.
    assert not any(
        e.payload.get("kind") == "runtime_error" for e in events
    ), [e.payload for e in events]


async def test_console_warning_caught_at_warning_level(tmp_path: Path) -> None:
    """``console.warn`` becomes ``runtime_console_warning`` \u2014 the lowest
    runtime severity bucket.
    """
    from playwright.async_api import async_playwright

    (tmp_path / "blank.html").write_text(
        "<!doctype html><html><body></body></html>", encoding="utf-8"
    )

    async with _serve(tmp_path) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()

                probe = RuntimeProbe()
                await probe.attach(page)
                await page.goto(f"{base_url}/blank.html", wait_until="load")
                await page.evaluate("console.warn('careful now')")
                await page.evaluate("1")

                events = probe.collect_events()
            finally:
                await browser.close()

    warns = [
        e for e in events
        if e.payload.get("kind") == "runtime_console_warning"
    ]
    assert warns, [e.payload for e in events]
    assert any(
        "careful now" in e.payload.get("message", "") for e in warns
    )
