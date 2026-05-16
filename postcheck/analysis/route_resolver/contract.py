# STABLE CONTRACT — see CLAUDE.md before modifying.
"""``RouteAdapter`` Protocol — the boundary every adapter implements.

Required methods (``detect``, ``list_routes``, ``files_for_route``) cover the
v0 routing surface. Optional methods return ``None`` when an adapter cannot
answer; the planner widens its scope accordingly. Treat this file as a
versioned interface from v0 onwards — adding methods is allowed only as
``Optional`` returning ``None`` by default; renames or signature changes
require a versioned ``RouteAdapterV2`` protocol.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ...core.types import Route, Selector, StateGraph, Symbol, SymbolGraph


@runtime_checkable
class RouteAdapter(Protocol):
    """Adapter contract for resolving routes and dependencies in a project."""

    name: str

    async def detect(self, project_root: Path) -> bool:
        """Return ``True`` if this adapter can describe ``project_root``."""
        ...

    async def list_routes(self, project_root: Path) -> list[Route]:
        """Return every route the application exposes."""
        ...

    async def files_for_route(self, route: Route) -> list[Path]:
        """Return every source file backing ``route`` (relative to project root)."""
        ...

    # ------------------------------------------------------------------
    # Optional capabilities — adapters return ``None`` when unsupported.
    # ------------------------------------------------------------------

    async def dependency_graph(self) -> SymbolGraph | None:
        """Return a directed import / inclusion graph, or ``None``."""
        ...

    async def state_graph(self) -> StateGraph | None:
        """Return a read/write graph for app-level state stores, or ``None``."""
        ...

    async def template_selector_map(self, symbol: Symbol) -> list[Selector] | None:
        """Return DOM selector candidates bound to ``symbol``, or ``None``."""
        ...


__all__ = ["RouteAdapter"]
