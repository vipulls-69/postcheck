"""Translate ``AffectedRoute.suspected_selectors`` to live Playwright locators.

For each selector on the affected route we ask Playwright to resolve it. In
v0 every adapter routinely emits **multiple selectors per changed symbol**
(e.g. the React/Vite adapter emits both a ``test_id`` and the visible
``text`` for the same JSX button). The locator deduplicates these so the
downstream scenario runner exercises each conceptual target *once*:

* If the adapter tagged selectors with ``symbol_name``, all selectors
  sharing a name are treated as **fallbacks for one target** — the
  highest-priority strategy wins (see :data:`SELECTOR_PRIORITY`). If those
  selectors resolve to *different* DOM elements, that is an adapter bug:
  the winner still wins but :attr:`LocatedTarget.ambiguity` is populated
  so it surfaces in the report.
* If the adapter did **not** tag selectors (v0 default), we group by a
  JS-side element fingerprint instead — distinct selectors that resolve
  to the same element collapse to one target.
* Failing selectors are recorded but do not abort the lookup.

Two failure modes are surfaced as :class:`LocationFailure` rather than as
silent skips, because a missing element is itself a probe-able regression
("we changed handler X but no element on this route is bound to it"):

* ``no_selectors_provided`` — the adapter offered no selector candidates
  at all (e.g. plain HTML changes that don't carry ``data-testid``).
* ``no_selector_matched`` — every provided candidate returned zero
  matches.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.types import (
    AffectedRoute,
    LocationFailure,
    Selector,
    SelectorStrategy,
    Symbol,
)

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page


# Selector strategies from most-stable to most-brittle. ``test_id`` is the
# app's explicit opt-in identifier; ``role`` is accessibility-grade and
# survives styling churn; ``label`` is form-field-specific but stable;
# ``text`` breaks when copy changes; ``css``/``xpath`` are last resort.
SELECTOR_PRIORITY: tuple[SelectorStrategy, ...] = (
    "test_id",
    "role",
    "label",
    "text",
    "css",
    "xpath",
)


def _priority(selector: Selector) -> int:
    try:
        return SELECTOR_PRIORITY.index(selector.strategy)
    except ValueError:  # pragma: no cover - SelectorStrategy is a Literal
        return len(SELECTOR_PRIORITY)


# JS evaluated against each resolved element to produce a fingerprint that
# is identical for the same element via different selectors but different
# across distinct elements. We deliberately combine tagName + id + bounding
# box + outerHTML length — collisions across all of those for distinct
# elements are vanishingly unlikely on a real page.
_FINGERPRINT_JS = r"""
(el) => {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return [
    el.tagName || '',
    el.id || '',
    r.left, r.top, r.width, r.height,
    el.outerHTML ? el.outerHTML.length : 0,
  ].join('|');
}
"""


@dataclass(slots=True)
class LocatedTarget:
    """A successfully resolved selector + its live Playwright locator.

    ``resolved_via`` records every strategy that bound to the same
    underlying element (the winner first); ``ambiguity`` is non-None only
    when selectors that *claim* to point at the same symbol resolve to
    different elements — an adapter bug worth surfacing.
    """

    selector: Selector
    locator: Locator
    symbol: Symbol | None = None
    resolved_via: list[SelectorStrategy] = field(default_factory=list)
    ambiguity: dict[str, Any] | None = None


@dataclass(slots=True)
class LocateResult:
    """Return value of :func:`locate` — found targets plus failure info.

    ``failures`` is at most one entry per call; it is a list to keep the
    shape uniform with future v1 work that may emit per-symbol failures.
    """

    targets: list[LocatedTarget] = field(default_factory=list)
    failures: list[LocationFailure] = field(default_factory=list)


def _resolve(page: Page, selector: Selector) -> Locator:
    """Build a Playwright ``Locator`` from a typed :class:`Selector`."""
    value = selector.value
    if selector.strategy == "test_id":
        return page.get_by_test_id(value)
    if selector.strategy == "role":
        # ``role:name`` packs an accessible name into the value (see
        # ``ReactViteAdapter.template_selector_map``); split it back out.
        if ":" in value:
            role, name = value.split(":", 1)
            return page.get_by_role(role.strip(), name=name.strip())  # type: ignore[arg-type]
        return page.get_by_role(value.strip())  # type: ignore[arg-type]
    if selector.strategy == "label":
        return page.get_by_label(value)
    if selector.strategy == "text":
        return page.get_by_text(value)
    if selector.strategy == "xpath":
        prefix = "" if value.startswith("xpath=") else "xpath="
        return page.locator(f"{prefix}{value}")
    # css (default)
    return page.locator(value)


async def _fingerprint(locator: Locator) -> str | None:
    """Return a JS-side element fingerprint or ``None`` if unavailable.

    Stubs without ``evaluate`` (or real failures) return None; the caller
    treats unfingerprinted matches as un-dedupable.
    """
    evaluate = getattr(locator, "evaluate", None)
    if evaluate is None:
        return None
    try:
        result = await evaluate(_FINGERPRINT_JS)
    except Exception:  # pragma: no cover - defensive
        return None
    if isinstance(result, str):
        return result
    return None


async def locate(page: Page, affected_route: AffectedRoute) -> LocateResult:
    """Resolve every suspected selector on ``affected_route`` and deduplicate.

    See module docstring. Returns a :class:`LocateResult` carrying:

    * ``targets`` — one :class:`LocatedTarget` per *distinct* DOM
      element (or per untagged-unfingerprinted selector). Multiple
      selectors pointing at the same element collapse to one target
      whose winning strategy is the highest-priority one.
    * ``failures`` — at most one :class:`LocationFailure` describing why
      no target could be produced.
    """
    result = LocateResult()
    selectors = affected_route.suspected_selectors

    if not selectors:
        result.failures.append(
            LocationFailure(
                route=affected_route.route,
                reason="no_selectors_provided",
                changed_symbols=list(affected_route.changed_symbols),
                attempted=[],
                detail=(
                    "Adapter returned no selector candidates for the changed "
                    "symbols on this route; the scenario runner should "
                    "broaden coverage instead of skipping."
                ),
            )
        )
        return result

    primary_symbol: Symbol | None = (
        affected_route.changed_symbols[0]
        if affected_route.changed_symbols
        else None
    )

    # Phase 1: resolve every selector.
    resolved: list[tuple[Selector, Locator, str | None]] = []
    attempted: list[Selector] = []
    for selector in selectors:
        attempted.append(selector)
        locator = _resolve(page, selector)
        try:
            count = await locator.count()
        except Exception:  # pragma: no cover - bad selector strings
            continue
        if count <= 0:
            continue
        first = locator.first
        fp = await _fingerprint(first)
        resolved.append((selector, first, fp))

    if not resolved:
        result.failures.append(
            LocationFailure(
                route=affected_route.route,
                reason="no_selector_matched",
                changed_symbols=list(affected_route.changed_symbols),
                attempted=attempted,
                detail=(
                    f"None of {len(attempted)} selector candidate(s) matched "
                    "a DOM element on this route. The change may have "
                    "removed the element, renamed its identifier, or moved "
                    "its mount."
                ),
            )
        )
        return result

    # Phase 2: bucket by symbol_name (adapter-declared) then by fingerprint
    # (inferred). Untagged + unfingerprinted matches can't be deduped, so
    # each becomes its own target — preserving v0 adapters' behaviour.
    by_symbol: dict[str, list[tuple[Selector, Locator, str | None]]] = {}
    by_fp: dict[str, list[tuple[Selector, Locator, str | None]]] = {}
    standalone: list[tuple[Selector, Locator, str | None]] = []
    for sel, loc, fp in resolved:
        if sel.symbol_name:
            by_symbol.setdefault(sel.symbol_name, []).append((sel, loc, fp))
        elif fp is not None:
            by_fp.setdefault(fp, []).append((sel, loc, fp))
        else:
            standalone.append((sel, loc, fp))

    targets: list[LocatedTarget] = []

    # Symbol-tagged groups: adapter says "these are the same target".
    # Sanity-check that the live DOM agrees; flag ambiguity if not.
    for symbol_name, members in by_symbol.items():
        members.sort(key=lambda m: _priority(m[0]))
        winner_sel, winner_loc, _ = members[0]
        ambiguity: dict[str, Any] | None = None
        fps = {m[2] for m in members if m[2] is not None}
        if len(fps) > 1:
            ambiguity = {
                "reason": "selectors_for_symbol_resolve_to_different_elements",
                "symbol_name": symbol_name,
                "strategies": [m[0].strategy for m in members],
                "fingerprints": sorted(fps),
            }
        targets.append(
            LocatedTarget(
                selector=winner_sel,
                locator=winner_loc,
                symbol=primary_symbol,
                resolved_via=[m[0].strategy for m in members],
                ambiguity=ambiguity,
            )
        )

    # Fingerprint groups: adapter didn't tag, but multiple selectors
    # resolved to the same element — collapse to the highest-priority
    # strategy.
    for _, members in by_fp.items():
        members.sort(key=lambda m: _priority(m[0]))
        winner_sel, winner_loc, _ = members[0]
        targets.append(
            LocatedTarget(
                selector=winner_sel,
                locator=winner_loc,
                symbol=primary_symbol,
                resolved_via=[m[0].strategy for m in members],
                ambiguity=None,
            )
        )

    # Standalone matches (no symbol, no fingerprint — typically test
    # stubs): one target per selector, preserving v0 behaviour.
    for sel, loc, _ in standalone:
        targets.append(
            LocatedTarget(
                selector=sel,
                locator=loc,
                symbol=primary_symbol,
                resolved_via=[sel.strategy],
                ambiguity=None,
            )
        )

    result.targets = targets
    return result


__all__ = ["SELECTOR_PRIORITY", "LocateResult", "LocatedTarget", "locate"]
