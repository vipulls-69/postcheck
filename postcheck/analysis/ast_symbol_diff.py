"""tree-sitter symbol-level diff for TS/JS/HTML/CSS (v0).

Given a set of :class:`FileChange` rows, derives a list of
:class:`SymbolChange` rows by parsing the post-change file contents on disk
and the hunk patch text, then matching symbol names against +/- regions:

* TS/JS: function declarations, class declarations, methods, top-level
  variable declarators, exported type/interface declarations, and JSX
  components (PascalCase top-level vars).
* HTML: elements bearing ``id`` or ``data-testid`` attributes, plus
  ``<script src=...>`` and ``<link rel=stylesheet href=...>`` tags.
* CSS: file-granularity only — one ``SymbolChange`` per changed file with
  ``symbol.name == "__file__"``.

Anything else is skipped silently.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import tree_sitter as ts
import tree_sitter_css as ts_css
import tree_sitter_html as ts_html
import tree_sitter_javascript as ts_js
import tree_sitter_typescript as ts_ts

from ..core.errors import AnalysisError
from ..core.types import ChangeKind, FileChange, Hunk, Symbol, SymbolChange, SymbolKind

# ---------------------------------------------------------------------------
# Languages (cached)
# ---------------------------------------------------------------------------


def _make_languages() -> dict[str, ts.Language]:
    return {
        "typescript": ts.Language(ts_ts.language_typescript()),
        "tsx": ts.Language(ts_ts.language_tsx()),
        "javascript": ts.Language(ts_js.language()),
        "html": ts.Language(ts_html.language()),
        "css": ts.Language(ts_css.language()),
    }


_LANGS: dict[str, ts.Language] = _make_languages()


def _parser_for(key: str) -> ts.Parser:
    return ts.Parser(_LANGS[key])


def _language_key(path: Path, declared: str | None) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".tsx":
        return "tsx"
    if suffix == ".ts":
        return "typescript"
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        return "javascript"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".css", ".scss"}:
        return "css"
    if declared in {"typescript", "javascript", "html", "css"}:
        return declared
    return None


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Extracted:
    name: str
    kind: SymbolKind
    start_line: int
    end_line: int
    exported: bool


def _walk(node: ts.Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _text_of(node: ts.Node | None) -> str:
    if node is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _is_under_export(node: ts.Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == "export_statement":
            return True
        parent = parent.parent
    return False


def _classify_var(name: str) -> SymbolKind:
    return "component" if name and name[0].isupper() else "variable"


def _extract_js_ts(source: bytes, lang_key: str) -> list[_Extracted]:
    parser = _parser_for(lang_key)
    tree = parser.parse(source)
    root = tree.root_node
    out: list[_Extracted] = []
    seen: set[tuple[str, str, int]] = set()

    def _emit(name: str, kind: SymbolKind, node: ts.Node, exported: bool) -> None:
        if not name:
            return
        key = (name, kind, node.start_point[0])
        if key in seen:
            return
        seen.add(key)
        out.append(
            _Extracted(
                name=name,
                kind=kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                exported=exported,
            )
        )

    for node in _walk(root):
        t = node.type
        if t == "function_declaration":
            name_node = node.child_by_field_name("name")
            _emit(_text_of(name_node), "function", node, _is_under_export(node))
        elif t == "class_declaration":
            name_node = node.child_by_field_name("name")
            _emit(_text_of(name_node), "class", node, _is_under_export(node))
        elif t == "method_definition":
            name_node = node.child_by_field_name("name")
            _emit(_text_of(name_node), "method", node, False)
        elif t == "variable_declarator":
            # only top-level (under program → lexical_declaration / variable_declaration)
            parent = node.parent
            grand = parent.parent if parent else None
            if (
                grand is not None
                and grand.type in {"program", "export_statement"}
                and parent is not None
                and parent.type in {"lexical_declaration", "variable_declaration"}
            ):
                name_node = node.child_by_field_name("name")
                name = _text_of(name_node)
                _emit(name, _classify_var(name), node, _is_under_export(node))
        elif t in {"type_alias_declaration", "interface_declaration"}:
            name_node = node.child_by_field_name("name")
            _emit(_text_of(name_node), "export", node, _is_under_export(node))

    return out


def _extract_html(source: bytes) -> list[_Extracted]:
    parser = _parser_for("html")
    tree = parser.parse(source)
    out: list[_Extracted] = []

    def _attrs(start_tag: ts.Node) -> dict[str, str]:
        result: dict[str, str] = {}
        for child in start_tag.children:
            if child.type != "attribute":
                continue
            name_node = next((c for c in child.children if c.type == "attribute_name"), None)
            value_node = next(
                (c for c in child.children if c.type == "quoted_attribute_value"), None
            )
            value: str = ""
            if value_node is not None:
                inner = next(
                    (c for c in value_node.children if c.type == "attribute_value"),
                    None,
                )
                value = _text_of(inner)
            if name_node is not None:
                result[_text_of(name_node).lower()] = value
        return result

    for node in _walk(tree.root_node):
        if node.type not in {"element", "script_element"}:
            continue
        start_tag = next((c for c in node.children if c.type == "start_tag"), None)
        if start_tag is None:
            continue
        tag_node = next((c for c in start_tag.children if c.type == "tag_name"), None)
        tag = _text_of(tag_node).lower()
        attrs = _attrs(start_tag)

        if tag == "script" and "src" in attrs:
            out.append(
                _Extracted(
                    name=f"script:{attrs['src']}",
                    kind="html_element",
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    exported=False,
                )
            )
            continue
        if tag == "link" and "href" in attrs:
            out.append(
                _Extracted(
                    name=f"link:{attrs['href']}",
                    kind="html_element",
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    exported=False,
                )
            )
            continue

        marker = attrs.get("data-testid") or attrs.get("id")
        if marker:
            out.append(
                _Extracted(
                    name=f"{tag}#{marker}",
                    kind="html_element",
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    exported=False,
                )
            )

    return out


def _extract_symbols(source: bytes, lang_key: str) -> list[_Extracted]:
    if lang_key in {"typescript", "tsx", "javascript"}:
        return _extract_js_ts(source, lang_key)
    if lang_key == "html":
        return _extract_html(source)
    return []


# ---------------------------------------------------------------------------
# Hunk-side reconstruction
# ---------------------------------------------------------------------------


def _split_hunks(hunks: list[Hunk]) -> tuple[bytes, bytes]:
    """Return (plus_text, minus_text) joined across all hunks."""
    plus: list[str] = []
    minus: list[str] = []
    for h in hunks:
        for line in h.content.splitlines():
            if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
                continue
            if line.startswith("+"):
                plus.append(line[1:])
            elif line.startswith("-"):
                minus.append(line[1:])
    return ("\n".join(plus) + "\n").encode("utf-8"), ("\n".join(minus) + "\n").encode("utf-8")


def _overlaps_hunk(start_line: int, end_line: int, hunks: list[Hunk]) -> bool:
    for h in hunks:
        if h.new_lines == 0:
            continue
        h_start = h.new_start
        h_end = h.new_start + max(h.new_lines, 1) - 1
        if not (end_line < h_start or start_line > h_end):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def compute_symbol_diff(
    changes: list[FileChange],
    project_root: Path,
) -> list[SymbolChange]:
    """Derive symbol-level changes from file-level changes.

    Args:
        changes: ``FileChange`` rows from :func:`diff_against`.
        project_root: Root used to read the post-change file contents.

    Returns:
        A flat list of :class:`SymbolChange`. Files in unsupported languages
        are skipped silently.

    Raises:
        AnalysisError: a TS/JS/HTML file claims to exist but cannot be read.
    """
    return await asyncio.to_thread(_compute_sync, changes, project_root)


def _compute_sync(changes: list[FileChange], project_root: Path) -> list[SymbolChange]:
    out: list[SymbolChange] = []
    for fc in changes:
        out.extend(_diff_one(fc, project_root))
    return out


def _diff_one(fc: FileChange, project_root: Path) -> list[SymbolChange]:
    lang = _language_key(fc.path, fc.language)
    if lang is None:
        return []

    if lang == "css":
        sym = Symbol(
            name="__file__",
            kind="css_rule",
            file=fc.path,
            start_line=1,
            end_line=1,
            exported=False,
        )
        return [SymbolChange(file=fc.path, symbol=sym, kind=fc.kind, hunks=list(fc.hunks))]

    plus_bytes, minus_bytes = _split_hunks(list(fc.hunks))

    if fc.kind == "added":
        current = _read_current(project_root, fc.path)
        return [
            SymbolChange(
                file=fc.path,
                symbol=_to_symbol(e, fc.path),
                kind="added",
                hunks=list(fc.hunks),
            )
            for e in _extract_symbols(current, lang)
        ]

    if fc.kind == "deleted":
        return [
            SymbolChange(
                file=fc.path,
                symbol=_to_symbol(e, fc.path),
                kind="deleted",
                hunks=list(fc.hunks),
            )
            for e in _extract_symbols(minus_bytes, lang)
        ]

    # modified or renamed — read current content
    current = _read_current(project_root, fc.path)
    current_extracted = _extract_symbols(current, lang)
    current_names = {e.name for e in current_extracted}
    added_names = {e.name for e in _extract_symbols(plus_bytes, lang)}
    removed_names = {e.name for e in _extract_symbols(minus_bytes, lang)}

    results: list[SymbolChange] = []
    for e in current_extracted:
        in_plus = e.name in added_names
        in_minus = e.name in removed_names
        kind: ChangeKind | None = None
        if in_plus and not in_minus:
            kind = "added"
        elif in_plus and in_minus:
            kind = "modified"
        elif _overlaps_hunk(e.start_line, e.end_line, list(fc.hunks)):
            kind = "modified"
        if kind is not None:
            results.append(
                SymbolChange(
                    file=fc.path,
                    symbol=_to_symbol(e, fc.path),
                    kind=kind,
                    hunks=list(fc.hunks),
                )
            )

    # deletions: names that were in the minus side and aren't in the current file
    for e in _extract_symbols(minus_bytes, lang):
        if e.name in current_names or e.name in added_names:
            continue
        results.append(
            SymbolChange(
                file=fc.path,
                symbol=_to_symbol(e, fc.path),
                kind="deleted",
                hunks=list(fc.hunks),
            )
        )

    return results


def _to_symbol(e: _Extracted, path: Path) -> Symbol:
    return Symbol(
        name=e.name,
        kind=e.kind,
        file=path,
        start_line=max(e.start_line, 1),
        end_line=max(e.end_line, e.start_line, 1),
        exported=e.exported,
    )


def _read_current(project_root: Path, rel: Path) -> bytes:
    full = project_root / rel
    try:
        return full.read_bytes()
    except OSError as exc:
        raise AnalysisError(
            f"Could not read {full}: {exc}",
            context={"path": str(full)},
        ) from exc


__all__ = ["compute_symbol_diff"]
