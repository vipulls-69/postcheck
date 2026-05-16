"""Tests for ``PlainHtmlAdapter``."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from postcheck.analysis.route_resolver.adapters.plain_html import PlainHtmlAdapter
from postcheck.core.types import Route

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "plain_html_site"


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(body).lstrip("\n"), encoding="utf-8")
    return p


async def test_detect_true_for_example_site():
    adapter = PlainHtmlAdapter()
    assert await adapter.detect(EXAMPLE) is True


async def test_detect_false_for_empty_dir(tmp_path):
    adapter = PlainHtmlAdapter()
    assert await adapter.detect(tmp_path) is False


async def test_detect_skips_node_modules(tmp_path):
    _write(tmp_path, "node_modules/foo/page.html", "<html></html>")
    adapter = PlainHtmlAdapter()
    assert await adapter.detect(tmp_path) is False


async def test_list_routes_maps_index_and_top_level_pages():
    adapter = PlainHtmlAdapter()
    routes = await adapter.list_routes(EXAMPLE)
    by_url = {r.url_path: r for r in routes}
    assert "/" in by_url
    assert "/about.html" in by_url
    assert "/pricing.html" in by_url


async def test_list_routes_nested_index(tmp_path):
    _write(tmp_path, "docs/index.html", "<html><head></head><body></body></html>")
    adapter = PlainHtmlAdapter()
    routes = await adapter.list_routes(tmp_path)
    assert {r.url_path for r in routes} == {"/docs/"}


async def test_files_for_route_includes_local_assets():
    adapter = PlainHtmlAdapter()
    routes = await adapter.list_routes(EXAMPLE)
    home = next(r for r in routes if r.url_path == "/")
    files = await adapter.files_for_route(home)
    files_posix = {f.as_posix() for f in files}
    assert "index.html" in files_posix
    assert "shared.js" in files_posix
    assert "shared.css" in files_posix


async def test_external_assets_are_skipped(tmp_path):
    _write(
        tmp_path,
        "page.html",
        """
        <html><head>
          <link rel="stylesheet" href="https://cdn.example.com/x.css" />
          <link rel="stylesheet" href="./local.css" />
        </head><body>
          <script src="//cdn.example.com/x.js"></script>
          <script src="./local.js"></script>
        </body></html>
        """,
    )
    _write(tmp_path, "local.css", "")
    _write(tmp_path, "local.js", "")
    adapter = PlainHtmlAdapter()
    routes = await adapter.list_routes(tmp_path)
    [route] = routes
    files = {f.as_posix() for f in await adapter.files_for_route(route)}
    assert files == {"page.html", "local.css", "local.js"}


async def test_dependency_graph_links_pages_to_assets():
    adapter = PlainHtmlAdapter()
    await adapter.list_routes(EXAMPLE)
    graph = await adapter.dependency_graph()
    assert graph is not None
    edges = set(graph.edges)
    assert ("index.html", "shared.js") in edges
    assert ("index.html", "shared.css") in edges


async def test_state_graph_returns_none(tmp_path):
    _write(tmp_path, "page.html", "<html></html>")
    adapter = PlainHtmlAdapter()
    await adapter.list_routes(tmp_path)
    assert await adapter.state_graph() is None


async def test_template_selector_map_returns_none(tmp_path):
    from postcheck.core.types import Symbol

    adapter = PlainHtmlAdapter()
    sym = Symbol(
        name="x", kind="html_element", file=Path("page.html"), start_line=1, end_line=1
    )
    assert await adapter.template_selector_map(sym) is None


async def test_dependency_graph_none_before_list_routes():
    adapter = PlainHtmlAdapter()
    assert await adapter.dependency_graph() is None
