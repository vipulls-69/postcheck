"""Plain HTML adapter — file-system routing over ``.html`` files (v0).

Detection:
    Returns ``True`` if the project root contains at least one ``.html`` file
    outside of common build / vendor directories.

Routing:
    Every ``.html`` file is a route. URL is derived from the path relative to
    the project root: ``index.html`` -> ``/``, ``foo/bar.html`` ->
    ``/foo/bar.html``, ``foo/index.html`` -> ``/foo/``.

Dependencies:
    Each route's files include the HTML file itself plus every local script
    and stylesheet it references via ``<script src="...">`` and
    ``<link rel="stylesheet" href="...">`` tags. External (``http(s)://``,
    ``//cdn``, ``data:``) URLs are skipped.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import tree_sitter as ts
import tree_sitter_html as ts_html

from ....core.errors import AnalysisError
from ....core.types import Route, Selector, StateGraph, Symbol, SymbolGraph

_HTML_LANG = ts.Language(ts_html.language())
_EXCLUDED_DIRS = frozenset(
    {"node_modules", "dist", "build", ".next", ".turbo", ".svelte-kit", "out", ".git", ".venv"}
)


def _iter_html_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*.html")):
        if any(part in _EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        out.append(path)
    return out


def _url_for(rel: Path) -> str:
    parts = list(rel.parts)
    if not parts:
        return "/"
    if parts[-1] == "index.html":
        parts = parts[:-1]
        return "/" + ("/".join(parts) + "/" if parts else "")
    return "/" + "/".join(parts)


def _attr_value(attr: ts.Node) -> tuple[str | None, str | None]:
    name: str | None = None
    value: str | None = None
    for child in attr.children:
        if child.type == "attribute_name":
            name = child.text.decode("utf-8", errors="replace").lower()
        elif child.type == "quoted_attribute_value":
            for sub in child.children:
                if sub.type == "attribute_value":
                    value = sub.text.decode("utf-8", errors="replace")
                    break
        elif child.type == "attribute_value":
            value = child.text.decode("utf-8", errors="replace")
    return name, value


def _is_local_ref(href: str) -> bool:
    if not href:
        return False
    lowered = href.lower()
    if lowered.startswith(("http://", "https://", "//", "data:", "mailto:", "javascript:")):
        return False
    if lowered.startswith("#"):
        return False
    return True


def _parse_includes(html_path: Path) -> list[str]:
    """Return local href/src strings referenced from the HTML file."""
    try:
        source = html_path.read_bytes()
    except OSError as exc:
        raise AnalysisError(
            f"Failed to read HTML file {html_path}: {exc}",
            context={"path": str(html_path)},
        ) from exc
    parser = ts.Parser(_HTML_LANG)
    tree = parser.parse(source)
    refs: list[str] = []

    def walk(node: ts.Node) -> None:
        if node.type in {"element", "script_element", "style_element"}:
            start_tag: ts.Node | None = None
            for child in node.children:
                if child.type in {"start_tag", "self_closing_tag"}:
                    start_tag = child
                    break
            if start_tag is not None:
                tag_name = ""
                attrs: dict[str, str] = {}
                for child in start_tag.children:
                    if child.type == "tag_name":
                        tag_name = child.text.decode("utf-8", errors="replace").lower()
                    elif child.type == "attribute":
                        k, v = _attr_value(child)
                        if k and v is not None:
                            attrs[k] = v
                if tag_name == "script" and _is_local_ref(attrs.get("src", "")):
                    refs.append(attrs["src"])
                elif (
                    tag_name == "link"
                    and attrs.get("rel", "").lower() == "stylesheet"
                    and _is_local_ref(attrs.get("href", ""))
                ):
                    refs.append(attrs["href"])
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return refs


def _resolve_local(html_path: Path, ref: str, project_root: Path) -> Path | None:
    cleaned = ref.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    candidate = (html_path.parent / cleaned).resolve()
    try:
        candidate.relative_to(project_root.resolve())
    except ValueError:
        return None
    if not candidate.exists():
        return None
    return candidate


class PlainHtmlAdapter:
    """File-system routing adapter for vanilla HTML sites."""

    name = "plain_html"

    def __init__(self) -> None:
        self._project_root: Path | None = None
        self._routes: list[Route] | None = None

    async def detect(self, project_root: Path) -> bool:
        def _check() -> bool:
            return bool(_iter_html_files(project_root))

        return await asyncio.to_thread(_check)

    async def list_routes(self, project_root: Path) -> list[Route]:
        def _build() -> list[Route]:
            root = project_root.resolve()
            routes: list[Route] = []
            for html in _iter_html_files(root):
                rel = html.relative_to(root)
                files: list[Path] = [rel]
                for ref in _parse_includes(html):
                    resolved = _resolve_local(html, ref, root)
                    if resolved is not None:
                        files.append(resolved.relative_to(root))
                routes.append(
                    Route(
                        url_path=_url_for(rel),
                        files=files,
                        name=rel.as_posix(),
                    )
                )
            return routes

        routes = await asyncio.to_thread(_build)
        self._project_root = project_root
        self._routes = routes
        return routes

    async def files_for_route(self, route: Route) -> list[Path]:
        return list(route.files)

    async def dependency_graph(self) -> SymbolGraph | None:
        if self._routes is None:
            return None
        nodes: list[str] = []
        seen: set[str] = set()
        edges: list[tuple[str, str]] = []
        for route in self._routes:
            if not route.files:
                continue
            page = route.files[0].as_posix()
            if page not in seen:
                nodes.append(page)
                seen.add(page)
            for asset in route.files[1:]:
                key = asset.as_posix()
                if key not in seen:
                    nodes.append(key)
                    seen.add(key)
                edges.append((page, key))
        return SymbolGraph(nodes=nodes, edges=edges)

    async def state_graph(self) -> StateGraph | None:
        return None

    async def template_selector_map(self, symbol: Symbol) -> list[Selector] | None:  # noqa: ARG002
        return None


__all__ = ["PlainHtmlAdapter"]
