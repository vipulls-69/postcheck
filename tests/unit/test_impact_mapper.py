"""Tests for ``analysis.impact_mapper`` (v0)."""
from __future__ import annotations

from pathlib import Path

import pytest

from postcheck.analysis.impact_mapper import map_impact
from postcheck.analysis.route_resolver.adapters.plain_html import PlainHtmlAdapter
from postcheck.analysis.route_resolver.adapters.react_vite import ReactViteAdapter
from postcheck.core.types import (
    AffectedRoute,
    Route,
    Selector,
    StateGraph,
    Symbol,
    SymbolChange,
    SymbolGraph,
)

PLAIN = Path(__file__).resolve().parents[2] / "examples" / "plain_html_site"
REACT = Path(__file__).resolve().parents[2] / "examples" / "react_vite_app"


def _change(file: str, name: str, kind: str = "function") -> SymbolChange:
    return SymbolChange(
        file=Path(file),
        symbol=Symbol(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            file=Path(file),
            start_line=1,
            end_line=2,
        ),
        kind="modified",
    )


async def test_empty_changes_returns_empty():
    adapter = PlainHtmlAdapter()
    out = await map_impact([], adapter, PLAIN)
    assert out == []


async def test_direct_mapping_for_plain_html_shared_asset():
    adapter = PlainHtmlAdapter()
    # Editing shared.js should impact every page that includes it.
    out = await map_impact([_change("shared.js", "onHomeClick")], adapter, PLAIN)
    routes = {r.route for r in out}
    assert routes == {"/", "/about.html", "/pricing.html"}
    for ar in out:
        assert ar.reason == "direct"
        assert ar.confidence == "high"
        assert ar.changed_symbols[0].name == "onHomeClick"


async def test_file_in_no_route_returns_empty():
    adapter = PlainHtmlAdapter()
    out = await map_impact(
        [_change("README.md", "noop")], adapter, PLAIN
    )
    assert out == []


async def test_react_vite_direct_mapping_with_selector():
    adapter = ReactViteAdapter()
    sc = SymbolChange(
        file=Path("src/routes/Home.tsx"),
        symbol=Symbol(
            name="Home",
            kind="component",
            file=Path("src/routes/Home.tsx"),
            start_line=3,
            end_line=20,
        ),
        kind="modified",
    )
    out = await map_impact([sc], adapter, REACT)
    [ar] = out
    assert ar.route == "/"
    assert ar.reason == "direct"
    assert ar.confidence == "high"
    assert any(
        s.strategy == "test_id" and s.value == "home-action"
        for s in ar.suspected_selectors
    )


async def test_groups_multiple_symbols_per_route():
    adapter = PlainHtmlAdapter()
    out = await map_impact(
        [
            _change("shared.css", "__file__", kind="css_rule"),
            _change("shared.js", "onHomeClick"),
        ],
        adapter,
        PLAIN,
    )
    by_route = {r.route: r for r in out}
    home = by_route["/"]
    names = {s.name for s in home.changed_symbols}
    assert names == {"__file__", "onHomeClick"}


async def test_selectors_are_empty_when_adapter_returns_none():
    """Stub adapter with template_selector_map=None — selectors stay empty."""

    class StubAdapter:
        name = "stub"

        async def detect(self, project_root: Path) -> bool:  # noqa: ARG002
            return True

        async def list_routes(self, project_root: Path) -> list[Route]:  # noqa: ARG002
            return [Route(url_path="/x", files=[Path("a.ts")], name="X")]

        async def files_for_route(self, route: Route) -> list[Path]:
            return list(route.files)

        async def dependency_graph(self) -> SymbolGraph | None:
            return None

        async def state_graph(self) -> StateGraph | None:
            return None

        async def template_selector_map(self, symbol: Symbol) -> list[Selector] | None:  # noqa: ARG002
            return None

    out = await map_impact([_change("a.ts", "f")], StubAdapter(), Path("/"))
    [ar] = out
    assert ar.suspected_selectors == []
    assert ar.route == "/x"


async def test_absolute_paths_normalize(tmp_path):
    """Symbol changes carrying absolute paths still map correctly."""
    adapter = PlainHtmlAdapter()
    abs_path = (PLAIN / "shared.js").resolve()
    sc = SymbolChange(
        file=abs_path,
        symbol=Symbol(
            name="onHomeClick",
            kind="function",
            file=abs_path,
            start_line=1,
            end_line=2,
        ),
        kind="modified",
    )
    out = await map_impact([sc], adapter, PLAIN)
    assert {r.route for r in out} == {"/", "/about.html", "/pricing.html"}
