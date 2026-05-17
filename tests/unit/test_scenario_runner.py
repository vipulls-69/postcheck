"""Unit tests for ``postcheck.browser.scenario_runner``.

These use a ``StubPage`` / ``StubLocator`` that mimic only the surface the
runner touches. Real-browser coverage lives in
``tests/integration/test_scenario_runner.py``; on this Python 3.14 +
Playwright 1.59 environment the integration test is skipped (see
``tests/README.md``), making this stub suite the load-bearing one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from postcheck.browser.scenario_runner import ScenarioRunConfig, run_scenario
from postcheck.browser.target_locator import LocatedTarget
from postcheck.core.types import ProbeEvent, Selector


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class StubLocator:
    tag: str
    input_type: str = "text"
    on_click_error: BaseException | None = None
    on_fill_error: BaseException | None = None
    actions: list[str] = field(default_factory=list)

    async def evaluate(self, expr: str, timeout: int | None = None) -> str:
        if "tagName" in expr:
            return self.tag
        if "type" in expr:
            return self.input_type
        return ""

    async def click(self, timeout: int | None = None) -> None:
        if self.on_click_error is not None:
            raise self.on_click_error
        self.actions.append("click")

    async def fill(self, value: str, timeout: int | None = None) -> None:
        if self.on_fill_error is not None:
            raise self.on_fill_error
        self.actions.append(f"fill:{value}")

    async def press(self, key: str, timeout: int | None = None) -> None:
        self.actions.append(f"press:{key}")


@dataclass
class StubPage:
    goto_calls: list[tuple[str, str, int]] = field(default_factory=list)
    on_goto_error: BaseException | None = None

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        if self.on_goto_error is not None:
            raise self.on_goto_error


class RecordingProbe:
    """Probe that emits one event per lifecycle call.

    Lets us assert ordering: events appear in the runner's output in the
    same order they were observed.
    """

    name = "runtime"

    def __init__(self) -> None:
        self.attached = False
        self.detached = False
        self._buffer: list[ProbeEvent] = []
        self._page: StubPage | None = None
        self._counter = 0

    async def attach(self, page) -> None:
        self.attached = True
        self._page = page
        self._emit("attached")

    def _emit(self, label: str) -> None:
        self._counter += 1
        self._buffer.append(
            ProbeEvent(
                probe="runtime",
                route="",  # exercise route stamping
                payload={"label": label, "n": self._counter},
            )
        )

    def trigger(self, label: str) -> None:
        """Manually push an event between runner steps."""
        self._emit(label)

    def collect_events(self) -> list[ProbeEvent]:
        out, self._buffer = self._buffer, []
        return out

    async def detach(self) -> None:
        self.detached = True


def _sel(value: str = "btn") -> Selector:
    return Selector(strategy="test_id", value=value)


def _target(loc: StubLocator) -> LocatedTarget:
    return LocatedTarget(selector=_sel(), locator=loc)  # type: ignore[arg-type]


def _cfg(**overrides) -> ScenarioRunConfig:
    # Unit tests use a StubPage that doesn't model ``page.reload``; default
    # to ``recovery_mode="never"`` so legacy assertions about exact event
    # sequences hold. Real-browser recovery is covered by the integration
    # tests in ``tests/integration/test_scenario_runner.py``.
    base = {
        "route": "/r",
        "url": "http://x/r",
        "settle_ms": 0,
        "recovery_mode": "never",
    }
    base.update(overrides)
    return ScenarioRunConfig(**base)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_runs_navigate_click_collect_detach_in_order():
    page = StubPage()
    button = StubLocator(tag="BUTTON")
    target = _target(button)
    probe = RecordingProbe()

    events = await run_scenario(page, [target], [probe], config=_cfg())  # type: ignore[arg-type]

    # 1. Navigated with configured wait + timeout.
    assert page.goto_calls == [("http://x/r", "networkidle", 30_000)]
    # 2. Default action for BUTTON is click.
    assert button.actions == ["click"]
    # 3. Probe lifecycle was honoured.
    assert probe.attached is True
    assert probe.detached is True
    # 4. The "attached" event is present, and route was stamped from "" to "/r".
    labels = [e.payload["label"] for e in events]
    assert "attached" in labels
    for ev in events:
        assert ev.route == "/r"


async def test_input_target_uses_fill_then_enter():
    page = StubPage()
    inp = StubLocator(tag="INPUT", input_type="text")
    probe = RecordingProbe()

    await run_scenario(page, [_target(inp)], [probe], config=_cfg(fill_value="hello"))  # type: ignore[arg-type]

    assert inp.actions == ["fill:hello", "press:Enter"]


@pytest.mark.parametrize("input_type", ["submit", "button", "checkbox", "radio"])
async def test_button_like_input_types_get_clicked_not_filled(input_type):
    page = StubPage()
    inp = StubLocator(tag="INPUT", input_type=input_type)
    await run_scenario(page, [_target(inp)], [], config=_cfg())  # type: ignore[arg-type]
    assert inp.actions == ["click"]


async def test_textarea_target_is_filled():
    page = StubPage()
    ta = StubLocator(tag="TEXTAREA")
    await run_scenario(page, [_target(ta)], [], config=_cfg(fill_value="x"))  # type: ignore[arg-type]
    assert ta.actions == ["fill:x", "press:Enter"]


# ---------------------------------------------------------------------------
# Ordering: events stamped with the right interaction_index
# ---------------------------------------------------------------------------


async def test_events_carry_correct_interaction_index():
    page = StubPage()
    b1 = StubLocator(tag="BUTTON")
    b2 = StubLocator(tag="BUTTON")

    class IndexingProbe(RecordingProbe):
        async def attach(self, page) -> None:  # type: ignore[override]
            await super().attach(page)
            self._page = page

        def collect_events(self) -> list[ProbeEvent]:
            # Emit one fresh event each time collect is called so we can see
            # the runner's per-interaction drain pattern.
            self._emit("drain")
            return super().collect_events()

    probe = IndexingProbe()
    events = await run_scenario(
        page, [_target(b1), _target(b2)], [probe], config=_cfg()  # type: ignore[arg-type]
    )

    drains = [e for e in events if e.payload["label"] == "drain"]
    # Six drains: post-navigate (no index), after b1 (idx=0), after b2
    # (idx=1), trailing drain after the loop (no index), a post-grace
    # drain (no index) that picks up any requests that resolved during
    # the detach grace window, and a final post-detach drain (no index)
    # so probes that synthesise events during ``detach`` (network
    # probe's in-flight-request synthesis) don't get their events
    # dropped on the floor.
    indices = [e.interaction_index for e in drains]
    assert indices == [None, 0, 1, None, None, None]


# ---------------------------------------------------------------------------
# Failure modes — fails soft as scenario_failure events
# ---------------------------------------------------------------------------


async def test_navigation_failure_emits_scenario_failure_and_skips_interactions():
    page = StubPage(on_goto_error=RuntimeError("net down"))
    button = StubLocator(tag="BUTTON")
    probe = RecordingProbe()

    events = await run_scenario(page, [_target(button)], [probe], config=_cfg())  # type: ignore[arg-type]

    failures = [e for e in events if e.probe == "scenario"]
    assert len(failures) == 1
    f = failures[0]
    assert f.payload["type"] == "scenario_failure"
    assert f.payload["phase"] == "navigate"
    assert "RuntimeError: net down" in f.payload["error"]
    assert f.payload["url"] == "http://x/r"
    # Interaction was skipped.
    assert button.actions == []
    # Probe was still detached.
    assert probe.detached is True


async def test_interaction_failure_emits_event_and_continues_with_next_target():
    page = StubPage()
    bad = StubLocator(tag="BUTTON", on_click_error=RuntimeError("intercepted"))
    good = StubLocator(tag="BUTTON")

    events = await run_scenario(
        page, [_target(bad), _target(good)], [], config=_cfg()  # type: ignore[arg-type]
    )

    # Good target still got clicked.
    assert good.actions == ["click"]

    failures = [e for e in events if e.probe == "scenario"]
    assert len(failures) == 1
    f = failures[0]
    assert f.payload["phase"] == "interact"
    assert f.interaction_index == 0
    assert "RuntimeError: intercepted" in f.payload["error"]
    assert f.payload["selector"]["strategy"] == "test_id"


async def test_attach_failure_does_not_block_other_probes():
    page = StubPage()
    button = StubLocator(tag="BUTTON")

    class BadProbe(RecordingProbe):
        name = "network"

        async def attach(self, page) -> None:  # type: ignore[override]
            raise RuntimeError("attach kaboom")

    bad = BadProbe()
    good = RecordingProbe()

    events = await run_scenario(
        page, [_target(button)], [bad, good], config=_cfg()  # type: ignore[arg-type]
    )

    # The good probe still ran end-to-end.
    assert good.attached is True
    assert good.detached is True
    assert button.actions == ["click"]

    # The bad probe never made the attached list, so detach was not called.
    assert bad.detached is False

    # The attach failure surfaces as a scenario_failure event tagged with the
    # offending probe's name.
    failures = [e for e in events if e.probe == "scenario"]
    assert any(
        f.payload["phase"] == "attach:network" for f in failures
    ), failures


async def test_no_targets_still_navigates_and_returns_probe_events():
    page = StubPage()
    probe = RecordingProbe()

    events = await run_scenario(page, [], [probe], config=_cfg())  # type: ignore[arg-type]

    assert page.goto_calls and page.goto_calls[0][0] == "http://x/r"
    # The "attached" event still comes through.
    assert any(e.payload.get("label") == "attached" for e in events)
    assert probe.detached is True


async def test_wait_until_is_passed_through():
    page = StubPage()
    await run_scenario(
        page, [], [], config=_cfg(wait_until="domcontentloaded", timeout_ms=1234)  # type: ignore[arg-type]
    )
    assert page.goto_calls == [("http://x/r", "domcontentloaded", 1234)]


async def test_runner_awaits_flush_before_collect():
    """Probes that expose ``async def flush`` are flushed by the runner."""
    from postcheck.browser.scenario_runner import ScenarioRunConfig, run_scenario
    from postcheck.core.types import ProbeEvent

    flush_calls: list[str] = []

    class FlushingProbe:
        name = "storage"

        def __init__(self) -> None:
            self._buf: list[ProbeEvent] = []

        async def attach(self, page) -> None:  # noqa: ANN001
            return

        async def flush(self) -> None:
            flush_calls.append("flush")
            self._buf.append(
                ProbeEvent(
                    probe="storage",
                    route="",
                    interaction_index=None,
                    payload={"kind": "storage_write", "via": "flush"},
                )
            )

        def collect_events(self) -> list[ProbeEvent]:
            out, self._buf = self._buf, []
            return out

        async def detach(self) -> None:
            return

    class FakePage:
        url = "http://example/"

        async def goto(self, *a, **k):  # noqa: ANN001, ANN003
            return None

        async def wait_for_timeout(self, *a, **k):  # noqa: ANN001, ANN003
            return None

    probe = FlushingProbe()
    cfg = ScenarioRunConfig(route="/", url="http://example/", settle_ms=0)
    events = await run_scenario(FakePage(), [], [probe], config=cfg)
    storage_events = [e for e in events if e.probe == "storage"]
    assert len(storage_events) >= 1
    assert all(e.payload.get("via") == "flush" for e in storage_events)
    # At least: post-navigation drain + final drain.
    assert len(flush_calls) >= 2
