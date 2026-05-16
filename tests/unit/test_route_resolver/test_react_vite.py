"""Tests for ``ReactViteAdapter``."""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from postcheck.analysis.route_resolver.adapters.react_vite import ReactViteAdapter
from postcheck.core.types import Symbol

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "react_vite_app"


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(body).lstrip("\n"), encoding="utf-8")
    return p


def _scaffold_minimal_vite(root: Path) -> None:
    _write(
        root,
        "package.json",
        json.dumps({"dependencies": {"react": "^18", "react-dom": "^18"}}),
    )
    _write(root, "vite.config.ts", "export default {}\n")


async def test_detect_true_for_example_app():
    adapter = ReactViteAdapter()
    assert await adapter.detect(EXAMPLE) is True


async def test_detect_false_when_no_vite_config(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"dependencies": {"react": "^18"}}))
    adapter = ReactViteAdapter()
    assert await adapter.detect(tmp_path) is False


async def test_detect_false_when_no_react_dep(tmp_path):
    _scaffold_minimal_vite(tmp_path)
    _write(tmp_path, "package.json", json.dumps({"dependencies": {}}))
    adapter = ReactViteAdapter()
    assert await adapter.detect(tmp_path) is False


async def test_list_routes_finds_router_routes_in_example():
    adapter = ReactViteAdapter()
    routes = await adapter.list_routes(EXAMPLE)
    urls = {r.url_path for r in routes}
    assert "/" in urls
    assert "/about" in urls
    assert "/settings" in urls


async def test_route_files_link_to_component_file():
    adapter = ReactViteAdapter()
    routes = await adapter.list_routes(EXAMPLE)
    about = next(r for r in routes if r.url_path == "/about")
    files = await adapter.files_for_route(about)
    files_posix = {f.as_posix() for f in files}
    assert any(p.endswith("routes/About.tsx") for p in files_posix)


async def test_dependency_graph_includes_local_imports():
    adapter = ReactViteAdapter()
    await adapter.list_routes(EXAMPLE)
    graph = await adapter.dependency_graph()
    assert graph is not None
    edges = {(a, b) for a, b in graph.edges}
    main = "src/main.tsx"
    # main.tsx imports App and the route components
    targets = {b for a, b in edges if a == main}
    assert any(t.endswith("App.tsx") for t in targets)
    assert any(t.endswith("routes/Home.tsx") for t in targets)


async def test_template_selector_map_finds_data_testid():
    adapter = ReactViteAdapter()
    await adapter.list_routes(EXAMPLE)
    sym = Symbol(
        name="Home",
        kind="component",
        file=Path("src/routes/Home.tsx"),
        start_line=3,
        end_line=20,
    )
    selectors = await adapter.template_selector_map(sym)
    assert selectors is not None
    assert any(s.strategy == "test_id" and s.value == "home-action" for s in selectors)


async def test_state_graph_returns_none():
    adapter = ReactViteAdapter()
    await adapter.list_routes(EXAMPLE)
    assert await adapter.state_graph() is None


async def test_filesystem_fallback_when_no_router(tmp_path):
    _scaffold_minimal_vite(tmp_path)
    _write(tmp_path, "src/main.tsx", "console.log('no router here')\n")
    _write(
        tmp_path,
        "src/pages/About.tsx",
        "export const About = () => <div>about</div>;\n",
    )
    _write(
        tmp_path,
        "src/pages/Index.tsx",
        "export const Index = () => <div>home</div>;\n",
    )
    adapter = ReactViteAdapter()
    routes = await adapter.list_routes(tmp_path)
    urls = {r.url_path for r in routes}
    assert urls == {"/about", "/"}


async def test_template_selector_skips_non_component_symbols():
    adapter = ReactViteAdapter()
    await adapter.list_routes(EXAMPLE)
    sym = Symbol(
        name="x",
        kind="variable",
        file=Path("src/routes/Home.tsx"),
        start_line=1,
        end_line=1,
    )
    assert await adapter.template_selector_map(sym) is None
