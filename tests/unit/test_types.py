"""Tests for ``postcheck.core.types``.

Covers serialization round-trips for every public model and Literal
validation rejecting invalid enum values.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from postcheck.core.types import (
    AffectedRoute,
    Bug,
    BugLocation,
    FileChange,
    Hunk,
    Interaction,
    ProbeEvent,
    Route,
    Selector,
    StateGraph,
    Symbol,
    SymbolChange,
    SymbolGraph,
    VerifyOptions,
    VerifyResult,
)


def _roundtrip(model):
    cls = type(model)
    dumped = model.model_dump(mode="json")
    restored = cls.model_validate(dumped)
    assert restored == model
    return restored


def test_hunk_roundtrip():
    _roundtrip(Hunk(old_start=1, old_lines=3, new_start=1, new_lines=4, content="@@ ..."))


def test_file_change_roundtrip_all_kinds():
    for kind in ("added", "modified", "deleted", "renamed"):
        fc = FileChange(
            path=Path("src/a.ts"),
            kind=kind,
            old_path=Path("src/old.ts") if kind == "renamed" else None,
            hunks=[Hunk(old_start=1, old_lines=1, new_start=1, new_lines=2)],
            language="typescript",
        )
        _roundtrip(fc)


def test_symbol_and_symbol_change_roundtrip():
    sym = Symbol(
        name="handleClick",
        kind="function",
        file=Path("src/Button.tsx"),
        start_line=10,
        end_line=20,
        exported=True,
    )
    _roundtrip(sym)
    sc = SymbolChange(
        file=sym.file,
        symbol=sym,
        kind="modified",
        hunks=[Hunk(old_start=11, old_lines=1, new_start=11, new_lines=2)],
    )
    _roundtrip(sc)


def test_route_and_selector_roundtrip():
    _roundtrip(Route(url_path="/about", files=[Path("src/About.tsx")], name="about"))
    _roundtrip(Selector(strategy="test_id", value="home-action"))


def test_affected_route_full_union_accepted():
    """All v1 reason/confidence values must validate even though v0 only emits direct/high."""
    for reason in ("direct", "reverse_graph", "declared", "llm_suggested"):
        for confidence in ("high", "medium", "low", "explicit"):
            ar = AffectedRoute(route="/", reason=reason, confidence=confidence)
            _roundtrip(ar)


def test_affected_route_rejects_unknown_reason():
    with pytest.raises(ValidationError):
        AffectedRoute(route="/", reason="guessed", confidence="high")


def test_affected_route_rejects_unknown_confidence():
    with pytest.raises(ValidationError):
        AffectedRoute(route="/", reason="direct", confidence="maybe")


def test_symbol_change_rejects_unknown_kind():
    sym = Symbol(name="x", kind="function", file=Path("a.ts"), start_line=1, end_line=2)
    with pytest.raises(ValidationError):
        SymbolChange(file=sym.file, symbol=sym, kind="mutated")


def test_interaction_and_probe_event_roundtrip():
    sel = Selector(strategy="test_id", value="home-action")
    inter = Interaction(kind="click", selector=sel)
    _roundtrip(inter)
    evt = ProbeEvent(
        probe="runtime",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        route="/",
        interaction_index=0,
        payload={"message": "TypeError"},
    )
    _roundtrip(evt)


def test_bug_full_provenance_roundtrip():
    bug = Bug(
        probe="runtime",
        route="/",
        severity="high",
        confidence="deterministic",
        title="Uncaught TypeError",
        detail="Cannot read properties of undefined",
        interaction=Interaction(
            kind="click",
            selector=Selector(strategy="test_id", value="home-action"),
        ),
        suspected_location=BugLocation(file=Path("src/Home.tsx"), line=12),
        evidence={"stack": "..."},
    )
    _roundtrip(bug)


def test_bug_rejects_unknown_probe():
    with pytest.raises(ValidationError):
        Bug(probe="vibes", route="/", confidence="deterministic", title="x")


def test_bug_rejects_unknown_confidence():
    with pytest.raises(ValidationError):
        Bug(probe="runtime", route="/", confidence="vibes-based", title="x")


def test_graphs_roundtrip():
    _roundtrip(SymbolGraph(nodes=["a.ts::x", "b.ts::y"], edges=[("a.ts::x", "b.ts::y")]))
    _roundtrip(
        StateGraph(
            nodes=["store::user"],
            reads=[("a.ts::x", "store::user")],
            writes=[("b.ts::y", "store::user")],
        )
    )


def test_verify_options_defaults():
    opts = VerifyOptions(project_root=Path("/tmp/proj"))
    assert opts.cdp_endpoint == "http://localhost:9222"
    assert opts.headless is False
    _roundtrip(opts)


def test_verify_result_roundtrip_all_statuses():
    for status in ("pending", "running", "completed", "failed", "cancelled"):
        result = VerifyResult(status=status)
        _roundtrip(result)


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        Route(url_path="/", files=[], name=None, mystery=1)  # type: ignore[call-arg]
