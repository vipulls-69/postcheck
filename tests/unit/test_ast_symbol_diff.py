"""Tests for ``postcheck.analysis.ast_symbol_diff``.

Uses real source-file fixtures under ``tests/fixtures/ast/`` written into a
temporary project root, plus synthetic ``FileChange`` rows whose ``hunks``
carry the unified-diff text the analyzer parses.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from postcheck.analysis.ast_symbol_diff import compute_symbol_diff
from postcheck.core.types import FileChange, Hunk

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ast"


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(content).lstrip("\n"), encoding="utf-8")
    return p


def _hunk(*, old_start=1, old_lines=0, new_start=1, new_lines=0, lines: list[str]) -> Hunk:
    body = "\n".join(
        [f"@@ -{old_start},{old_lines} +{new_start},{new_lines} @@", *lines]
    )
    return Hunk(
        old_start=old_start,
        old_lines=old_lines,
        new_start=new_start,
        new_lines=new_lines,
        content=body,
    )


# ---------------------------------------------------------------------------
# TS / JS
# ---------------------------------------------------------------------------


async def test_function_added_in_modified_file(tmp_path):
    _write(
        tmp_path,
        "src/util.ts",
        """
        export function existing() { return 1; }
        export function freshlyAdded() { return 2; }
        """,
    )
    fc = FileChange(
        path=Path("src/util.ts"),
        kind="modified",
        language="typescript",
        hunks=[
            _hunk(
                old_start=2,
                old_lines=0,
                new_start=2,
                new_lines=1,
                lines=["+export function freshlyAdded() { return 2; }"],
            )
        ],
    )
    out = await compute_symbol_diff([fc], tmp_path)
    by_name = {sc.symbol.name: sc for sc in out}
    assert by_name["freshlyAdded"].kind == "added"
    assert by_name["freshlyAdded"].symbol.kind == "function"
    assert by_name["freshlyAdded"].symbol.exported is True
    assert "existing" not in by_name


async def test_function_modified_in_place(tmp_path):
    _write(
        tmp_path,
        "src/util.ts",
        """
        export function compute() {
          return 42;
        }
        """,
    )
    fc = FileChange(
        path=Path("src/util.ts"),
        kind="modified",
        language="typescript",
        hunks=[
            _hunk(
                old_start=2,
                old_lines=1,
                new_start=2,
                new_lines=1,
                lines=["-  return 1;", "+  return 42;"],
            )
        ],
    )
    out = await compute_symbol_diff([fc], tmp_path)
    [sc] = out
    assert sc.symbol.name == "compute"
    assert sc.kind == "modified"


async def test_class_deleted_via_deleted_file(tmp_path):
    fc = FileChange(
        path=Path("src/Old.ts"),
        kind="deleted",
        language="typescript",
        hunks=[
            _hunk(
                old_start=1,
                old_lines=3,
                new_start=0,
                new_lines=0,
                lines=[
                    "-export class GoneClass {",
                    "-  method() { return 1; }",
                    "-}",
                ],
            )
        ],
    )
    out = await compute_symbol_diff([fc], tmp_path)
    names = {(sc.symbol.name, sc.symbol.kind, sc.kind) for sc in out}
    assert ("GoneClass", "class", "deleted") in names
    assert ("method", "method", "deleted") in names


async def test_export_renamed_counts_as_delete_plus_add(tmp_path):
    _write(
        tmp_path,
        "src/api.ts",
        """
        export function newName() { return 1; }
        """,
    )
    fc = FileChange(
        path=Path("src/api.ts"),
        kind="modified",
        language="typescript",
        hunks=[
            _hunk(
                old_start=1,
                old_lines=1,
                new_start=1,
                new_lines=1,
                lines=[
                    "-export function oldName() { return 1; }",
                    "+export function newName() { return 1; }",
                ],
            )
        ],
    )
    out = await compute_symbol_diff([fc], tmp_path)
    by = {(sc.symbol.name, sc.kind) for sc in out}
    assert ("newName", "added") in by
    assert ("oldName", "deleted") in by


async def test_jsx_component_classified_as_component(tmp_path):
    _write(
        tmp_path,
        "src/Widget.tsx",
        """
        export const Widget = () => <div data-testid="w" />;
        """,
    )
    fc = FileChange(
        path=Path("src/Widget.tsx"),
        kind="added",
        language="typescript",
        hunks=[],
    )
    out = await compute_symbol_diff([fc], tmp_path)
    [sc] = out
    assert sc.symbol.name == "Widget"
    assert sc.symbol.kind == "component"
    assert sc.kind == "added"


async def test_added_file_emits_added_for_all_symbols(tmp_path):
    _write(
        tmp_path,
        "src/created.js",
        """
        export function a() {}
        export function b() {}
        class C {}
        """,
    )
    fc = FileChange(
        path=Path("src/created.js"),
        kind="added",
        language="javascript",
        hunks=[],
    )
    out = await compute_symbol_diff([fc], tmp_path)
    kinds = {sc.symbol.name: sc.kind for sc in out}
    assert kinds == {"a": "added", "b": "added", "C": "added"}


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


async def test_html_element_changed_by_testid(tmp_path):
    _write(
        tmp_path,
        "page.html",
        """
        <html><body>
          <button data-testid="save">Save</button>
          <button data-testid="cancel">Cancel</button>
        </body></html>
        """,
    )
    fc = FileChange(
        path=Path("page.html"),
        kind="modified",
        language="html",
        hunks=[
            _hunk(
                old_start=3,
                old_lines=1,
                new_start=3,
                new_lines=1,
                lines=[
                    '-  <button data-testid="cancel">Abort</button>',
                    '+  <button data-testid="cancel">Cancel</button>',
                ],
            )
        ],
    )
    out = await compute_symbol_diff([fc], tmp_path)
    by = {sc.symbol.name: sc.kind for sc in out}
    assert by.get("button#cancel") == "modified"
    assert "button#save" not in by  # untouched


async def test_html_script_tag_added(tmp_path):
    _write(
        tmp_path,
        "page.html",
        """
        <html><body>
          <script src="a.js"></script>
          <script src="b.js"></script>
        </body></html>
        """,
    )
    fc = FileChange(
        path=Path("page.html"),
        kind="modified",
        language="html",
        hunks=[
            _hunk(
                old_start=3,
                old_lines=0,
                new_start=3,
                new_lines=1,
                lines=['+  <script src="b.js"></script>'],
            )
        ],
    )
    out = await compute_symbol_diff([fc], tmp_path)
    by = {sc.symbol.name: sc.kind for sc in out}
    assert by.get("script:b.js") == "added"


# ---------------------------------------------------------------------------
# CSS (file granularity)
# ---------------------------------------------------------------------------


async def test_css_file_emits_single_file_marker(tmp_path):
    _write(tmp_path, "styles.css", ".x { color: red; }\n")
    fc = FileChange(
        path=Path("styles.css"),
        kind="modified",
        language="css",
        hunks=[
            _hunk(
                old_start=1,
                old_lines=1,
                new_start=1,
                new_lines=1,
                lines=["-.x { color: blue; }", "+.x { color: red; }"],
            )
        ],
    )
    out = await compute_symbol_diff([fc], tmp_path)
    [sc] = out
    assert sc.symbol.name == "__file__"
    assert sc.symbol.kind == "css_rule"
    assert sc.kind == "modified"


# ---------------------------------------------------------------------------
# Unsupported / mixed
# ---------------------------------------------------------------------------


async def test_unknown_file_types_skipped_silently(tmp_path):
    fc_md = FileChange(path=Path("README.md"), kind="modified", hunks=[])
    fc_py = FileChange(path=Path("scripts/build.py"), kind="modified", hunks=[])
    fc_json = FileChange(path=Path("config.json"), kind="modified", hunks=[])
    out = await compute_symbol_diff([fc_md, fc_py, fc_json], tmp_path)
    assert out == []


async def test_mixed_inputs_processed_in_order(tmp_path):
    _write(tmp_path, "src/a.ts", "export function a() {}\n")
    _write(tmp_path, "styles.css", ".y {}\n")
    out = await compute_symbol_diff(
        [
            FileChange(path=Path("src/a.ts"), kind="added", language="typescript", hunks=[]),
            FileChange(path=Path("README.md"), kind="modified", hunks=[]),
            FileChange(
                path=Path("styles.css"),
                kind="modified",
                language="css",
                hunks=[
                    _hunk(
                        old_start=1,
                        old_lines=1,
                        new_start=1,
                        new_lines=1,
                        lines=["-.y { color: blue; }", "+.y {}"],
                    )
                ],
            ),
        ],
        tmp_path,
    )
    assert [sc.symbol.name for sc in out] == ["a", "__file__"]


async def test_modified_file_missing_on_disk_raises(tmp_path):
    fc = FileChange(
        path=Path("src/ghost.ts"),
        kind="modified",
        language="typescript",
        hunks=[],
    )
    from postcheck.core.errors import AnalysisError

    with pytest.raises(AnalysisError):
        await compute_symbol_diff([fc], tmp_path)
