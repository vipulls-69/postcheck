"""Shared pydantic models — the contract between modules (v0).

Every model crossing module boundaries lives here. The ``AffectedRoute``
schema includes the full v1 union of ``reason`` and ``confidence`` values
even though v0 only ever emits ``reason="direct"`` / ``confidence="high"``;
this is the schema hedge described in CLAUDE.md so v1 cross-route work
does not require refactoring downstream consumers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums (Literal aliases)
# ---------------------------------------------------------------------------

ChangeKind = Literal["added", "modified", "deleted", "renamed"]
SymbolKind = Literal[
    "function",
    "class",
    "method",
    "export",
    "variable",
    "component",
    "css_rule",
    "html_element",
]
ImpactReason = Literal["direct", "reverse_graph", "declared", "llm_suggested"]
ImpactConfidence = Literal["high", "medium", "low", "explicit"]
BugConfidence = Literal["deterministic", "heuristic", "llm_judged"]
BugSeverity = Literal["info", "low", "medium", "high", "critical"]
ProbeName = Literal["runtime", "network", "storage", "ui", "scenario"]
InteractionKind = Literal[
    "navigate",
    "click",
    "type",
    "fill",
    "press",
    "hover",
    "select",
    "wait",
]
SelectorStrategy = Literal[
    "test_id",
    "role",
    "label",
    "text",
    "css",
    "xpath",
]
LocationFailureReason = Literal[
    "no_selectors_provided",
    "no_selector_matched",
]
RunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class _Model(BaseModel):
    """Shared base — strict mode, unknown fields are forbidden."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Diff / AST
# ---------------------------------------------------------------------------


class Hunk(_Model):
    """A unified-diff hunk header plus its raw patch body.

    Mirrors ``@@ -old_start,old_lines +new_start,new_lines @@`` from a git
    unified diff. ``old_lines`` / ``new_lines`` are 0 for pure
    insertions/deletions; ``old_start`` / ``new_start`` are 0 only when the
    corresponding side is empty (file added or deleted entirely).
    """

    old_start: int = Field(ge=0)
    old_lines: int = Field(ge=0)
    new_start: int = Field(ge=0)
    new_lines: int = Field(ge=0)
    content: str = ""


class FileChange(_Model):
    """A file modified between two git refs."""

    path: Path
    kind: ChangeKind
    old_path: Path | None = None
    hunks: list[Hunk] = Field(default_factory=list)
    language: str | None = None


class Symbol(_Model):
    """A named code entity referenced by the rest of the system."""

    name: str
    kind: SymbolKind
    file: Path
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    exported: bool = False


class SymbolChange(_Model):
    """A symbol-level change derived from AST diffing."""

    file: Path
    symbol: Symbol
    kind: ChangeKind
    hunks: list[Hunk] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class Route(_Model):
    """A logical route exposed by the application."""

    url_path: str
    files: list[Path] = Field(default_factory=list)
    name: str | None = None


class Selector(_Model):
    """A target-element selector with a strategy hint."""

    strategy: SelectorStrategy
    value: str
    confidence: ImpactConfidence = "high"


class AffectedRoute(_Model):
    """A route impacted by a set of symbol changes.

    The ``reason`` and ``confidence`` unions ship in v0 with only ``direct`` /
    ``high`` ever emitted. v1 widens the actual emitter without schema churn.
    """

    route: str
    reason: ImpactReason
    confidence: ImpactConfidence
    changed_symbols: list[Symbol] = Field(default_factory=list)
    suspected_selectors: list[Selector] = Field(default_factory=list)


class LocationFailure(_Model):
    """A route's selectors could not be bound to live DOM elements.

    Surfaced by ``browser.target_locator.locate`` when either the adapter
    provided no selectors at all (``no_selectors_provided``) or every
    provided selector failed to match (``no_selector_matched``). Downstream,
    the scenario runner turns this into a ``Bug`` rather than a silent skip
    ("changed handler X but couldn't find an element bound to it").
    """

    route: str
    reason: LocationFailureReason
    changed_symbols: list[Symbol] = Field(default_factory=list)
    attempted: list[Selector] = Field(default_factory=list)
    detail: str = ""


# ---------------------------------------------------------------------------
# Adapter optional graphs (forward declared; v0 emits None for these)
# ---------------------------------------------------------------------------


class SymbolGraph(_Model):
    """Directed import / call graph between symbols.

    Stored as an adjacency list of ``Symbol`` ids ("file::name") so it can be
    serialized without dragging in ``networkx`` types.
    """

    nodes: list[str] = Field(default_factory=list)
    edges: list[tuple[str, str]] = Field(default_factory=list)


class StateGraph(_Model):
    """Reads / writes against named state stores (v1)."""

    nodes: list[str] = Field(default_factory=list)
    reads: list[tuple[str, str]] = Field(default_factory=list)
    writes: list[tuple[str, str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------


class Interaction(_Model):
    """A single user-style action issued by the scenario runner."""

    kind: InteractionKind
    selector: Selector | None = None
    value: str | None = None
    url: str | None = None
    timeout_ms: int = Field(default=5000, ge=0)


class ProbeEvent(_Model):
    """A raw observation collected by a probe during a scenario."""

    probe: ProbeName
    timestamp: datetime = Field(default_factory=_utcnow)
    route: str
    interaction_index: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Bugs
# ---------------------------------------------------------------------------


class BugLocation(_Model):
    """A file:line pointer back into the diff that likely caused a bug."""

    file: Path
    line: int = Field(ge=1)


class Bug(_Model):
    """A single reported defect with full provenance."""

    probe: ProbeName
    route: str
    severity: BugSeverity = "medium"
    confidence: BugConfidence
    title: str
    detail: str = ""
    interaction: Interaction | None = None
    suspected_location: BugLocation | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    detected_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Top-level options / result
# ---------------------------------------------------------------------------


class VerifyOptions(_Model):
    """Inputs to a single verification run."""

    project_root: Path
    since: str | None = None
    routes: list[str] | None = None
    headless: bool = False
    cdp_endpoint: str = "http://localhost:9222"
    storage_state_path: Path | None = None
    exclude_globs: list[str] = Field(default_factory=list)
    dry_run: bool = False


class VerifyResult(_Model):
    """The full output of a verification run."""

    status: RunStatus
    run_id: str = ""
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    file_changes: list[FileChange] = Field(default_factory=list)
    symbol_changes: list[SymbolChange] = Field(default_factory=list)
    affected_routes: list[AffectedRoute] = Field(default_factory=list)
    events: list[ProbeEvent] = Field(default_factory=list)
    bugs: list[Bug] = Field(default_factory=list)
    report_paths: dict[str, str] = Field(default_factory=dict)
    error: str | None = None


__all__ = [
    "AffectedRoute",
    "Bug",
    "BugConfidence",
    "BugLocation",
    "BugSeverity",
    "ChangeKind",
    "FileChange",
    "Hunk",
    "ImpactConfidence",
    "ImpactReason",
    "Interaction",
    "InteractionKind",
    "LocationFailure",
    "LocationFailureReason",
    "ProbeEvent",
    "ProbeName",
    "Route",
    "RunStatus",
    "Selector",
    "SelectorStrategy",
    "StateGraph",
    "Symbol",
    "SymbolChange",
    "SymbolGraph",
    "SymbolKind",
    "VerifyOptions",
    "VerifyResult",
]
