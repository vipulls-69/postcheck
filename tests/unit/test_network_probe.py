"""Unit tests for ``postcheck.probes.network_probe``."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from postcheck.core.config import NetworkSettings
from postcheck.probes import shared
from postcheck.probes.network_probe import NetworkProbe, _classify_failure


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class StubRequest:
    url: str
    method: str = "GET"
    resource_type: str = "fetch"
    failure: Any = None  # ``str | dict | None``


@dataclass
class StubResponse:
    request: StubRequest
    status: int
    url: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            self.url = self.request.url


@dataclass
class StubPage:
    url: str = "http://example.com/app"
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_context():
    shared.reset_interaction_context()
    shared.set_current_route("/")
    yield
    shared.reset_interaction_context()


@pytest.fixture
def probe_and_page() -> tuple[NetworkProbe, StubPage]:
    page = StubPage()
    probe = NetworkProbe()  # default settings — HMR & telemetry ignored
    return probe, page


def _exchange(page: StubPage, req: StubRequest, status: int) -> None:
    page.fire("request", req)
    page.fire("response", StubResponse(req, status=status))


# ---------------------------------------------------------------------------
# Lifecycle / filter basics
# ---------------------------------------------------------------------------


async def test_attach_subscribes_to_three_network_events(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    assert set(page.listeners) == {"request", "response", "requestfailed"}


async def test_detach_removes_listeners(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    await probe.detach()
    assert {ev for ev, _ in page.removed} == {
        "request",
        "response",
        "requestfailed",
    }


async def test_detach_synthesizes_unresolved_for_pending_requests(
    probe_and_page,
):
    """In-flight requests at detach must surface as ``network_unresolved_at_detach``.

    Chromium's ``requestfailed`` event for an in-flight fetch only fires
    after the page is torn down — by which time the probe's listeners are
    gone and the event is dropped. Real-world bug: a fixture using
    ``setTimeout(() => ctrl.abort(), 2000)`` against a 10 s endpoint
    produced zero network events end-to-end before this fix. The probe
    now synthesises one ``network_unresolved_at_detach`` per still-tracked
    request at detach time (heuristic confidence — we don't know *why*
    it didn't resolve), with ``phase="probe_detach"`` so consumers can
    tell the synthetic case apart from a real Chromium-reported abort
    (``network_aborted``, deterministic).
    """
    probe, page = probe_and_page
    await probe.attach(page)
    req = StubRequest(
        url="http://example.com/api/slow", method="GET", resource_type="fetch"
    )
    page.fire("request", req)
    # No ``response`` and no ``requestfailed`` — request stays in flight.
    assert probe.collect_events() == []  # nothing emitted yet

    await probe.detach()

    [event] = probe.collect_events()
    p = event.payload
    assert p["kind"] == "network_unresolved_at_detach"
    assert p["url"] == "http://example.com/api/slow"
    assert p["method"] == "GET"
    assert p["phase"] == "probe_detach"
    assert p["status"] is None
    assert "did not resolve" in (p["error"] or "")


async def test_detach_does_not_synthesize_for_completed_requests(
    probe_and_page,
):
    """Requests that already produced a response/failure must not double-emit."""
    probe, page = probe_and_page
    await probe.attach(page)
    req = StubRequest(url="http://example.com/api/x")
    _exchange(page, req, 500)  # full request → 500 response cycle
    pre_detach = probe.collect_events()
    assert len(pre_detach) == 1 and pre_detach[0].payload["phase"] == "response"

    await probe.detach()
    # No additional synthetic event — the request was already accounted for.
    assert probe.collect_events() == []


async def test_detach_synthesizes_for_each_pending_request(
    probe_and_page,
):
    probe, page = probe_and_page
    await probe.attach(page)
    urls = [
        "http://example.com/api/a",
        "http://example.com/api/b",
        "http://example.com/api/c",
    ]
    # Hold references so id() doesn't collide via memory reuse.
    reqs = [StubRequest(url=u) for u in urls]
    for r in reqs:
        page.fire("request", r)
    await probe.detach()
    events = probe.collect_events()
    assert {e.payload["url"] for e in events} == set(urls)
    assert {e.payload["kind"] for e in events} == {"network_unresolved_at_detach"}
    assert {e.payload["phase"] for e in events} == {"probe_detach"}



async def test_2xx_response_emits_nothing(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    _exchange(page, StubRequest(url="http://example.com/api/ok"), 200)
    assert probe.collect_events() == []


async def test_3xx_response_emits_nothing(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    _exchange(page, StubRequest(url="http://example.com/api/redirect"), 302)
    assert probe.collect_events() == []


# ---------------------------------------------------------------------------
# Layer 1 — resource-type whitelist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rt", ["image", "stylesheet", "font", "media"])
async def test_non_whitelisted_resource_type_is_dropped(probe_and_page, rt):
    probe, page = probe_and_page
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(url="http://example.com/asset", resource_type=rt),
        500,
    )
    assert probe.collect_events() == []


@pytest.mark.parametrize("rt", ["xhr", "fetch", "websocket", "eventsource"])
async def test_whitelisted_resource_types_are_tracked(probe_and_page, rt):
    probe, page = probe_and_page
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(url="http://example.com/api/x", resource_type=rt),
        500,
    )
    [event] = probe.collect_events()
    assert event.payload["kind"] == "network_error"
    assert event.payload["resource_type"] == rt


async def test_document_failures_bypass_resource_type_gate():
    """Documents are always tracked even if dropped from the whitelist."""
    page = StubPage()
    settings = NetworkSettings(resource_types=["xhr"])  # no "document"
    probe = NetworkProbe(settings)
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(
            url="http://example.com/page", resource_type="document"
        ),
        500,
    )
    [event] = probe.collect_events()
    assert event.payload["kind"] == "navigation_failure"


# ---------------------------------------------------------------------------
# Layer 2 — ignore / focus patterns
# ---------------------------------------------------------------------------


async def test_hmr_url_is_ignored(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(url="http://example.com/@vite/client"),
        500,
    )
    assert probe.collect_events() == []


async def test_telemetry_domain_is_ignored(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(url="https://www.google-analytics.com/collect"),
        500,
    )
    assert probe.collect_events() == []


async def test_focus_pattern_boosts_cross_origin_to_high():
    page = StubPage(url="http://app.example.com/")
    settings = NetworkSettings(focus_patterns=[r"api\.example\.com"])
    probe = NetworkProbe(settings)
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(url="https://api.example.com/users"),
        500,
    )
    [event] = probe.collect_events()
    assert event.payload["confidence"] == "high"


# ---------------------------------------------------------------------------
# Layer 3 — origin classification
# ---------------------------------------------------------------------------


async def test_same_origin_failure_gets_high_confidence(probe_and_page):
    probe, page = probe_and_page  # page.url = http://example.com/app
    await probe.attach(page)
    _exchange(page, StubRequest(url="http://example.com/api/users"), 500)
    [event] = probe.collect_events()
    assert event.payload["confidence"] == "high"


async def test_cross_origin_failure_gets_medium_by_default(probe_and_page):
    probe, page = probe_and_page  # page.url = http://example.com/app
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(url="https://otherdomain.test/api/x"),
        500,
    )
    [event] = probe.collect_events()
    assert event.payload["confidence"] == "medium"


async def test_cross_origin_policy_ignore_drops_event():
    page = StubPage(url="http://example.com/app")
    settings = NetworkSettings(treat_cross_origin_as="ignore")
    probe = NetworkProbe(settings)
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(url="https://otherdomain.test/api/x"),
        500,
    )
    assert probe.collect_events() == []


async def test_cross_origin_policy_low():
    page = StubPage(url="http://example.com/app")
    settings = NetworkSettings(treat_cross_origin_as="low")
    probe = NetworkProbe(settings)
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(url="https://otherdomain.test/api/x"),
        500,
    )
    [event] = probe.collect_events()
    assert event.payload["confidence"] == "low"


# ---------------------------------------------------------------------------
# Document → navigation_failure
# ---------------------------------------------------------------------------


async def test_document_4xx_is_navigation_failure(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(
            url="http://example.com/missing", resource_type="document"
        ),
        404,
    )
    [event] = probe.collect_events()
    assert event.payload["kind"] == "navigation_failure"
    assert event.payload["status"] == 404


async def test_non_document_5xx_is_network_error(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    _exchange(
        page,
        StubRequest(url="http://example.com/api/x", resource_type="xhr"),
        503,
    )
    [event] = probe.collect_events()
    assert event.payload["kind"] == "network_error"


# ---------------------------------------------------------------------------
# requestfailed
# ---------------------------------------------------------------------------


async def test_requestfailed_emits_event_with_error_text(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    req = StubRequest(
        url="http://example.com/api/x", failure="net::ERR_FAILED"
    )
    page.fire("request", req)
    page.fire("requestfailed", req)
    [event] = probe.collect_events()
    assert event.payload["kind"] == "network_error"
    assert event.payload["status"] is None
    assert event.payload["error"] == "net::ERR_FAILED"
    assert event.payload["phase"] == "requestfailed"


async def test_requestfailed_on_document_is_navigation_failure(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    req = StubRequest(
        url="http://example.com/page",
        resource_type="document",
        failure="net::ERR_NAME_NOT_RESOLVED",
    )
    page.fire("request", req)
    page.fire("requestfailed", req)
    [event] = probe.collect_events()
    assert event.payload["kind"] == "navigation_failure"
    assert event.payload["error"] == "net::ERR_NAME_NOT_RESOLVED"


async def test_requestfailed_with_dict_failure_extracts_error_text(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    req = StubRequest(
        url="http://example.com/api/x",
        failure={"errorText": "net::ERR_ABORTED"},
    )
    page.fire("request", req)
    page.fire("requestfailed", req)
    [event] = probe.collect_events()
    assert event.payload["error"] == "net::ERR_ABORTED"


async def test_requestfailed_with_no_failure_text_uses_fallback(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    req = StubRequest(url="http://example.com/api/x", failure=None)
    page.fire("request", req)
    page.fire("requestfailed", req)
    [event] = probe.collect_events()
    assert event.payload["error"] == "request failed"


async def test_requestfailed_for_unfiltered_url_is_silent(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    req = StubRequest(url="http://example.com/@vite/client", failure="x")
    page.fire("request", req)
    page.fire("requestfailed", req)
    assert probe.collect_events() == []


# ---------------------------------------------------------------------------
# Classifier — pure function, locked to Chromium's ``net::ERR_*`` vocabulary.
#
# The strings here are not invented: they are the verbatim ``request.failure``
# values Chromium emits, captured by running ``postcheck verify`` against
# the ``probe_stress_react`` fixture with stderr instrumentation. CORS
# specifically arrives as ``net::ERR_FAILED`` — there is no CORS-specific
# string at the Playwright layer in v0, so we accept the fall-through to
# ``network_error``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_text,expected",
    [
        ("net::ERR_ABORTED", "network_aborted"),
        ("net::ERR_TIMED_OUT", "network_timeout"),
        ("net::ERR_CONNECTION_TIMED_OUT", "network_timeout"),
        ("net::ERR_NAME_NOT_RESOLVED", "network_dns_error"),
        ("net::ERR_CONNECTION_REFUSED", "network_connection_error"),
        ("net::ERR_CONNECTION_RESET", "network_connection_error"),
        ("net::ERR_CONNECTION_CLOSED", "network_connection_error"),
        # CORS failures surface only as ``net::ERR_FAILED`` — accepted v0
        # limitation, classifier must fall through to ``network_error``.
        ("net::ERR_FAILED", "network_error"),
        # Unknown / future Chromium errors must not crash; fall through.
        ("net::ERR_SOMETHING_NEW_IN_CHROME_200", "network_error"),
        ("", "network_error"),
    ],
)
def test_classify_failure_maps_chromium_tokens(error_text, expected):
    assert _classify_failure(error_text, is_navigation=False) == expected


def test_classify_failure_navigation_always_returns_navigation_failure():
    """Document-level failures bypass the token map entirely.

    A failed top-level navigation is a different bug category ("page
    didn't load") from a failed in-page fetch and the bug aggregator
    renders it differently — regardless of *why* the navigation failed.
    """
    for token in (
        "net::ERR_NAME_NOT_RESOLVED",
        "net::ERR_ABORTED",
        "net::ERR_TIMED_OUT",
        "net::ERR_FAILED",
        "",
    ):
        assert (
            _classify_failure(token, is_navigation=True)
            == "navigation_failure"
        )


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


async def test_correlates_with_interaction_at_request_time(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    shared.set_interaction_index(2)
    shared.set_current_route("/dashboard")
    req = StubRequest(url="http://example.com/api/save")
    page.fire("request", req)
    # Long delay — runner moves on before the response comes back.
    shared.set_interaction_index(7)
    page.fire("response", StubResponse(req, status=500))
    [event] = probe.collect_events()
    assert event.interaction_index == 2  # captured at request time
    assert event.route == "/dashboard"


async def test_request_id_is_unique_and_propagated(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    for _ in range(3):
        req = StubRequest(url="http://example.com/api/x")
        _exchange(page, req, 500)
    ids = [e.payload["request_id"] for e in probe.collect_events()]
    assert ids == ["req-1", "req-2", "req-3"]


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


async def test_response_for_untracked_request_is_silent(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    # Response without prior tracked request (edge case: probe attached late).
    rogue = StubRequest(url="http://example.com/api/x")
    page.fire("response", StubResponse(rogue, status=500))
    assert probe.collect_events() == []


async def test_emits_url_method_and_phase(probe_and_page):
    probe, page = probe_and_page
    await probe.attach(page)
    req = StubRequest(
        url="http://example.com/api/save", method="POST", resource_type="fetch"
    )
    _exchange(page, req, 500)
    [event] = probe.collect_events()
    p = event.payload
    assert p["url"] == "http://example.com/api/save"
    assert p["method"] == "POST"
    assert p["resource_type"] == "fetch"
    assert p["status"] == 500
    assert p["phase"] == "response"
