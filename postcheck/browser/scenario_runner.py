"""Drive a single route through a scenario, collecting probe events (v0).

The runner is intentionally dumb in v0:

1. Attach every :class:`ProbeHandler` *before* navigation so first-byte
   events (page errors, request log, storage init) aren't lost.
2. Navigate to ``url`` with ``wait_until=networkidle`` (or the configured
   signal) bounded by ``timeout_ms``.
3. For each :class:`LocatedTarget`, perform one default interaction:
   ``click`` for buttons / links / generic elements, ``fill + Enter`` for
   inputs and textareas. Picking the action is based on the live element's
   ``tagName`` only — no framework knowledge.
4. After each interaction, ``await asyncio.sleep(settle_ms / 1000)`` so
   probe handlers can flush async events that fire on the microtask /
   network-idle queues.
5. Drain every probe via :meth:`ProbeHandler.collect_events` and tag the
   events with the triggering ``interaction_index``.
6. Detach all probes.

**Fails soft.** A navigation failure, a missing locator, an interaction
exception, or a probe lifecycle error never propagates. Each becomes a
:class:`ProbeEvent` with ``probe="scenario"`` and a ``payload`` carrying
``type="scenario_failure"`` plus enough context (phase, error, selector,
interaction index) for the bug aggregator downstream to render an
actionable bug. The scenario continues with the remaining targets.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..core.types import Interaction, ProbeEvent, Selector
from ..probes.shared import (
    reset_interaction_context,
    set_current_route,
    set_interaction_index,
)
from .target_locator import LocatedTarget

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ..probes.shared import ProbeHandler


WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]


@dataclass(slots=True)
class ScenarioRunConfig:
    """Tunables for a single scenario run.

    Defaults match CLAUDE.md: ``networkidle`` wait, 30 s navigation budget
    (mirrors :class:`postcheck.core.config.Settings.timeout_ms`), 5 s
    per-interaction budget, and a 50 ms post-interaction settle that gives
    Playwright enough time to flush ``console`` / ``request`` events to
    Python listeners without being noticeable.
    """

    route: str
    url: str
    wait_until: WaitUntil = "networkidle"
    timeout_ms: int = 30_000
    interaction_timeout_ms: int = 5_000
    settle_ms: int = 50
    fill_value: str = "postcheck"


@dataclass(slots=True)
class _DefaultInteraction:
    kind: Literal["click", "fill"]
    value: str | None = None


def _scenario_event(
    *,
    route: str,
    phase: str,
    error: BaseException,
    interaction_index: int | None = None,
    selector: Selector | None = None,
    extra: dict[str, object] | None = None,
) -> ProbeEvent:
    payload: dict[str, object] = {
        "type": "scenario_failure",
        "phase": phase,
        "error": f"{type(error).__name__}: {error}",
    }
    if selector is not None:
        payload["selector"] = selector.model_dump(mode="json")
    if extra:
        payload.update(extra)
    return ProbeEvent(
        probe="scenario",
        route=route,
        interaction_index=interaction_index,
        payload=payload,
    )


def _stamp_route(events: list[ProbeEvent], route: str) -> list[ProbeEvent]:
    for ev in events:
        if not ev.route:
            ev.route = route
    return events


def _stamp_index(events: list[ProbeEvent], idx: int) -> list[ProbeEvent]:
    for ev in events:
        if ev.interaction_index is None:
            ev.interaction_index = idx
    return events


async def _decide_interaction(
    target: LocatedTarget,
    *,
    timeout_ms: int,
    fill_value: str,
) -> _DefaultInteraction:
    """Inspect the live element to pick click vs fill+submit.

    Decision uses ``tagName`` / ``type`` only — no framework knowledge.
    """
    tag_raw = await target.locator.evaluate(
        "el => el && el.tagName ? el.tagName.toUpperCase() : ''",
        timeout=timeout_ms,
    )
    tag = (tag_raw or "").upper()
    if tag in {"INPUT", "TEXTAREA", "SELECT"}:
        if tag == "INPUT":
            input_type_raw = await target.locator.evaluate(
                "el => (el.type || '').toLowerCase()",
                timeout=timeout_ms,
            )
            input_type = (input_type_raw or "text").lower()
            if input_type in {
                "button",
                "submit",
                "reset",
                "checkbox",
                "radio",
                "image",
            }:
                return _DefaultInteraction(kind="click")
        return _DefaultInteraction(kind="fill", value=fill_value)
    return _DefaultInteraction(kind="click")


async def _perform(
    target: LocatedTarget,
    decision: _DefaultInteraction,
    *,
    timeout_ms: int,
) -> Interaction:
    if decision.kind == "click":
        await target.locator.click(timeout=timeout_ms)
        return Interaction(
            kind="click", selector=target.selector, timeout_ms=timeout_ms
        )
    value = decision.value or ""
    await target.locator.fill(value, timeout=timeout_ms)
    await target.locator.press("Enter", timeout=timeout_ms)
    return Interaction(
        kind="fill",
        selector=target.selector,
        value=value,
        timeout_ms=timeout_ms,
    )


async def _drain(
    handlers: list[ProbeHandler], route: str
) -> list[ProbeEvent]:
    out: list[ProbeEvent] = []
    for h in handlers:
        try:
            # Optional async hook — probes that buffer in the page (e.g.
            # storage_probe via window.__postcheck_storage_events) drain
            # via page.evaluate here before the sync collect.
            flush = getattr(h, "flush", None)
            if flush is not None:
                await flush()
            out.extend(_stamp_route(list(h.collect_events()), route))
        except Exception as exc:  # pragma: no cover
            out.append(
                _scenario_event(
                    route=route, phase=f"collect:{h.name}", error=exc
                )
            )
    return out


async def _detach_all(
    handlers: list[ProbeHandler], route: str
) -> list[ProbeEvent]:
    out: list[ProbeEvent] = []
    for h in handlers:
        try:
            await h.detach()
        except Exception as exc:  # pragma: no cover
            out.append(
                _scenario_event(
                    route=route, phase=f"detach:{h.name}", error=exc
                )
            )
    return out


async def _notify_interaction(
    handlers: list[ProbeHandler],
    *,
    hook: str,
    index: int,
    target: LocatedTarget,
    route: str,
) -> list[ProbeEvent]:
    """Invoke ``before_interaction`` / ``after_interaction`` if present.

    The hooks are optional — only DOM-style probes that need pre/post
    snapshots implement them. Failures become ``scenario_failure`` events
    so a buggy probe never tanks the scenario.
    """
    out: list[ProbeEvent] = []
    for h in handlers:
        callback = getattr(h, hook, None)
        if callback is None:
            continue
        try:
            await callback(index, target)
        except Exception as exc:  # pragma: no cover
            out.append(
                _scenario_event(
                    route=route,
                    phase=f"{hook}:{h.name}",
                    error=exc,
                    interaction_index=index,
                    selector=target.selector,
                )
            )
    return out


async def run_scenario(
    page: Page,
    located_targets: list[LocatedTarget],
    probe_handlers: list[ProbeHandler],
    *,
    config: ScenarioRunConfig,
) -> list[ProbeEvent]:
    """Drive ``page`` through one scenario; return every probe event.

    See module docstring for soft-failure semantics. The returned ordering
    is: navigation events first, then per-interaction events in
    interaction order, then any trailing events drained after the final
    interaction settles.
    """
    events: list[ProbeEvent] = []

    # Publish the active route so probes that capture events outside of an
    # interaction (initial navigation, trailing microtasks) can stamp it.
    set_current_route(config.route)
    set_interaction_index(None)

    # 1. Attach probes before navigation so first-byte events are caught.
    attached: list[ProbeHandler] = []
    for handler in probe_handlers:
        try:
            await handler.attach(page)
            attached.append(handler)
        except Exception as exc:
            events.append(
                _scenario_event(
                    route=config.route,
                    phase=f"attach:{handler.name}",
                    error=exc,
                )
            )

    # 2. Navigate.
    nav_failed = False
    try:
        await page.goto(
            config.url,
            wait_until=config.wait_until,
            timeout=config.timeout_ms,
        )
    except Exception as exc:
        nav_failed = True
        events.append(
            _scenario_event(
                route=config.route,
                phase="navigate",
                error=exc,
                extra={"url": config.url},
            )
        )

    # Drain navigation-phase events before per-interaction work.
    events.extend(await _drain(attached, config.route))

    if nav_failed:
        events.extend(await _detach_all(attached, config.route))
        reset_interaction_context()
        return events

    # 4 + 5. Interactions.
    for index, target in enumerate(located_targets):
        # Publish the index so probe callbacks can correlate at capture time;
        # the runner's downstream stamping leaves any probe-set index alone.
        set_interaction_index(index)
        events.extend(
            await _notify_interaction(
                attached,
                hook="before_interaction",
                index=index,
                target=target,
                route=config.route,
            )
        )
        try:
            decision = await _decide_interaction(
                target,
                timeout_ms=config.interaction_timeout_ms,
                fill_value=config.fill_value,
            )
            await _perform(
                target, decision, timeout_ms=config.interaction_timeout_ms
            )
        except Exception as exc:
            events.append(
                _scenario_event(
                    route=config.route,
                    phase="interact",
                    error=exc,
                    interaction_index=index,
                    selector=target.selector,
                )
            )
            events.extend(
                _stamp_index(await _drain(attached, config.route), index)
            )
            continue

        if config.settle_ms > 0:
            await asyncio.sleep(config.settle_ms / 1000)
        events.extend(
            await _notify_interaction(
                attached,
                hook="after_interaction",
                index=index,
                target=target,
                route=config.route,
            )
        )
        events.extend(
            _stamp_index(await _drain(attached, config.route), index)
        )

    # Clear before the trailing drain so late events get index=None.
    set_interaction_index(None)

    # Final drain for trailing events.
    if config.settle_ms > 0:
        await asyncio.sleep(config.settle_ms / 1000)
    events.extend(await _drain(attached, config.route))

    # 7. Detach.
    events.extend(await _detach_all(attached, config.route))
    reset_interaction_context()
    return events


__all__ = ["ScenarioRunConfig", "WaitUntil", "run_scenario"]
