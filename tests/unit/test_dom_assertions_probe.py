"""Unit tests for ``DomAssertionsProbe`` using stub locators."""
from __future__ import annotations

from typing import Any

import pytest

from postcheck.browser.target_locator import LocatedTarget
from postcheck.core.types import Selector
from postcheck.probes import shared
from postcheck.probes.ui_probe import DomAssertionsProbe


@pytest.fixture(autouse=True)
def _clean_context():
    shared.reset_interaction_context()
    yield
    shared.reset_interaction_context()


class StubLocator:
    """Returns the next queued snapshot dict from ``evaluate``."""

    def __init__(self) -> None:
        self._queue: list[Any] = []
        self.evaluate_calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: Exception | None = None

    def queue(self, value: Any) -> "StubLocator":
        self._queue.append(value)
        return self

    async def evaluate(self, script: str, **kwargs: Any) -> Any:
        self.evaluate_calls.append((script, kwargs))
        if self.fail_with is not None:
            raise self.fail_with
        if not self._queue:
            return None
        return self._queue.pop(0)


class StubPage:
    pass


def _target(test_id: str = "btn") -> LocatedTarget:
    return LocatedTarget(
        selector=Selector(strategy="test_id", value=test_id),
        locator=StubLocator(),  # type: ignore[arg-type]
    )


SNAP_BASE = {
    "outerHTML": "<button>x</button>",
    "visible": True,
    "covered": False,
    "cover_descriptor": None,
    "body_text": "hello world",
    "body_html_length": 200,
}


def _snap(**overrides: Any) -> dict[str, Any]:
    out = dict(SNAP_BASE)
    out.update(overrides)
    return out


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_collect_events_empty_initially():
    probe = DomAssertionsProbe()
    assert probe.collect_events() == []


async def test_attach_detach_no_events():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    await probe.detach()
    assert probe.collect_events() == []


async def test_before_without_attach_is_noop():
    """If attach was never called the snapshot returns None and we exit cleanly."""
    probe = DomAssertionsProbe()
    target = _target()
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    assert probe.collect_events() == []


# ---------------------------------------------------------------------------
# ui_no_change
# ---------------------------------------------------------------------------


async def test_no_change_emitted_when_dom_unchanged():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap()).queue(_snap())  # type: ignore[attr-defined]
    shared.set_current_route("/")
    shared.set_interaction_index(0)

    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    [event] = probe.collect_events()
    assert event.probe == "ui"
    assert event.route == "/"
    assert event.interaction_index == 0
    assert event.payload["kind"] == "ui_no_change"
    assert event.payload["target_outer_html_unchanged"] is True
    assert event.payload["selector"]["value"] == "btn"


async def test_no_change_not_emitted_when_target_outer_html_changed():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap()).queue(  # type: ignore[attr-defined]
        _snap(outerHTML="<button>y</button>")
    )
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    assert [e for e in probe.collect_events() if e.payload["kind"] == "ui_no_change"] == []


async def test_no_change_not_emitted_when_body_text_changed():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap()).queue(  # type: ignore[attr-defined]
        _snap(body_text="hello world!")
    )
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    events = [e for e in probe.collect_events() if e.payload["kind"] == "ui_no_change"]
    assert events == []


async def test_no_change_not_emitted_when_body_html_length_changed():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap()).queue(  # type: ignore[attr-defined]
        _snap(body_html_length=201)
    )
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    events = [e for e in probe.collect_events() if e.payload["kind"] == "ui_no_change"]
    assert events == []


async def test_no_change_not_emitted_when_target_disappeared():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap()).queue(None)  # type: ignore[attr-defined]
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    assert probe.collect_events() == []


# ---------------------------------------------------------------------------
# ui_overlay_blocks
# ---------------------------------------------------------------------------


async def test_overlay_blocks_before_interaction():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target("save")
    target.locator.queue(_snap(covered=True, cover_descriptor="div#modal"))  # type: ignore[attr-defined]
    shared.set_interaction_index(2)

    await probe.before_interaction(2, target)
    [event] = probe.collect_events()
    assert event.payload["kind"] == "ui_overlay_blocks"
    assert event.payload["phase"] == "before"
    assert event.payload["cover_descriptor"] == "div#modal"
    assert event.interaction_index == 2


async def test_overlay_blocks_late_emits_after_phase():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap()).queue(  # type: ignore[attr-defined]
        _snap(covered=True, cover_descriptor="div.modal-shade")
    )
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    overlays = [e for e in probe.collect_events() if e.payload["kind"] == "ui_overlay_blocks"]
    assert len(overlays) == 1
    assert overlays[0].payload["phase"] == "after"
    assert overlays[0].payload["cover_descriptor"] == "div.modal-shade"


async def test_overlay_already_present_not_re_emitted_in_after():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(  # type: ignore[attr-defined]
        _snap(covered=True, cover_descriptor="div#modal")
    ).queue(_snap(covered=True, cover_descriptor="div#modal"))
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    overlays = [e for e in probe.collect_events() if e.payload["kind"] == "ui_overlay_blocks"]
    assert len(overlays) == 1
    assert overlays[0].payload["phase"] == "before"


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


async def test_evaluate_failure_swallowed():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.fail_with = RuntimeError("detached")  # type: ignore[attr-defined]
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    assert probe.collect_events() == []


async def test_after_without_before_does_nothing():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap())  # type: ignore[attr-defined]
    await probe.after_interaction(0, target)
    assert probe.collect_events() == []


async def test_collect_events_drains_buffer():
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap()).queue(_snap())  # type: ignore[attr-defined]
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    assert len(probe.collect_events()) == 1
    assert probe.collect_events() == []


async def test_correlation_uses_providers_at_event_time():
    probe = DomAssertionsProbe(
        interaction_provider=lambda: 9,
        route_provider=lambda: "/dashboard",
    )
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap(covered=True, cover_descriptor="span"))  # type: ignore[attr-defined]
    await probe.before_interaction(0, target)
    [event] = probe.collect_events()
    assert event.route == "/dashboard"
    assert event.interaction_index == 9


async def test_snapshot_timeout_passed_to_locator_evaluate():
    probe = DomAssertionsProbe(snapshot_timeout_ms=750)
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap())  # type: ignore[attr-defined]
    await probe.before_interaction(0, target)
    _, kwargs = target.locator.evaluate_calls[0]  # type: ignore[attr-defined]
    assert kwargs.get("timeout") == 750
