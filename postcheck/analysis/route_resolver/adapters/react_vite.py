"""React + Vite adapter (v0).

Detection
    Returns ``True`` when the project root contains a ``vite.config.{ts,js,mjs}``
    file *and* declares ``react`` (or ``react-dom``) in ``package.json``.

Routing
    Two strategies, in order:

    1. **React Router (JSX form)** — scan TS/TSX/JS/JSX files under ``src/``
       for ``<Route path="..." element={<Component />}>`` elements (including
       nested children and ``<Route index />``) and resolve component names
       through the file's ES module imports.
    2. **File-system fallback** — if the JSX form yields no routes and either
       ``src/pages/`` or ``src/app/`` exists, treat each top-level component
       file in those directories as a route at ``/<basename-lowercased>``.

Capabilities
    * ``dependency_graph`` — walks every TS/JS file under ``src/`` and emits an
      edge for each local ``import ... from "./x"`` style import.
    * ``template_selector_map`` — given a symbol, parses its file, locates the
      JSX element bearing an ``onClick={<symbol or arrow>}`` attribute, and
      returns selector candidates: ``data-testid`` (test_id), ``role`` + name
      (role), then the element's text content (text).
    * ``state_graph`` — ``None`` (v1).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import tree_sitter as ts
import tree_sitter_javascript as ts_js
import tree_sitter_typescript as ts_ts

from ....core.types import Route, Selector, StateGraph, Symbol, SymbolGraph

_LANGS: dict[str, ts.Language] = {
    "tsx": ts.Language(ts_ts.language_tsx()),
    "typescript": ts.Language(ts_ts.language_typescript()),
    "javascript": ts.Language(ts_js.language()),
}

_VITE_CONFIG_NAMES = ("vite.config.ts", "vite.config.js", "vite.config.mjs", "vite.config.cjs")
_SCAN_SUFFIXES = (".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs")
_EXCLUDED_DIRS = frozenset(
    {"node_modules", "dist", "build", ".next", ".turbo", ".svelte-kit", "out", ".git", ".venv"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lang_for(path: Path) -> ts.Language | None:
    suffix = path.suffix.lower()
    if suffix == ".tsx":
        return _LANGS["tsx"]
    if suffix == ".ts":
        return _LANGS["typescript"]
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        # Use tsx parser for JSX, plain JS otherwise.
        return _LANGS["tsx"] if suffix == ".jsx" else _LANGS["javascript"]
    return None


def _text(node: ts.Node | None) -> str:
    if node is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _iter_source_files(root: Path) -> list[Path]:
    out: list[Path] = []
    src = root / "src"
    base = src if src.is_dir() else root
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _SCAN_SUFFIXES:
            continue
        if any(part in _EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        out.append(path)
    return out


def _walk(node: ts.Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] in {'"', "'", "`"} and value[-1] == value[0]:
        return value[1:-1]
    return value


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def _parse_imports(file_path: Path) -> dict[str, str]:
    """Return ``{local_name: source_string}`` for every ES import in the file."""
    lang = _lang_for(file_path)
    if lang is None:
        return {}
    try:
        source = file_path.read_bytes()
    except OSError:
        return {}
    tree = ts.Parser(lang).parse(source)
    out: dict[str, str] = {}
    for node in _walk(tree.root_node):
        if node.type != "import_statement":
            continue
        source_node = node.child_by_field_name("source")
        src = _strip_quotes(_text(source_node)) if source_node else ""
        if not src:
            continue
        clause = None
        for child in node.children:
            if child.type == "import_clause":
                clause = child
                break
        if clause is None:
            continue
        for sub in _walk(clause):
            if sub.type == "identifier" and sub.parent is clause:
                out[_text(sub)] = src
            elif sub.type == "import_specifier":
                alias = sub.child_by_field_name("alias")
                name = sub.child_by_field_name("name")
                local = _text(alias) if alias is not None else _text(name)
                if local:
                    out[local] = src
            elif sub.type == "namespace_import":
                for c in sub.children:
                    if c.type == "identifier":
                        out[_text(c)] = src
    return out


def _resolve_import(importer: Path, source: str, project_root: Path) -> Path | None:
    """Resolve ``source`` as a local file relative to ``importer``."""
    if not source.startswith((".", "/")):
        return None
    base = importer.parent / source
    candidates = [base]
    if not base.suffix:
        candidates.extend(base.with_suffix(s) for s in _SCAN_SUFFIXES)
        candidates.extend((base / "index").with_suffix(s) for s in _SCAN_SUFFIXES)
    else:
        candidates = [base]
    for cand in candidates:
        try:
            resolved = cand.resolve()
            resolved.relative_to(project_root.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


# ---------------------------------------------------------------------------
# JSX <Route> extraction
# ---------------------------------------------------------------------------


def _opening_of(element: ts.Node) -> ts.Node | None:
    if element.type == "jsx_self_closing_element":
        return element
    for child in element.children:
        if child.type == "jsx_opening_element":
            return child
    return None


def _jsx_tag_name(element: ts.Node) -> str:
    opening = _opening_of(element)
    if opening is None:
        return ""
    name_node = opening.child_by_field_name("name")
    return _text(name_node)


def _iter_jsx_attrs(element: ts.Node):
    opening = _opening_of(element)
    if opening is None:
        return
    for child in opening.children:
        if child.type == "jsx_attribute":
            yield child


def _attr_name(attr: ts.Node) -> str:
    for child in attr.children:
        if child.type == "property_identifier":
            return _text(child)
    return ""


def _attr_string_value(attr: ts.Node) -> str | None:
    for child in attr.children:
        if child.type == "string":
            inner = "".join(
                _text(s) for s in child.children if s.type == "string_fragment"
            )
            if inner:
                return inner
            return _strip_quotes(_text(child))
    return None


def _attr_expr_value(attr: ts.Node) -> ts.Node | None:
    for child in attr.children:
        if child.type == "jsx_expression":
            for sub in child.children:
                if sub.type not in {"{", "}"}:
                    return sub
    return None


def _attr_template_prefix(attr: ts.Node) -> str | None:
    """Return the static leading literal of a template-string attribute.

    For ``data-testid={`remove-${id}`}`` returns ``"remove-"``. Returns
    ``None`` when the attribute is not a template literal or has no
    non-empty leading literal segment. Used so that template-literal
    ``data-testid`` values can still seed a useful attribute-prefix CSS
    selector candidate.
    """
    expr = _attr_expr_value(attr)
    if expr is None or expr.type != "template_string":
        return None
    for child in expr.children:
        if child.type in {"`", "template_substitution"}:
            # An immediate substitution at the start means no static prefix.
            if child.type == "template_substitution":
                return None
            continue
        if child.type == "string_fragment":
            prefix = _text(child)
            return prefix or None
        # Any other node shape: bail.
        return None
    return None


def _join_url(parent: str, child: str) -> str:
    if child.startswith("/"):
        return child
    if not parent:
        return "/" + child if child else "/"
    if parent == "/":
        return "/" + child if child else "/"
    return parent.rstrip("/") + "/" + child if child else parent


def _component_name_from_element(expr: ts.Node | None) -> str | None:
    if expr is None:
        return None
    target = expr
    if target.type == "parenthesized_expression":
        for c in target.children:
            if c.type not in {"(", ")"}:
                target = c
                break
    if target.type in {"jsx_element", "jsx_self_closing_element"}:
        return _jsx_tag_name(target) or None
    return None


def _collect_routes_from_jsx(file_path: Path, project_root: Path) -> list[Route]:
    """Walk JSX ``<Route>`` trees in ``file_path`` and produce ``Route`` rows."""
    lang = _lang_for(file_path)
    if lang is None:
        return []
    try:
        source = file_path.read_bytes()
    except OSError:
        return []
    tree = ts.Parser(lang).parse(source)
    imports = _parse_imports(file_path)
    routes: list[Route] = []

    def visit(node: ts.Node, parent_url: str) -> None:
        url_for_children = parent_url
        if node.type in {"jsx_element", "jsx_self_closing_element"}:
            tag = _jsx_tag_name(node)
            if tag == "Route":
                path_value: str | None = None
                element_expr: ts.Node | None = None
                is_index = False
                for attr in _iter_jsx_attrs(node):
                    name = _attr_name(attr)
                    if name == "path":
                        path_value = _attr_string_value(attr)
                    elif name == "element":
                        element_expr = _attr_expr_value(attr)
                    elif name == "index":
                        is_index = True
                if path_value is not None:
                    url = _join_url(parent_url, path_value)
                elif is_index:
                    url = parent_url or "/"
                else:
                    url = parent_url or "/"
                comp = _component_name_from_element(element_expr)
                files: list[Path] = [file_path.relative_to(project_root)]
                if comp and comp in imports:
                    resolved = _resolve_import(file_path, imports[comp], project_root)
                    if resolved is not None:
                        files.append(resolved.relative_to(project_root))
                # only record routes that actually point at a component
                if comp is not None:
                    routes.append(Route(url_path=url, files=files, name=comp))
                url_for_children = url
        for child in node.children:
            visit(child, url_for_children)

    visit(tree.root_node, "")
    return routes


# ---------------------------------------------------------------------------
# JSX selector extraction
# ---------------------------------------------------------------------------


def _extract_selectors_from_file(
    file_path: Path, symbol_name: str
) -> list[Selector]:
    lang = _lang_for(file_path)
    if lang is None:
        return []
    try:
        source = file_path.read_bytes()
    except OSError:
        return []
    tree = ts.Parser(lang).parse(source)
    candidates: list[Selector] = []

    for node in _walk(tree.root_node):
        if node.type not in {"jsx_element", "jsx_self_closing_element"}:
            continue
        # Does this element have an onClick or similar handler?
        has_handler = False
        attrs: dict[str, str] = {}
        testid_prefix: str | None = None
        for attr in _iter_jsx_attrs(node):
            name = _attr_name(attr)
            if name.startswith("on") and name[2:3].isupper():
                has_handler = True
            sval = _attr_string_value(attr)
            if sval is not None:
                attrs[name] = sval
            elif name == "data-testid":
                # Template literal — capture its static prefix for a
                # ``[data-testid^="…"]`` candidate below.
                testid_prefix = _attr_template_prefix(attr)
        if not has_handler:
            continue
        if "data-testid" in attrs:
            candidates.append(Selector(strategy="test_id", value=attrs["data-testid"]))
        elif testid_prefix:
            candidates.append(
                Selector(strategy="css", value=f'[data-testid^="{testid_prefix}"]')
            )
        if "role" in attrs and "aria-label" in attrs:
            candidates.append(
                Selector(strategy="role", value=f"{attrs['role']}:{attrs['aria-label']}")
            )
        elif "role" in attrs:
            candidates.append(Selector(strategy="role", value=attrs["role"]))
        if "aria-label" in attrs and "role" not in attrs:
            candidates.append(Selector(strategy="label", value=attrs["aria-label"]))
        # Fallback: visible text content
        text_content = _jsx_text_content(node).strip()
        if text_content:
            candidates.append(Selector(strategy="text", value=text_content))

    # symbol_name may help disambiguate later; v0 returns all candidates from
    # the file as the symbol's component file is usually small.
    _ = symbol_name
    return candidates


def _jsx_text_content(element: ts.Node) -> str:
    parts: list[str] = []
    for node in _walk(element):
        if node.type == "jsx_text":
            parts.append(_text(node))
    return " ".join(p.strip() for p in parts if p.strip())


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ReactViteAdapter:
    """Adapter for React projects scaffolded with Vite."""

    name = "react_vite"

    def __init__(self) -> None:
        self._project_root: Path | None = None
        self._routes: list[Route] | None = None
        self._symbol_files: dict[str, Path] | None = None

    async def detect(self, project_root: Path) -> bool:
        def _check() -> bool:
            has_vite = any((project_root / n).is_file() for n in _VITE_CONFIG_NAMES)
            if not has_vite:
                return False
            pkg = project_root / "package.json"
            if not pkg.is_file():
                return False
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            deps = {
                **(data.get("dependencies") or {}),
                **(data.get("devDependencies") or {}),
            }
            return "react" in deps or "react-dom" in deps

        return await asyncio.to_thread(_check)

    async def list_routes(self, project_root: Path) -> list[Route]:
        def _build() -> list[Route]:
            root = project_root.resolve()
            jsx_routes: list[Route] = []
            for src_file in _iter_source_files(root):
                jsx_routes.extend(_collect_routes_from_jsx(src_file, root))
            if jsx_routes:
                return _dedupe_routes(jsx_routes)
            # Filesystem fallback
            for sub in ("src/pages", "src/app"):
                folder = root / sub
                if not folder.is_dir():
                    continue
                fs_routes: list[Route] = []
                for path in sorted(folder.rglob("*")):
                    if not path.is_file() or path.suffix.lower() not in {".tsx", ".jsx"}:
                        continue
                    rel = path.relative_to(root)
                    name = path.stem
                    url = "/" + name.lower() if name.lower() != "index" else "/"
                    fs_routes.append(Route(url_path=url, files=[rel], name=name))
                if fs_routes:
                    return fs_routes
            return []

        routes = await asyncio.to_thread(_build)
        self._project_root = project_root
        self._routes = routes
        # index symbol→file map by exported component name for selector lookups
        self._symbol_files = {
            r.name: project_root / r.files[-1]
            for r in routes
            if r.name and r.files
        }
        return routes

    async def files_for_route(self, route: Route) -> list[Path]:
        return list(route.files)

    async def dependency_graph(self) -> SymbolGraph | None:
        root = self._project_root
        if root is None:
            return None

        def _build() -> SymbolGraph:
            root_resolved = root.resolve()
            nodes: list[str] = []
            seen: set[str] = set()
            edges: list[tuple[str, str]] = []
            for src_file in _iter_source_files(root_resolved):
                rel = src_file.relative_to(root_resolved).as_posix()
                if rel not in seen:
                    nodes.append(rel)
                    seen.add(rel)
                for source in set(_parse_imports(src_file).values()):
                    resolved = _resolve_import(src_file, source, root_resolved)
                    if resolved is None:
                        continue
                    target = resolved.relative_to(root_resolved).as_posix()
                    if target not in seen:
                        nodes.append(target)
                        seen.add(target)
                    edges.append((rel, target))
            return SymbolGraph(nodes=nodes, edges=edges)

        return await asyncio.to_thread(_build)

    async def state_graph(self) -> StateGraph | None:
        return None

    async def template_selector_map(self, symbol: Symbol) -> list[Selector] | None:
        if symbol.kind not in {"component", "function"}:
            return None
        # Prefer the symbol's own file; fall back to the route index.
        candidate = symbol.file
        if not candidate.is_absolute() and self._project_root is not None:
            candidate = self._project_root / candidate
        if not candidate.is_file() and self._symbol_files is not None:
            candidate = self._symbol_files.get(symbol.name) or candidate
        if not candidate.is_file():
            return None
        return await asyncio.to_thread(_extract_selectors_from_file, candidate, symbol.name)


def _dedupe_routes(routes: list[Route]) -> list[Route]:
    """Merge routes sharing a URL (parent layout + index Route both yield "/")."""
    by_url: dict[str, Route] = {}
    for r in routes:
        existing = by_url.get(r.url_path)
        if existing is None:
            by_url[r.url_path] = r
            continue
        merged_files = list(existing.files)
        for f in r.files:
            if f not in merged_files:
                merged_files.append(f)
        by_url[r.url_path] = Route(
            url_path=r.url_path,
            files=merged_files,
            name=existing.name or r.name,
        )
    return list(by_url.values())


__all__ = ["ReactViteAdapter"]
