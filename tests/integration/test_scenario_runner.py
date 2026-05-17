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
from postcheck.probes.runtime_probe import RuntimeProbe
from postcheck.probes.storage_probe import StorageProbe
from postcheck.probes.ui_probe.dom_assertions import DomAssertionsProbe


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


# ---------------------------------------------------------------------------
# ISSUE B regression coverage: per-interaction failures route to the UI
# probe, not to ``scenario_failure``. ``scenario_failure`` stays reserved
# for harness-level errors (navigation, attach, no targets at all).
# ---------------------------------------------------------------------------


_HIDDEN_HTML = """<!doctype html>
<html><body>
  <h1>Hidden-button fixture</h1>
  <button data-testid="ghost" style="display:none">Ghost</button>
</body></html>
"""


_DETACH_HTML = """<!doctype html>
<html><body>
  <h1>Self-removing fixture</h1>
  <button data-testid="vanish">Vanish</button>
  <button data-testid="survivor">Survivor</button>
  <script>
    document.querySelector('[data-testid="vanish"]')
      .addEventListener('click', (ev) => { ev.target.remove(); });
    window.__survivorClicks = 0;
    document.querySelector('[data-testid="survivor"]')
      .addEventListener('click', () => { window.__survivorClicks += 1; });
  </script>
</body></html>
"""


async def test_hidden_element_emits_ui_event_not_scenario_failure(
    tmp_path: Path,
) -> None:
    """A click on a ``display:none`` button must become a ``ui_*`` event,
    not a ``scenario_failure``. The DomAssertionsProbe drains the
    queued failure inside ``collect_events``.
    """
    from playwright.async_api import async_playwright

    (tmp_path / "hidden.html").write_text(_HIDDEN_HTML, encoding="utf-8")

    async with _serve(tmp_path) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(f"{base_url}/hidden.html", wait_until="load")

                affected = AffectedRoute(
                    route="/hidden.html",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="ghost"),
                    ],
                )
                located = await locate(page, affected)
                assert located.failures == []
                assert len(located.targets) == 1

                probe = DomAssertionsProbe()
                cfg = ScenarioRunConfig(
                    route="/hidden.html",
                    url=f"{base_url}/hidden.html",
                    interaction_timeout_ms=750,
                    settle_ms=20,
                )
                events = await run_scenario(
                    page, located.targets, [probe], config=cfg
                )

                # No scenario_failure — the failure is a UI bug.
                assert all(
                    ev.probe != "scenario" for ev in events
                ), [ev.payload for ev in events if ev.probe == "scenario"]
                # Exactly one ui_element_hidden event from the queue drain.
                ui_kinds = [
                    ev.payload.get("kind")
                    for ev in events
                    if ev.probe == "ui"
                ]
                assert "ui_element_hidden" in ui_kinds, ui_kinds
            finally:
                await browser.close()


async def test_detached_element_after_interaction_handled_gracefully(
    tmp_path: Path,
) -> None:
    """A handler that removes its own button mid-scenario must not
    derail subsequent targets, and must not produce a ``scenario_failure``
    when the after-snapshot finds the element detached.
    """
    from playwright.async_api import async_playwright

    (tmp_path / "detach.html").write_text(_DETACH_HTML, encoding="utf-8")

    async with _serve(tmp_path) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(f"{base_url}/detach.html", wait_until="load")

                affected = AffectedRoute(
                    route="/detach.html",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="vanish"),
                        Selector(strategy="test_id", value="survivor"),
                    ],
                )
                located = await locate(page, affected)
                assert located.failures == []
                assert len(located.targets) == 2

                probe = DomAssertionsProbe()
                cfg = ScenarioRunConfig(
                    route="/detach.html",
                    url=f"{base_url}/detach.html",
                    interaction_timeout_ms=750,
                    settle_ms=20,
                )
                events = await run_scenario(
                    page, located.targets, [probe], config=cfg
                )

                # The survivor still got clicked after the vanish target
                # removed itself.
                survivor_clicks = await page.evaluate("window.__survivorClicks")
                assert survivor_clicks == 1
                # And no scenario_failure was emitted by the after-snapshot
                # finding the element gone.
                assert all(ev.probe != "scenario" for ev in events)
            finally:
                await browser.close()


async def test_real_scenario_failure_still_emitted(tmp_path: Path) -> None:
    """Navigation timeouts are *harness* failures — they must still emit
    ``probe=scenario, payload.type='scenario_failure'``. Per-interaction
    failure rerouting must not swallow them.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context = await browser.new_context()
            page = await context.new_page()

            # Point at a TCP port that won't accept connections so
            # ``page.goto`` raises before any interaction can run.
            # 127.0.0.1:9 is the standard discard port — closed on most
            # boxes including Codespaces.
            cfg = ScenarioRunConfig(
                route="/dead",
                url="http://127.0.0.1:9/never",
                timeout_ms=500,
                settle_ms=10,
            )
            events = await run_scenario(page, [], [], config=cfg)

            scenario_events = [
                ev
                for ev in events
                if ev.probe == "scenario"
                and ev.payload.get("type") == "scenario_failure"
            ]
            assert scenario_events, [ev.payload for ev in events]
            assert scenario_events[0].payload.get("phase") == "navigate"
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# ISSUE #5 regression coverage: a hard runtime error in one interaction's
# handler must not poison subsequent interactions on the same route. The
# scenario runner reloads the page between interactions when
# ``recovery_mode`` says so (default ``on_failure``).
# ---------------------------------------------------------------------------


# Three buttons; the first one *both* mutates the DOM (wiping the other
# two buttons) and throws — mimicking a React handler that crashes during
# render after dispatching a state change. Without recovery, clicks 2/3
# can't be located (DOM is gone). With ``on_failure`` recovery, the
# runner reloads, finds the buttons again, and the counters reach 1.
_POISON_HTML = """<!doctype html>
<html><body>
  <h1>Cascade fixture</h1>
  <button data-testid="b1">B1</button>
  <button data-testid="b2">B2</button>
  <button data-testid="b3">B3</button>
  <script>
    // Counters live in localStorage so they survive page.reload().
    function bump(k) {
      const cur = parseInt(localStorage.getItem(k) || '0', 10);
      localStorage.setItem(k, String(cur + 1));
    }
    document.querySelector('[data-testid="b1"]')
      .addEventListener('click', () => {
        bump('b1');
        document.body.innerHTML = '<div>POISONED</div>';
        throw new Error('boom-in-handler');
      });
    document.querySelector('[data-testid="b2"]')
      .addEventListener('click', () => bump('b2'));
    document.querySelector('[data-testid="b3"]')
      .addEventListener('click', () => bump('b3'));
  </script>
</body></html>
"""


_STORAGE_HTML = """<!doctype html>
<html><body>
  <h1>Storage-then-throw fixture</h1>
  <button data-testid="writer">Writer</button>
  <button data-testid="next">Next</button>
  <script>
    document.querySelector('[data-testid="writer"]')
      .addEventListener('click', () => {
        localStorage.setItem('postcheck_recovery_probe', 'survived');
        throw new Error('boom-after-write');
      });
    document.querySelector('[data-testid="next"]')
      .addEventListener('click', () => {});
  </script>
</body></html>
"""


async def _run_cascade(
    tmp_path: Path,
    recovery_mode: str,
    *,
    extra_probes: list | None = None,
) -> tuple[list[ProbeEvent], dict[str, int]]:
    """Drive the 3-button poison fixture and return events + counter map."""
    from playwright.async_api import async_playwright

    (tmp_path / "poison.html").write_text(_POISON_HTML, encoding="utf-8")

    async with _serve(tmp_path) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(f"{base_url}/poison.html", wait_until="load")

                affected = AffectedRoute(
                    route="/poison.html",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="b1"),
                        Selector(strategy="test_id", value="b2"),
                        Selector(strategy="test_id", value="b3"),
                    ],
                )
                located = await locate(page, affected)
                assert located.failures == []
                assert len(located.targets) == 3

                probes = [RuntimeProbe()] + list(extra_probes or [])
                cfg = ScenarioRunConfig(
                    route="/poison.html",
                    url=f"{base_url}/poison.html",
                    interaction_timeout_ms=1_500,
                    settle_ms=30,
                    recovery_mode=recovery_mode,  # type: ignore[arg-type]
                )
                events = await run_scenario(
                    page, located.targets, probes, config=cfg
                )

                # Read counters from localStorage (survives reloads).
                counts: dict[str, int] = {}
                for k in ("b1", "b2", "b3"):
                    raw = await page.evaluate(
                        f"localStorage.getItem('{k}')"
                    )
                    counts[k] = int(raw or "0")
                return events, counts
            finally:
                await browser.close()


async def test_hard_react_error_does_not_block_subsequent_interactions(
    tmp_path: Path,
) -> None:
    """Default ``recovery_mode='on_failure'`` should reload after the
    poisoned click so b2 and b3 still execute.
    """
    events, counts = await _run_cascade(tmp_path, recovery_mode="on_failure")

    # The runtime probe saw the hard error.
    runtime_errors = [
        ev for ev in events
        if ev.probe == "runtime" and ev.payload.get("kind") == "runtime_error"
    ]
    assert runtime_errors, [ev.payload for ev in events if ev.probe == "runtime"]

    # All three handlers fired exactly once.
    assert counts == {"b1": 1, "b2": 1, "b3": 1}, counts


async def test_recovery_emits_scenario_recovery_event(tmp_path: Path) -> None:
    """The reload between interactions surfaces as a
    ``probe=scenario, payload.type='scenario_recovery'`` event so users
    can see why a route's behaviour changed mid-run.
    """
    events, _counts = await _run_cascade(tmp_path, recovery_mode="on_failure")

    recoveries = [
        ev for ev in events
        if ev.probe == "scenario"
        and ev.payload.get("type") == "scenario_recovery"
    ]
    assert recoveries, [ev.payload for ev in events if ev.probe == "scenario"]
    ev = recoveries[0]
    assert ev.payload.get("reloaded") is True
    assert ev.payload.get("reason") == "previous_interaction_errored"
    # Recovery happens *after* interaction 0, *before* interaction 1.
    assert ev.interaction_index == 0


async def test_recovery_mode_never_preserves_legacy_behavior(
    tmp_path: Path,
) -> None:
    """With ``recovery_mode='never'`` the cascade reproduces: only b1
    fires, b2 and b3 are gone from the DOM so their clicks fail.
    """
    events, counts = await _run_cascade(tmp_path, recovery_mode="never")

    # No reload happened.
    assert not any(
        ev.probe == "scenario"
        and ev.payload.get("type") == "scenario_recovery"
        for ev in events
    )
    # Only b1 actually ran; b2/b3 couldn't be clicked because the DOM
    # was wiped by b1's handler.
    assert counts["b1"] == 1
    assert counts["b2"] == 0
    assert counts["b3"] == 0


async def test_storage_events_flushed_before_recovery_reload(
    tmp_path: Path,
) -> None:
    """The storage probe's in-page buffer is wiped by ``page.reload()``.
    The scenario runner must flush it *before* reloading so the write
    survives into the returned events.
    """
    from playwright.async_api import async_playwright

    (tmp_path / "storage.html").write_text(_STORAGE_HTML, encoding="utf-8")

    async with _serve(tmp_path) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()

                storage_probe = StorageProbe()
                runtime_probe = RuntimeProbe()
                # Attach BEFORE navigation so the init script runs on
                # the first document.
                await storage_probe.attach(page)
                await runtime_probe.attach(page)

                await page.goto(
                    f"{base_url}/storage.html", wait_until="load"
                )

                affected = AffectedRoute(
                    route="/storage.html",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="writer"),
                        Selector(strategy="test_id", value="next"),
                    ],
                )
                located = await locate(page, affected)
                assert located.failures == []
                assert len(located.targets) == 2

                cfg = ScenarioRunConfig(
                    route="/storage.html",
                    url=f"{base_url}/storage.html",
                    interaction_timeout_ms=1_500,
                    settle_ms=50,
                    recovery_mode="on_failure",
                )
                events = await run_scenario(
                    page,
                    located.targets,
                    [storage_probe, runtime_probe],
                    config=cfg,
                )

                # Storage write was captured even though page.reload()
                # wiped the in-page __postcheck_storage_events array.
                storage_writes = [
                    ev for ev in events
                    if ev.probe == "storage"
                    and ev.payload.get("kind") == "storage_write"
                    and ev.payload.get("key") == "postcheck_recovery_probe"
                ]
                assert storage_writes, [
                    ev.payload for ev in events if ev.probe == "storage"
                ]
                # And the recovery reload actually happened.
                assert any(
                    ev.probe == "scenario"
                    and ev.payload.get("type") == "scenario_recovery"
                    and ev.payload.get("reloaded") is True
                    for ev in events
                )
            finally:
                await browser.close()


# ---------------------------------------------------------------------------
# ISSUE B regression coverage: form-fill-and-submit support.
#
# The scenario runner detects submit-button / form targets, fills every
# required input with a deterministic default, and submits. The new
# behaviour is exercised end-to-end: the test fixture serves a real HTTP
# endpoint that processes the POST and inspects the body, so the form
# round-trip is *not* mocked. The NetworkProbe rides along to confirm
# the fetch shows up in the network log.
# ---------------------------------------------------------------------------


_FORM_BASIC_HTML = """<!doctype html>
<html><body>
  <form id="signup" onsubmit="event.preventDefault(); window.__lastSubmit = {
    email: this.email.value,
    nickname: this.nickname.value,
  }; fetch('/api/signup', {method:'POST', body: new FormData(this)});">
    <input data-testid="email" name="email" type="email" required />
    <input data-testid="nickname" name="nickname" type="text" required />
    <input data-testid="optional" name="bio" type="text" />
    <button data-testid="submit" type="submit">Sign up</button>
  </form>
</body></html>
"""


_FORM_EXTERNAL_BUTTON_HTML = """<!doctype html>
<html><body>
  <form id="login" onsubmit="event.preventDefault(); window.__lastSubmit = {
    username: this.username.value,
  }; fetch('/api/login', {method:'POST', body: new FormData(this)});">
    <input data-testid="username" name="username" type="text" required />
  </form>
  <!-- submit button is OUTSIDE the form, linked via form="login" -->
  <button data-testid="external-submit" type="submit" form="login">Log in</button>
</body></html>
"""


_FORM_VALIDATION_HTML = """<!doctype html>
<html><body>
  <form id="strict" onsubmit="event.preventDefault(); fetch('/api/strict', {
    method:'POST', body: new FormData(this)
  });">
    <input data-testid="email" name="email" type="email" required />
    <button data-testid="submit" type="submit">Save</button>
  </form>
</body></html>
"""


def _form_handler_factory(html: str, *, strict: bool = False):
    """Return a request handler that serves ``html`` at /index.html and
    accepts POSTs to the form endpoints. Records every received body on
    the class for the test to inspect. ``strict=True`` rejects any POST
    body with a 400 — used by the validation-failure test.
    """
    from http.server import BaseHTTPRequestHandler

    class _H(BaseHTTPRequestHandler):
        received_posts: list[tuple[str, bytes]] = []

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

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            type(self).received_posts.append((self.path, body))
            if strict:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"validation failed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    # Fresh per-test state — each factory call returns a distinct class.
    _H.received_posts = []
    return _H


@asynccontextmanager
async def _serve_handler(handler_cls) -> AsyncIterator[str]:
    from http.server import HTTPServer

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


async def test_form_with_required_inputs_filled_and_submitted() -> None:
    """End-to-end: the scenario runner fills the email + text required
    inputs (skipping the optional one), submits the form, and the real
    HTTP server records the POST. The network probe sees the request.
    """
    from playwright.async_api import async_playwright

    from postcheck.core.config import NetworkSettings
    from postcheck.probes.network_probe import NetworkProbe

    handler = _form_handler_factory(_FORM_BASIC_HTML)
    async with _serve_handler(handler) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await (await browser.new_context()).new_page()
                await page.goto(f"{base_url}/", wait_until="load")
                affected = AffectedRoute(
                    route="/",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="submit"),
                    ],
                )
                located = await locate(page, affected)
                assert located.failures == [], located.failures

                probe = NetworkProbe(NetworkSettings())
                cfg = ScenarioRunConfig(
                    route="/",
                    url=f"{base_url}/",
                    settle_ms=400,
                )
                events = await run_scenario(
                    page, located.targets, [probe], config=cfg
                )

                # The server received the POST with our defaults.
                assert handler.received_posts, "server saw no POST"
                path, body = handler.received_posts[0]
                assert path == "/api/signup"
                body_text = body.decode("utf-8")
                assert "test@example.com" in body_text
                assert "test" in body_text  # nickname default

                # Page-side capture confirms the inputs received the
                # values *before* submit, not after.
                last = await page.evaluate("window.__lastSubmit")
                assert last == {"email": "test@example.com", "nickname": "test"}

                # The optional ``bio`` field was *not* filled.
                assert "bio=test" not in body_text

                # No POST failure showed up — the network probe is
                # silent for happy-path 200s.
                net_errs = [
                    e for e in events
                    if e.probe == "network"
                    and "/api/signup" in e.payload.get("url", "")
                ]
                assert net_errs == [], net_errs
            finally:
                await browser.close()


async def test_form_submit_button_outside_form_still_works() -> None:
    """``<button form="myform" type="submit">`` outside the <form> element
    still resolves to the form via ``el.form`` and triggers the fill +
    submit flow.
    """
    from playwright.async_api import async_playwright

    handler = _form_handler_factory(_FORM_EXTERNAL_BUTTON_HTML)
    async with _serve_handler(handler) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await (await browser.new_context()).new_page()
                await page.goto(f"{base_url}/", wait_until="load")
                affected = AffectedRoute(
                    route="/",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="external-submit"),
                    ],
                )
                located = await locate(page, affected)
                assert located.failures == [], located.failures

                cfg = ScenarioRunConfig(
                    route="/",
                    url=f"{base_url}/",
                    settle_ms=400,
                )
                await run_scenario(page, located.targets, [], config=cfg)

                assert handler.received_posts, "server saw no POST"
                path, body = handler.received_posts[0]
                assert path == "/api/login"
                assert b'name="username"' in body
                assert b"\r\n\r\ntest\r\n" in body
            finally:
                await browser.close()


async def test_form_with_validation_failure_logs_useful_event() -> None:
    """When the server rejects the form submit with a 400, the network
    probe captures the failure as a same-origin ``network_error`` so a
    downstream bug aggregator can attribute it to the form interaction.
    """
    from playwright.async_api import async_playwright

    from postcheck.core.config import NetworkSettings
    from postcheck.probes.network_probe import NetworkProbe

    handler = _form_handler_factory(_FORM_VALIDATION_HTML, strict=True)
    async with _serve_handler(handler) as base_url:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await (await browser.new_context()).new_page()
                await page.goto(f"{base_url}/", wait_until="load")
                affected = AffectedRoute(
                    route="/",
                    reason="direct",
                    confidence="high",
                    suspected_selectors=[
                        Selector(strategy="test_id", value="submit"),
                    ],
                )
                located = await locate(page, affected)
                assert located.failures == [], located.failures

                probe = NetworkProbe(NetworkSettings())
                cfg = ScenarioRunConfig(
                    route="/",
                    url=f"{base_url}/",
                    settle_ms=400,
                )
                events = await run_scenario(
                    page, located.targets, [probe], config=cfg
                )

                # Server actually received the POST with the defaults.
                assert handler.received_posts, "server saw no POST"
                # Network probe captured the 400.
                net_errs = [
                    e for e in events
                    if e.probe == "network"
                    and e.payload.get("kind") == "network_error"
                    and "/api/strict" in e.payload.get("url", "")
                ]
                assert net_errs, [e.payload for e in events if e.probe == "network"]
                assert net_errs[0].payload["status"] == 400
                # And it's tagged to the submit interaction.
                assert net_errs[0].interaction_index == 0
            finally:
                await browser.close()
