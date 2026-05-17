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

from ..core.types import Interaction, ProbeEvent, RecoveryMode, Selector
from ..probes.shared import (
    InteractionFailure,
    flush_handlers,
    queue_ui_interaction_failure,
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
    # See :class:`postcheck.core.config.ScenarioSettings.recovery_mode`.
    # Default mirrors that field so callers who don't plumb the Settings
    # object get the same isolation behaviour.
    recovery_mode: RecoveryMode = "on_failure"
    # See :class:`postcheck.core.config.ScenarioSettings.detach_grace_ms`.
    # 3 s default lets real Chromium ``requestfailed`` events (user
    # aborts, ``AbortSignal.timeout``, slow 5xx) surface before
    # teardown; set to 0 to disable (legacy v0 behaviour, used by unit
    # tests that assert exact event sequences).
    detach_grace_ms: int = 3_000
    detach_poll_ms: int = 100


@dataclass(slots=True)
class _DefaultInteraction:
    kind: Literal["click", "fill", "submit_form"]
    value: str | None = None


# ---------------------------------------------------------------------------
# Form-fill defaults (ISSUE B)
# ---------------------------------------------------------------------------
#
# When the runner detects a submit-button or <form> target, it fills every
# *required* input in the form with a predictable default before clicking.
# Predictability — not randomness — is the priority: a failing scenario
# should reproduce byte-for-byte across runs so the bug aggregator can
# attribute the failure to the change under test, not to fuzz luck.
# Optional inputs are left blank by design; v0 explicitly does not try
# to satisfy server-side business rules beyond "required field present".
#
# Keys are HTML5 ``<input type="...">`` values (lowercase). ``select``
# means "pick the first non-empty <option>"; ``checkbox`` / ``radio``
# mean "check it". Unrecognised types fall through to ``_DEFAULT_TEXT``.

_DEFAULT_TEXT = "test"
FORM_FILL_DEFAULTS: dict[str, str] = {
    "email": "test@example.com",
    "number": "1",
    "tel": "1234567890",
    "url": "https://example.com",
    "search": _DEFAULT_TEXT,
    "password": _DEFAULT_TEXT,
    "text": _DEFAULT_TEXT,
    "textarea": _DEFAULT_TEXT,
    # ``date`` / ``datetime-local`` / ``time`` / ``month`` / ``week``
    # are computed at fill time from ``datetime.now(timezone.utc)`` so
    # tests don't depend on a frozen-in-source date drifting stale.
}


def _today_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


# JS that runs against a target locator and reports the form-fill plan.
# Returns ``null`` when the target is not in a form context. Otherwise
# returns a dict describing the form and the required inputs that need
# filling. The Python side does the actual ``locator.fill``/``check``
# calls so Playwright's auto-wait + actionability checks apply.
_FORM_INSPECT_JS = r"""
(el) => {
  if (!el) return null;
  // Identify the form. Three shapes:
  //   1. <form> element directly
  //   2. <button type="submit"> / <input type="submit"> inside a <form>
  //   3. <button form="myform" type="submit"> outside any <form>
  let form = null;
  let submitDescriptor = null;
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'form') {
    form = el;
    submitDescriptor = {kind: 'requestSubmit'};
  } else {
    const type = (el.type || '').toLowerCase();
    const isSubmit = (tag === 'button' || tag === 'input') && (type === 'submit' || (tag === 'button' && type === ''));
    if (!isSubmit) return null;
    // ``el.form`` resolves both nested-in-form and ``form="id"`` cases.
    form = el.form || el.closest('form');
    if (!form) return null;
    submitDescriptor = {kind: 'click'};
  }
  const fields = [];
  for (const node of form.querySelectorAll('input, select, textarea')) {
    if (!node.required) continue;
    if (node.disabled) continue;
    const t = (node.type || '').toLowerCase();
    let strategy = 'fill';
    let plan = null;
    if (node.tagName.toLowerCase() === 'select') {
      strategy = 'select';
    } else if (t === 'checkbox' || t === 'radio') {
      strategy = 'check';
    } else if (t === 'file') {
      // v0 does not fabricate uploads. Skip.
      continue;
    }
    fields.push({
      tag: node.tagName.toLowerCase(),
      type: t,
      name: node.name || null,
      id: node.id || null,
      testid: node.getAttribute('data-testid') || null,
      strategy: strategy,
    });
  }
  return {form_id: form.id || null, submit: submitDescriptor, fields: fields};
}
"""


def _fill_value_for(input_type: str, tag: str) -> str:
    if tag == "textarea":
        return FORM_FILL_DEFAULTS["textarea"]
    if input_type in ("date", "datetime-local"):
        v = _today_iso()
        return v + "T00:00" if input_type == "datetime-local" else v
    if input_type == "month":
        return _today_iso()[:7]
    if input_type == "week":
        # ISO week of today; cheap and deterministic.
        from datetime import datetime, timezone

        y, w, _ = datetime.now(timezone.utc).isocalendar()
        return f"{y}-W{w:02d}"
    if input_type == "time":
        return "12:00"
    if input_type == "color":
        return "#000000"
    return FORM_FILL_DEFAULTS.get(input_type, _DEFAULT_TEXT)


async def _fill_form(page: Page, plan: dict, timeout_ms: int) -> None:
    """Fill every required field in the form per the inspection plan."""
    for field in plan.get("fields", []):
        # Build a locator scoped to the form. Prefer data-testid, fall
        # back to name, fall back to id. If none of the three is set we
        # cannot uniquely target the field; skip — the form submission
        # will likely fail validation, which is itself the bug signal.
        form_id = plan.get("form_id")
        scope = page.locator(f"form#{form_id}") if form_id else page.locator("form")
        if field["testid"]:
            loc = scope.locator(f'[data-testid="{field["testid"]}"]')
        elif field["name"]:
            loc = scope.locator(f'[name="{field["name"]}"]')
        elif field["id"]:
            loc = page.locator(f'#{field["id"]}')
        else:
            continue
        strategy = field["strategy"]
        if strategy == "check":
            await loc.check(timeout=timeout_ms)
        elif strategy == "select":
            # Pick the first <option> with a non-empty ``value``.
            value = await loc.evaluate(
                """sel => {
                    for (const o of sel.options) {
                        if (o.value && !o.disabled) return o.value;
                    }
                    return null;
                }"""
            )
            if value is not None:
                await loc.select_option(value=value, timeout=timeout_ms)
        else:
            await loc.fill(
                _fill_value_for(field["type"], field["tag"]),
                timeout=timeout_ms,
            )


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


def _scenario_recovery_event(
    *,
    route: str,
    interaction_index: int,
    reason: str,
    reloaded: bool,
) -> ProbeEvent:
    """Informational event marking a between-interaction page reset.

    Emitted with ``payload['type'] = 'scenario_recovery'`` so the bug
    aggregator — which only special-cases ``scenario_failure`` — ignores
    it. ``reason`` is one of ``always`` (recovery_mode=always),
    ``previous_interaction_errored`` (on_failure path triggered), or
    ``page_closed_unrecoverable`` (page died, no reload attempted).
    """
    return ProbeEvent(
        probe="scenario",
        route=route,
        interaction_index=interaction_index,
        payload={
            "type": "scenario_recovery",
            "reason": reason,
            "reloaded": reloaded,
        },
    )


# Runtime probe kinds that indicate the page is in a broken state. Used
# by the recovery decision in ``run_scenario`` when ``recovery_mode ==
# 'on_failure'`` — a runtime error in the just-drained events triggers a
# reload before the next interaction, even if the click itself succeeded.
_HARD_RUNTIME_KINDS: frozenset[str] = frozenset({"runtime_error", "page_crash"})


def _runtime_hard_error_in(events: list[ProbeEvent]) -> bool:
    """True if any event signals a render-breaking JS error."""
    for ev in events:
        if ev.probe != "runtime":
            continue
        kind = ev.payload.get("kind")
        if isinstance(kind, str) and kind in _HARD_RUNTIME_KINDS:
            return True
    return False


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
    """Inspect the live element to pick click vs fill+submit vs form submit.

    Decision uses ``tagName`` / ``type`` / form-membership only — no
    framework knowledge.
    """
    tag_raw = await target.locator.evaluate(
        "el => el && el.tagName ? el.tagName.toUpperCase() : ''",
        timeout=timeout_ms,
    )
    tag = (tag_raw or "").upper()
    if tag == "FORM":
        return _DefaultInteraction(kind="submit_form")
    if tag in {"INPUT", "TEXTAREA", "SELECT"}:
        if tag == "INPUT":
            input_type_raw = await target.locator.evaluate(
                "el => (el.type || '').toLowerCase()",
                timeout=timeout_ms,
            )
            input_type = (input_type_raw or "text").lower()
            if input_type in {"submit", "image"}:
                return _DefaultInteraction(kind="submit_form")
            if input_type in {
                "button",
                "reset",
                "checkbox",
                "radio",
            }:
                return _DefaultInteraction(kind="click")
        return _DefaultInteraction(kind="fill", value=fill_value)
    if tag == "BUTTON":
        # Either ``type="submit"`` or default-submit-when-in-form. Let
        # the form inspector decide; if it returns null we fall back to
        # plain click.
        is_submit = await target.locator.evaluate(
            r"""el => {
                const t = (el.type || '').toLowerCase();
                if (t === 'submit') return true;
                if (t === '' && (el.form || el.closest('form'))) return true;
                return false;
            }""",
            timeout=timeout_ms,
        )
        # Strict ``is True`` (not just truthy) — unit-test stubs return
        # placeholder strings here and must fall through to click.
        if is_submit is True:
            return _DefaultInteraction(kind="submit_form")
    return _DefaultInteraction(kind="click")


async def _perform(
    target: LocatedTarget,
    decision: _DefaultInteraction,
    *,
    timeout_ms: int,
    page: Page | None = None,
) -> Interaction:
    if decision.kind == "click":
        await target.locator.click(timeout=timeout_ms)
        return Interaction(
            kind="click", selector=target.selector, timeout_ms=timeout_ms
        )
    if decision.kind == "submit_form":
        # Inspect, fill, then submit. If the inspector returns a
        # non-dict (target wasn't actually in a form after all, or this
        # is a stub locator from a unit test) fall back to a plain click
        # so we don't hang the scenario.
        plan = await target.locator.evaluate(
            _FORM_INSPECT_JS, timeout=timeout_ms
        )
        if not isinstance(plan, dict) or page is None:
            await target.locator.click(timeout=timeout_ms)
            return Interaction(
                kind="click", selector=target.selector, timeout_ms=timeout_ms
            )
        await _fill_form(page, plan, timeout_ms)
        submit_kind = plan.get("submit", {}).get("kind", "click")
        if submit_kind == "requestSubmit":
            await target.locator.evaluate("f => f.requestSubmit()")
        else:
            await target.locator.click(timeout=timeout_ms)
        return Interaction(
            kind="click",
            selector=target.selector,
            timeout_ms=timeout_ms,
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


async def _classify_interaction_failure(
    target: LocatedTarget, exc: BaseException
) -> str:
    """Map a per-interaction exception to a ``UiEventKind`` string.

    Tries cheap message-pattern matching first, then introspects the live
    locator (``count`` / ``is_visible``) for the common ``Timeout 5000ms``
    case where the message alone doesn't say why. Returns one of:
    ``ui_overlay_blocks``, ``ui_element_detached``, ``ui_element_hidden``,
    ``ui_locator_timeout``.
    """
    msg = str(exc).lower()
    if "intercept" in msg and "pointer" in msg:
        return "ui_overlay_blocks"
    if "not attached" in msg or "is detached" in msg or "detached from" in msg:
        return "ui_element_detached"
    if (
        "not visible" in msg
        or "is hidden" in msg
        or "display: none" in msg
        or "visibility: hidden" in msg
    ):
        return "ui_element_hidden"
    # Cheap introspection — these calls don't wait by default.
    try:
        if await target.locator.count() == 0:
            return "ui_element_detached"
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        visible = await target.locator.is_visible()
        if visible is False:
            return "ui_element_hidden"
    except Exception:  # pragma: no cover - defensive
        pass
    return "ui_locator_timeout"


async def _drain(
    handlers: list[ProbeHandler], route: str
) -> list[ProbeEvent]:
    out: list[ProbeEvent] = []
    # Drain in-page buffers first (storage_probe etc.); failures here
    # become scenario_failure via the per-handler collect_events catch
    # below — see ``flush_handlers`` docstring for why this ordering is
    # load-bearing for between-interaction reloads.
    await flush_handlers(handlers)
    for h in handlers:
        try:
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
        # Probes may emit synthetic events during ``detach`` (e.g. the
        # network probe synthesises ``network_aborted`` for requests still
        # in flight when the page is torn down). Drain after detach so
        # those don't get dropped on the floor.
        try:
            trailing = h.collect_events()
        except Exception as exc:  # pragma: no cover
            out.append(
                _scenario_event(
                    route=route,
                    phase=f"detach_drain:{h.name}",
                    error=exc,
                )
            )
            continue
        for ev in trailing:
            if not ev.route:
                ev = ev.model_copy(update={"route": route})
            out.append(ev)
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
    last_index = len(located_targets) - 1
    for index, target in enumerate(located_targets):
        # Publish the index so probe callbacks can correlate at capture time;
        # the runner's downstream stamping leaves any probe-set index alone.
        set_interaction_index(index)
        interaction_errored = False
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
                target,
                decision,
                timeout_ms=config.interaction_timeout_ms,
                page=page,
            )
        except Exception as exc:
            interaction_errored = True
            # Per-interaction failures (timeouts, hidden/detached elements,
            # overlay-blocked clicks) are *UI bugs*, not harness failures.
            # Hand them to the UI probe via the shared queue; fall back to
            # ``scenario_failure`` only if no UI probe is attached so the
            # bug isn't silently dropped.
            kind = await _classify_interaction_failure(target, exc)
            ui_attached = any(h.name == "ui" for h in attached)
            if ui_attached:
                queue_ui_interaction_failure(
                    InteractionFailure(
                        index=index,
                        route=config.route,
                        selector=target.selector.model_dump(mode="json"),
                        kind=kind,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                events.append(
                    _scenario_event(
                        route=config.route,
                        phase="interact",
                        error=exc,
                        interaction_index=index,
                        selector=target.selector,
                        extra={"classified_kind": kind},
                    )
                )
            drained = _stamp_index(
                await _drain(attached, config.route), index
            )
            events.extend(drained)
        else:
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
            drained = _stamp_index(
                await _drain(attached, config.route), index
            )
            events.extend(drained)

        # Recovery decision: a hard runtime error in the events we just
        # drained also counts as a failure for ``on_failure`` mode — the
        # click may have returned cleanly while a useEffect threw and
        # poisoned the React tree.
        if not interaction_errored and _runtime_hard_error_in(drained):
            interaction_errored = True

        if index < last_index and config.recovery_mode != "never":
            should_recover = (
                config.recovery_mode == "always"
                or (
                    config.recovery_mode == "on_failure"
                    and interaction_errored
                )
            )
            if should_recover:
                is_closed = getattr(page, "is_closed", None)
                if callable(is_closed) and is_closed():
                    events.append(
                        _scenario_recovery_event(
                            route=config.route,
                            interaction_index=index,
                            reason="page_closed_unrecoverable",
                            reloaded=False,
                        )
                    )
                    break
                # Belt-and-suspenders flush so storage-probe events that
                # landed in the in-page buffer between the post-interaction
                # drain and now survive the reload's wipe. ``_drain`` will
                # surface anything new on the next iteration.
                await flush_handlers(attached)
                events.extend(
                    _stamp_index(
                        await _drain(attached, config.route), index
                    )
                )
                reason = (
                    "always"
                    if config.recovery_mode == "always"
                    else "previous_interaction_errored"
                )
                try:
                    await page.reload(
                        wait_until=config.wait_until,
                        timeout=config.timeout_ms,
                    )
                except Exception as exc:
                    events.append(
                        _scenario_event(
                            route=config.route,
                            phase="recovery_reload",
                            error=exc,
                            interaction_index=index,
                        )
                    )
                    break
                events.append(
                    _scenario_recovery_event(
                        route=config.route,
                        interaction_index=index,
                        reason=reason,
                        reloaded=True,
                    )
                )

    # Clear before the trailing drain so late events get index=None.
    set_interaction_index(None)

    # Final drain for trailing events.
    if config.settle_ms > 0:
        await asyncio.sleep(config.settle_ms / 1000)
    events.extend(await _drain(attached, config.route))

    # 6b. Detach grace window — give in-flight network requests a chance
    # to surface as real ``requestfailed`` / ``response`` events before
    # we detach. Without this, user-controlled aborts (fixtures using
    # ``setTimeout(() => ctrl.abort(), 2000)`` or
    # ``AbortSignal.timeout(N)``) and slow 5xx responses arrive after
    # listener teardown and are dropped on the floor, forcing the
    # network probe's backstop to synthesise lower-confidence
    # ``network_unresolved_at_detach`` events for what were in fact
    # real, classifiable failures.
    if config.detach_grace_ms > 0:
        await _wait_for_probe_idle(
            attached,
            grace_ms=config.detach_grace_ms,
            poll_ms=config.detach_poll_ms,
        )
        # Drain anything that resolved during the wait so those events
        # are still tagged with the route (the post-detach drain in
        # ``_detach_all`` would also pick them up, but doing it here
        # keeps event ordering deterministic w.r.t. the synthetic
        # ``network_unresolved_at_detach`` emissions in ``detach()``).
        events.extend(await _drain(attached, config.route))

    # 7. Detach.
    events.extend(await _detach_all(attached, config.route))
    reset_interaction_context()
    return events


async def _wait_for_probe_idle(
    handlers: list[ProbeHandler], *, grace_ms: int, poll_ms: int
) -> None:
    """Poll probes' optional ``pending_request_count()`` until idle or budget elapsed.

    Probes that don't expose a pending-count method are treated as idle
    (they have no concept of in-flight work). A handler whose count
    raises is treated as idle and skipped, never as a scenario failure.
    """
    deadline = asyncio.get_event_loop().time() + (grace_ms / 1000)
    interval = max(poll_ms / 1000, 0.01)
    while True:
        pending = 0
        for h in handlers:
            counter = getattr(h, "pending_request_count", None)
            if counter is None:
                continue
            try:
                pending += int(counter())
            except Exception:  # pragma: no cover - defensive
                continue
        if pending == 0:
            return
        if asyncio.get_event_loop().time() >= deadline:
            return
        await asyncio.sleep(interval)


__all__ = ["ScenarioRunConfig", "WaitUntil", "run_scenario"]
