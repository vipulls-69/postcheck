"""Unit tests for ``postcheck.probes.runtime_probe``.

Uses a tiny ``StubPage`` mimicking Playwright's ``page.on`` /
``remove_listener`` and stub event objects mimicking ``Error`` and
``ConsoleMessage``. Real-browser coverage is in
``tests/integration/test_probes/test_runtime_probe.py`` (gated, see
``tests/README.md``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from postcheck.probes import shared
from postcheck.probes.runtime_probe import RuntimeProbe


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class StubPage:
    listeners: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)
    removed: list[tuple[str, Callable[..., Any]]] = field(default_factory=list)

    def on(self, event: str, cb: Callable[..., Any]) -> None:
        self.listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event: str, cb: Callable[..., Any]) -> None:
        self.removed.append((event, cb))
        if event in self.listeners and cb in self.listeners[event]:
            self.listeners[event].remove(cb)

    def fire(self, event: str, payload: Any) -> None:
        for cb in list(self.listeners.get(event, [])):
            cb(payload)


@dataclass
class StubError:
    message: str = ""
    name: str | None = None
    stack: str | None = None


@dataclass
class StubConsole:
    type: str = "log"
    text: str = ""
    location: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_context():
    """Each test starts with a clean module-level interaction context."""
    shared.reset_interaction_context()
    yield
    shared.reset_interaction_context()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_attach_subscribes_to_three_events():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)
    assert set(page.listeners) == {"pageerror", "console", "crash"}
    for evs in page.listeners.values():
        assert len(evs) == 1


async def test_attach_is_idempotent():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)
    await probe.attach(page)
    assert all(len(v) == 1 for v in page.listeners.values())


async def test_detach_removes_every_listener():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)
    await probe.detach()
    assert {ev for ev, _ in page.removed} == {"pageerror", "console", "crash"}
    assert all(v == [] for v in page.listeners.values())


async def test_detach_without_attach_is_noop():
    probe = RuntimeProbe()
    await probe.detach()  # must not raise


# ---------------------------------------------------------------------------
# pageerror
# ---------------------------------------------------------------------------


async def test_pageerror_emits_runtime_error_with_stack():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)

    page.fire(
        "pageerror",
        StubError(
            message="boom",
            name="TypeError",
            stack="TypeError: boom\n    at <anonymous>:1:1",
        ),
    )

    [event] = probe.collect_events()
    assert event.probe == "runtime"
    assert event.payload["kind"] == "runtime_error"
    assert event.payload["source"] == "pageerror"
    assert event.payload["message"] == "boom"
    assert event.payload["name"] == "TypeError"
    assert "TypeError: boom" in event.payload["stack"]


async def test_pageerror_with_no_stack_still_captured():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)

    page.fire("pageerror", StubError(message="primitive throw"))
    [event] = probe.collect_events()
    assert event.payload["message"] == "primitive throw"
    assert event.payload["stack"] is None


# ---------------------------------------------------------------------------
# console filtering
# ---------------------------------------------------------------------------


async def test_console_error_emits_runtime_console_error_with_location():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)

    page.fire(
        "console",
        StubConsole(
            type="error",
            text="bad thing",
            location={"url": "http://x/app.js", "lineNumber": 10, "columnNumber": 4},
        ),
    )
    [event] = probe.collect_events()
    # Console errors are deliberately distinct from pageerror's
    # ``runtime_error`` — a logged-and-handled error is meaningfully
    # less severe than an uncaught throw.
    assert event.payload["kind"] == "runtime_console_error"
    assert event.payload["console_type"] == "error"
    assert event.payload["message"] == "bad thing"
    assert event.payload["location"] == {
        "url": "http://x/app.js",
        "line": 10,
        "column": 4,
    }


async def test_console_warning_emits_runtime_console_warning():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)
    page.fire("console", StubConsole(type="warning", text="hmm"))
    [event] = probe.collect_events()
    assert event.payload["kind"] == "runtime_console_warning"


async def test_console_assert_classified_as_runtime_console_error():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)
    page.fire("console", StubConsole(type="assert", text="assertion failed"))
    [event] = probe.collect_events()
    assert event.payload["kind"] == "runtime_console_error"


@pytest.mark.parametrize("ctype", ["log", "info", "debug", "trace"])
async def test_low_severity_console_levels_are_dropped(ctype):
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)
    page.fire("console", StubConsole(type=ctype, text="noise"))
    assert probe.collect_events() == []


# ---------------------------------------------------------------------------
# crash
# ---------------------------------------------------------------------------


async def test_crash_emits_page_crash():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)
    page.fire("crash", page)
    [event] = probe.collect_events()
    assert event.payload["kind"] == "page_crash"
    assert event.payload["source"] == "crash"


# ---------------------------------------------------------------------------
# Correlation with interaction context
# ---------------------------------------------------------------------------


async def test_event_carries_interaction_index_at_capture_time():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)

    shared.set_current_route("/dashboard")
    shared.set_interaction_index(2)
    page.fire("pageerror", StubError(message="during click"))

    # Even if the runner moves on before we collect, the index captured at
    # event-fire time persists.
    shared.set_interaction_index(5)
    [event] = probe.collect_events()
    assert event.interaction_index == 2
    assert event.route == "/dashboard"


async def test_event_outside_any_interaction_has_index_none():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)

    shared.set_current_route("/")
    shared.set_interaction_index(None)
    page.fire("pageerror", StubError(message="on load"))
    [event] = probe.collect_events()
    assert event.interaction_index is None
    assert event.route == "/"


async def test_explicit_providers_override_module_state():
    probe = RuntimeProbe(
        interaction_provider=lambda: 99,
        route_provider=lambda: "/injected",
    )
    page = StubPage()
    await probe.attach(page)

    shared.set_current_route("/wrong")
    shared.set_interaction_index(1)

    page.fire("pageerror", StubError(message="x"))
    [event] = probe.collect_events()
    assert event.interaction_index == 99
    assert event.route == "/injected"


async def test_collect_events_drains_buffer():
    probe = RuntimeProbe()
    page = StubPage()
    await probe.attach(page)
    page.fire("pageerror", StubError(message="a"))
    page.fire("pageerror", StubError(message="b"))
    first = probe.collect_events()
    assert [e.payload["message"] for e in first] == ["a", "b"]
    assert probe.collect_events() == []
