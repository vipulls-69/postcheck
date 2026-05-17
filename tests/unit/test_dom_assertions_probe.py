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


def _target(
    test_id: str = "btn", *, expected_visible_effect: bool | None = None
) -> LocatedTarget:
    return LocatedTarget(
        selector=Selector(
            strategy="test_id",
            value=test_id,
            expected_visible_effect=expected_visible_effect,
        ),
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


async def test_no_change_emitted_even_when_target_outer_html_changed():
    """Target ``outerHTML`` is intentionally *not* part of the route-window
    diff (ISSUE A fix). Many real handlers update a sibling element (e.g.
    ``setMsg(...)`` writing to a ``<p data-testid="*-msg">``); the clicked
    button's HTML stays identical and the route window's three metrics
    are what decide whether the interaction had a visible effect. A
    target-only HTML change with an unchanged route window must therefore
    still emit ``ui_no_change`` (at heuristic confidence, since the stub
    selector has no ``expected_visible_effect`` hint).
    """
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target()
    target.locator.queue(_snap()).queue(  # type: ignore[attr-defined]
        _snap(outerHTML="<button>y</button>")
    )
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    events = [e for e in probe.collect_events() if e.payload["kind"] == "ui_no_change"]
    assert len(events) == 1
    assert events[0].payload["confidence"] == "heuristic"
    assert events[0].payload["target_outer_html_unchanged"] is False


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
# ISSUE A — route-window snapshot + expected_visible_effect grading
# ---------------------------------------------------------------------------


async def test_setMsg_update_does_not_trigger_no_change():
    """A handler that updates a sibling ``<p>`` (the ``setMsg`` pattern)
    moves the route window's text — even though the clicked button's
    own ``outerHTML`` is unchanged. No ``ui_no_change`` should fire.
    """
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target(expected_visible_effect=True)
    # ``outerHTML`` stays identical (the button didn't change); the
    # route-window text length changes because a sibling <p> received
    # the new message.
    target.locator.queue(_snap(text_length=42, child_count=10, text_hash=1)).queue(  # type: ignore[attr-defined]
        _snap(text_length=58, child_count=10, text_hash=2)
    )
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    no_change = [
        e for e in probe.collect_events()
        if e.payload["kind"] == "ui_no_change"
    ]
    assert no_change == []


async def test_truly_noop_handler_emits_ui_no_change():
    """A handler that is literally ``() => {}`` leaves the route window
    byte-identical; ``ui_no_change`` must fire. When the adapter is
    confident (``expected_visible_effect=True``) the finding ships at
    ``deterministic`` confidence.
    """
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target(expected_visible_effect=True)
    snap = _snap(text_length=42, child_count=10, text_hash=1)
    target.locator.queue(snap).queue(snap)  # type: ignore[attr-defined]
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    [event] = [
        e for e in probe.collect_events()
        if e.payload["kind"] == "ui_no_change"
    ]
    assert event.payload["confidence"] == "deterministic"
    assert event.payload["expected_visible_effect"] is True


async def test_no_state_change_intent_emits_heuristic_confidence():
    """When the adapter could not predict the handler's intent
    (``expected_visible_effect=None``, e.g. handler body was a bare
    ``console.log`` we couldn't classify), ``ui_no_change`` still
    surfaces — but at ``heuristic`` confidence so consumers can rank it
    below cases where we *know* a visible effect was expected.
    """
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target(expected_visible_effect=None)
    snap = _snap(text_length=42, child_count=10, text_hash=1)
    target.locator.queue(snap).queue(snap)  # type: ignore[attr-defined]
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    [event] = [
        e for e in probe.collect_events()
        if e.payload["kind"] == "ui_no_change"
    ]
    assert event.payload["confidence"] == "heuristic"
    assert event.payload.get("expected_visible_effect") is None


async def test_expected_visible_effect_false_suppresses_no_change():
    """When the adapter is confident the handler has no visible effect
    (``expected_visible_effect=False`` — handler body was a bare
    analytics / fire-and-forget fetch), the probe must suppress
    ``ui_no_change`` entirely. Surfacing it would be pure noise.
    """
    probe = DomAssertionsProbe()
    await probe.attach(StubPage())  # type: ignore[arg-type]
    target = _target(expected_visible_effect=False)
    snap = _snap(text_length=42, child_count=10, text_hash=1)
    target.locator.queue(snap).queue(snap)  # type: ignore[attr-defined]
    await probe.before_interaction(0, target)
    await probe.after_interaction(0, target)
    no_change = [
        e for e in probe.collect_events()
        if e.payload["kind"] == "ui_no_change"
    ]
    assert no_change == []


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
