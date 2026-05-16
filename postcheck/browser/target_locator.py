"""Translate ``AffectedRoute.suspected_selectors`` to live Playwright locators.

For each selector on the affected route we ask Playwright to resolve it; the
first selector that matches at least one element wins. Selectors that fail
to match are recorded but do not abort the lookup.

Two failure modes are surfaced as :class:`LocationFailure` rather than as
silent skips, because a missing element is itself a probe-able regression
("we changed handler X but no element on this route is bound to it"):

* ``no_selectors_provided`` — the adapter offered no selector candidates at
  all (e.g. plain HTML changes that don't carry ``data-testid``).
* ``no_selector_matched`` — every provided candidate returned zero matches.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..core.types import AffectedRoute, LocationFailure, Selector, Symbol

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page


@dataclass(slots=True)
class LocatedTarget:
    """A successfully resolved selector + its live Playwright locator."""

    selector: Selector
    locator: Locator
    symbol: Symbol | None = None


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


async def locate(page: Page, affected_route: AffectedRoute) -> LocateResult:
    """Resolve every suspected selector on ``affected_route``.

    Returns a :class:`LocateResult` carrying:

    * ``targets`` — every selector that matched at least one DOM element
      (first match per selector wins via ``Locator.first``).
    * ``failures`` — at most one :class:`LocationFailure` describing why no
      target could be produced.

    Selectors are tried in order. Per the v0 contract, the symbol-to-
    selector association is route-level (not per-symbol), so each
    :class:`LocatedTarget` carries the route's first changed symbol when
    available — purely as provenance for the bug reporter.
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

    matched: list[LocatedTarget] = []
    attempted: list[Selector] = []
    for selector in selectors:
        attempted.append(selector)
        locator = _resolve(page, selector)
        try:
            count = await locator.count()
        except Exception:  # pragma: no cover - bad selector strings
            continue
        if count > 0:
            matched.append(
                LocatedTarget(
                    selector=selector,
                    locator=locator.first,
                    symbol=primary_symbol,
                )
            )

    if matched:
        result.targets = matched
        return result

    result.failures.append(
        LocationFailure(
            route=affected_route.route,
            reason="no_selector_matched",
            changed_symbols=list(affected_route.changed_symbols),
            attempted=attempted,
            detail=(
                f"None of {len(attempted)} selector candidate(s) matched a "
                "DOM element on this route. The change may have removed "
                "the element, renamed its identifier, or moved its mount."
            ),
        )
    )
    return result


__all__ = ["LocateResult", "LocatedTarget", "locate"]
