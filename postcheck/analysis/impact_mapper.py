"""Map symbol changes to affected routes (v0: direct only).

For each ``SymbolChange``, find every ``Route`` that lists the change's file
in ``files_for_route`` and emit one ``AffectedRoute`` per route — grouping
all changed symbols touching that route into a single entry. ``reason`` is
always ``"direct"`` and ``confidence`` always ``"high"`` in v0; the v1 union
ships in the schema today (see ``core/types.AffectedRoute``) so v1's richer
emitter does not require a downstream refactor.

When the adapter implements ``template_selector_map``, suspected selectors
are collected for each affected component-kind symbol; otherwise the list is
empty (an accepted v0 limitation).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..core.types import AffectedRoute, Selector, SymbolChange

if TYPE_CHECKING:
    from .route_resolver.contract import RouteAdapter


def _normalize(path: Path, project_root: Path) -> Path:
    if path.is_absolute():
        try:
            return path.resolve().relative_to(project_root.resolve())
        except ValueError:
            return path
    return path


async def map_impact(
    symbol_changes: list[SymbolChange],
    adapter: RouteAdapter,
    project_root: Path,
) -> list[AffectedRoute]:
    """Return the set of routes impacted by ``symbol_changes``.

    Empty input returns an empty list. Symbol changes whose file is not
    included by any route's ``files_for_route`` produce no output.
    """
    if not symbol_changes:
        return []

    routes = await adapter.list_routes(project_root)

    # Build {file -> [route urls]} index.
    file_to_routes: dict[Path, list[str]] = {}
    url_to_route_name: dict[str, str | None] = {}
    for route in routes:
        url_to_route_name[route.url_path] = route.name
        for f in await adapter.files_for_route(route):
            file_to_routes.setdefault(_normalize(f, project_root), []).append(
                route.url_path
            )

    # Group SymbolChanges by url_path, preserving input order.
    grouped: dict[str, list[SymbolChange]] = {}
    for change in symbol_changes:
        key = _normalize(change.file, project_root)
        for url in file_to_routes.get(key, []):
            grouped.setdefault(url, []).append(change)

    affected: list[AffectedRoute] = []
    for url, changes in grouped.items():
        selectors: list[Selector] = []
        seen_selectors: set[tuple[str, str]] = set()
        for sc in changes:
            try:
                from_adapter = await adapter.template_selector_map(sc.symbol)
            except NotImplementedError:
                from_adapter = None
            for sel in from_adapter or []:
                key = (sel.strategy, sel.value)
                if key in seen_selectors:
                    continue
                seen_selectors.add(key)
                selectors.append(sel)
        affected.append(
            AffectedRoute(
                route=url,
                reason="direct",
                confidence="high",
                changed_symbols=[sc.symbol for sc in changes],
                suspected_selectors=selectors,
            )
        )
    return affected


__all__ = ["map_impact"]
