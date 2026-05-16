"""Unit tests for ``bug_aggregator``, ``reporter``, ``severity_ranker``."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from postcheck.core.types import (
    Bug,
    FileChange,
    Hunk,
    Interaction,
    ProbeEvent,
    Selector,
    Symbol,
    SymbolChange,
)
from postcheck.reporting import (
    aggregate_bugs,
    to_json,
    to_markdown,
    write_report,
)
from postcheck.reporting.severity_ranker import SeverityRanker, rank_bugs


# ---------------------------------------------------------------------------
# severity_ranker stub
# ---------------------------------------------------------------------------


def test_severity_ranker_class_is_stub():
    with pytest.raises(NotImplementedError, match="v1 only"):
        SeverityRanker()


def test_severity_ranker_function_is_stub():
    with pytest.raises(NotImplementedError, match="v1 only"):
        rank_bugs([])


# ---------------------------------------------------------------------------
# aggregate_bugs
# ---------------------------------------------------------------------------


def _ev(probe: str, route: str, payload: dict, *, idx: int | None = None) -> ProbeEvent:
    return ProbeEvent(
        probe=probe,  # type: ignore[arg-type]
        route=route,
        interaction_index=idx,
        payload=payload,
    )


def test_no_events_no_bugs():
    assert aggregate_bugs([]) == []


def test_informational_storage_write_dropped():
    events = [_ev("storage", "/", {"kind": "storage_write", "key": "x"})]
    assert aggregate_bugs(events) == []


def test_unknown_kind_silently_dropped():
    events = [_ev("runtime", "/", {"kind": "made_up_thing"})]
    assert aggregate_bugs(events) == []


def test_payload_without_kind_dropped():
    events = [_ev("runtime", "/", {"no_kind": "here"})]
    assert aggregate_bugs(events) == []


def test_runtime_error_becomes_bug():
    events = [
        _ev(
            "runtime",
            "/",
            {
                "kind": "runtime_error",
                "message": "TypeError: x is not a function",
                "stack": "Error\n    at handler (foo.tsx:42:9)",
            },
            idx=2,
        )
    ]
    [bug] = aggregate_bugs(events)
    assert bug.probe == "runtime"
    assert bug.route == "/"
    assert bug.severity == "high"
    assert bug.confidence == "deterministic"
    assert "TypeError" in bug.title
    assert "TypeError" in bug.detail
    assert bug.evidence["kind"] == "runtime_error"


def test_runtime_warning_is_low_severity_heuristic():
    events = [
        _ev(
            "runtime",
            "/",
            {"kind": "runtime_warning", "message": "deprecation notice"},
        )
    ]
    [bug] = aggregate_bugs(events)
    assert bug.severity == "low"
    assert bug.confidence == "heuristic"


def test_page_crash_is_critical():
    events = [_ev("runtime", "/", {"kind": "page_crash"})]
    [bug] = aggregate_bugs(events)
    assert bug.severity == "critical"
    assert bug.confidence == "deterministic"


def test_network_error_renders_method_url_status():
    events = [
        _ev(
            "network",
            "/dashboard",
            {
                "kind": "network_error",
                "method": "GET",
                "url": "/api/widgets",
                "status": 500,
                "resource_type": "fetch",
                "confidence": "high",
            },
        )
    ]
    [bug] = aggregate_bugs(events)
    assert bug.title == "Network error: GET /api/widgets → 500"
    assert "500" not in bug.detail  # status is in the title
    assert "network confidence: high" in bug.detail


def test_navigation_failure_is_critical():
    events = [
        _ev(
            "network",
            "/x",
            {
                "kind": "navigation_failure",
                "method": "GET",
                "url": "/x",
                "error": "net::ERR_FAILED",
            },
        )
    ]
    [bug] = aggregate_bugs(events)
    assert bug.severity == "critical"
    assert "ERR_FAILED" in bug.title


def test_storage_quota_and_serialization():
    events = [
        _ev("storage", "/", {
            "kind": "storage_quota_error", "storage": "localStorage",
            "key": "blob", "attempted_size": 5_500_000,
            "error": "QuotaExceededError",
        }),
        _ev("storage", "/", {
            "kind": "storage_serialization_error", "storage": "json",
            "error": "TypeError: Converting circular structure to JSON",
        }),
    ]
    bugs = aggregate_bugs(events)
    assert [b.severity for b in bugs] == ["high", "medium"]
    assert "blob" in bugs[0].title
    assert "5500000" in bugs[0].detail or "5_500_000" in bugs[0].detail or "5500000 bytes" in bugs[0].detail
    assert "circular" in bugs[1].detail.lower()


def test_ui_no_change_and_overlay():
    events = [
        _ev("ui", "/", {
            "kind": "ui_no_change",
            "selector": {"strategy": "test_id", "value": "save"},
        }, idx=1),
        _ev("ui", "/", {
            "kind": "ui_overlay_blocks",
            "selector": {"strategy": "test_id", "value": "submit"},
            "phase": "before",
            "cover_descriptor": "div#modal",
        }, idx=2),
    ]
    bugs = aggregate_bugs(events)
    assert bugs[0].confidence == "heuristic"
    assert "save" in bugs[0].title
    assert bugs[1].severity == "medium"
    assert "div#modal" in bugs[1].detail
    assert "submit" in bugs[1].title


def test_scenario_failure_navigate_phase_is_critical():
    events = [
        _ev("scenario", "/", {
            "type": "scenario_failure",
            "phase": "navigate",
            "error": "TimeoutError: goto exceeded 30000ms",
        }),
        _ev("scenario", "/", {
            "type": "scenario_failure",
            "phase": "interact",
            "error": "ClickError: covered",
        }, idx=0),
    ]
    bugs = aggregate_bugs(events)
    assert bugs[0].severity == "critical"
    assert bugs[1].severity == "medium"
    assert all(b.confidence == "deterministic" for b in bugs)
    assert "navigate" in bugs[0].title


def test_suspected_location_via_explicit_file_line():
    fc = FileChange(path=Path("src/foo.tsx"), kind="modified")
    events = [
        _ev("runtime", "/", {
            "kind": "runtime_error", "message": "boom",
            "stack": "TypeError\n    at handler (src/foo.tsx:42:9)",
        }),
    ]
    [bug] = aggregate_bugs(events, file_changes=[fc])
    assert bug.suspected_location is not None
    assert bug.suspected_location.file == Path("src/foo.tsx")
    assert bug.suspected_location.line == 42


def test_suspected_location_substring_fallback_to_hunk_start():
    fc = FileChange(
        path=Path("src/widgets.tsx"),
        kind="modified",
        hunks=[Hunk(old_start=10, old_lines=2, new_start=15, new_lines=4)],
    )
    events = [
        _ev("network", "/", {
            "kind": "network_error", "method": "GET",
            "url": "/api/data?from=src/widgets.tsx",
            "status": 500,
        }),
    ]
    [bug] = aggregate_bugs(events, file_changes=[fc])
    assert bug.suspected_location is not None
    assert bug.suspected_location.file == Path("src/widgets.tsx")
    assert bug.suspected_location.line == 15


def test_suspected_location_falls_back_to_first_symbol():
    sc = SymbolChange(
        file=Path("src/handlers.ts"),
        symbol=Symbol(
            name="onSave", kind="function",
            file=Path("src/handlers.ts"), start_line=88, end_line=120,
        ),
        kind="modified",
    )
    events = [_ev("runtime", "/", {"kind": "runtime_error", "message": "x"})]
    [bug] = aggregate_bugs(events, symbol_changes=[sc])
    assert bug.suspected_location is not None
    assert bug.suspected_location.file == Path("src/handlers.ts")
    assert bug.suspected_location.line == 88


def test_suspected_location_none_when_no_match():
    events = [_ev("runtime", "/", {"kind": "runtime_error", "message": "x"})]
    [bug] = aggregate_bugs(events)
    assert bug.suspected_location is None


def test_interaction_lookup_per_route():
    interactions = {
        "/": [
            Interaction(kind="click", selector=Selector(strategy="test_id", value="a")),
            Interaction(kind="click", selector=Selector(strategy="test_id", value="b")),
        ]
    }
    events = [
        _ev("runtime", "/", {"kind": "runtime_error", "message": "x"}, idx=1),
        _ev("runtime", "/", {"kind": "runtime_error", "message": "y"}, idx=99),
    ]
    bugs = aggregate_bugs(events, interactions_by_route=interactions)
    assert bugs[0].interaction is not None
    assert bugs[0].interaction.selector.value == "b"
    # Out-of-range index → no interaction attached, but bug still emitted.
    assert bugs[1].interaction is None


def test_aggregator_preserves_event_order():
    events = [
        _ev("runtime", "/a", {"kind": "runtime_error", "message": "1"}),
        _ev("runtime", "/b", {"kind": "runtime_error", "message": "2"}),
        _ev("runtime", "/a", {"kind": "runtime_error", "message": "3"}),
    ]
    bugs = aggregate_bugs(events)
    assert [b.evidence["message"] for b in bugs] == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# to_markdown
# ---------------------------------------------------------------------------


def _bug(
    *,
    probe: str = "runtime",
    route: str = "/",
    severity: str = "high",
    confidence: str = "deterministic",
    title: str = "boom",
    detail: str = "",
) -> Bug:
    return Bug(
        probe=probe,  # type: ignore[arg-type]
        route=route,
        severity=severity,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        title=title,
        detail=detail,
    )


def test_markdown_clean_report():
    md = to_markdown([], {"run_id": "abc-123"})
    assert "# Postcheck verification report" in md
    assert "Clean" in md
    assert "abc-123" in md
    assert md.endswith("\n")


def test_markdown_groups_by_route_then_probe():
    bugs = [
        _bug(probe="runtime", route="/a", title="r-a"),
        _bug(probe="network", route="/a", title="n-a"),
        _bug(probe="runtime", route="/b", title="r-b"),
        _bug(probe="ui", route="/a", title="u-a"),
    ]
    md = to_markdown(bugs, {"run_id": "r1"})
    # Routes alphabetically.
    a_idx = md.index("Route `/a`")
    b_idx = md.index("Route `/b`")
    assert a_idx < b_idx
    # Probe sections within /a.
    network_idx = md.index("network probe", a_idx)
    runtime_idx = md.index("runtime probe", a_idx)
    ui_idx = md.index("ui probe", a_idx)
    assert network_idx < runtime_idx < ui_idx < b_idx
    # Each title rendered.
    for t in ("r-a", "n-a", "r-b", "u-a"):
        assert t in md
    # Severity tag and confidence both visible.
    assert "[high]" in md
    assert "deterministic" in md


def test_markdown_includes_summary_tables():
    bugs = [
        _bug(severity="critical"),
        _bug(severity="high"),
        _bug(severity="high"),
    ]
    md = to_markdown(bugs, {})
    assert "| Severity | Count |" in md
    assert "| critical | 1 |" in md
    assert "| high | 2 |" in md
    assert "| Probe | Count |" in md


def test_markdown_renders_suspected_location_and_interaction():
    bug = Bug(
        probe="runtime",
        route="/",
        severity="high",
        confidence="deterministic",
        title="boom",
        detail="TypeError: x is undefined\nat handler (src/foo.ts:42)",
        suspected_location={"file": Path("src/foo.ts"), "line": 42},  # type: ignore[arg-type]
        interaction=Interaction(
            kind="click",
            selector=Selector(strategy="test_id", value="save"),
        ),
    )
    md = to_markdown([bug], {})
    assert "src/foo.ts:42" in md
    assert "click" in md
    assert "test_id" in md
    assert "save" in md


# ---------------------------------------------------------------------------
# to_json
# ---------------------------------------------------------------------------


def test_json_shape_for_clean_run():
    out = to_json([], {"run_id": "r1", "status": "completed"})
    assert out["run"] == {"run_id": "r1", "status": "completed"}
    assert out["summary"]["total"] == 0
    assert out["summary"]["by_probe"] == {}
    assert out["summary"]["by_severity"] == {}
    assert out["bugs"] == []


def test_json_summary_counts():
    bugs = [
        _bug(probe="runtime", route="/a", severity="high"),
        _bug(probe="network", route="/a", severity="high"),
        _bug(probe="runtime", route="/b", severity="medium"),
    ]
    out = to_json(bugs, {})
    s = out["summary"]
    assert s["total"] == 3
    assert s["by_probe"] == {"network": 1, "runtime": 2}
    assert s["by_severity"] == {"high": 2, "medium": 1}
    assert s["by_route"] == {"/a": 2, "/b": 1}


def test_json_bugs_are_serializable_round_trip():
    bug = Bug(
        probe="runtime",
        route="/",
        severity="high",
        confidence="deterministic",
        title="boom",
        suspected_location={"file": Path("src/foo.ts"), "line": 7},  # type: ignore[arg-type]
        detected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    out = to_json([bug], {"started_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    serialized = json.dumps(out)
    parsed = json.loads(serialized)
    assert parsed["bugs"][0]["title"] == "boom"
    assert parsed["bugs"][0]["suspected_location"]["file"] == "src/foo.ts"
    assert parsed["run"]["started_at"].startswith("2026-01-01")


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------


def test_write_report_creates_files_under_runs_run_id(tmp_path: Path):
    bugs = [_bug(title="hello")]
    paths = write_report(bugs, {"run_id": "abc-123"}, tmp_path)
    assert paths["markdown"] == tmp_path / "runs" / "abc-123" / "report.md"
    assert paths["json"] == tmp_path / "runs" / "abc-123" / "report.json"
    assert paths["markdown"].exists()
    assert paths["json"].exists()
    md = paths["markdown"].read_text(encoding="utf-8")
    assert "hello" in md
    parsed = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert parsed["bugs"][0]["title"] == "hello"


def test_write_report_missing_run_id_uses_unknown(tmp_path: Path):
    paths = write_report([], {}, tmp_path)
    assert paths["markdown"].parent == tmp_path / "runs" / "unknown"


def test_write_report_sanitises_run_id(tmp_path: Path):
    paths = write_report([], {"run_id": "../etc/passwd"}, tmp_path)
    assert paths["markdown"].is_relative_to(tmp_path / "runs")
    assert "/" not in paths["markdown"].parent.name
    assert ".." not in paths["markdown"].parent.name


def test_write_report_handles_path_and_datetime_in_metadata(tmp_path: Path):
    md_path = write_report(
        [],
        {
            "run_id": "r1",
            "project": Path("/tmp/some/proj"),
            "started_at": datetime(2026, 5, 14, tzinfo=timezone.utc),
        },
        tmp_path,
    )["json"]
    parsed = json.loads(md_path.read_text(encoding="utf-8"))
    assert parsed["run"]["project"] == "/tmp/some/proj"
    assert parsed["run"]["started_at"].startswith("2026-05-14")
