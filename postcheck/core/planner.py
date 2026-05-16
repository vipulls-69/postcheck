"""Verification planner.

v0
--
Trivial fixed plan: every affected route runs the four v0 probes
(``runtime``, ``network``, ``storage``, ``ui``) sequentially. No Claude
calls, no parallelism, no token budget. The structured ``ExecutionPlan``
shape exists today so v1 can swap in the Claude-backed agent without
reshaping :mod:`orchestrator`.

v1
--
Will return a richer plan: parallel vs sequential phases, subagent
fan-out for cross-route impact sets, per-route probe selection, and a
hard token budget cap. See CLAUDE.md "v1 verification engine adds".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .types import AffectedRoute, ProbeName

# Probes that ship in v0. Order is the order the scenario runner will
# attach them in — it doesn't matter for correctness (each probe owns its
# own listeners) but a stable order makes test assertions tractable.
DEFAULT_PROBES: tuple[ProbeName, ...] = (
    "runtime",
    "network",
    "storage",
    "ui",
)


@dataclass(slots=True)
class RoutePlan:
    """One affected route + the probes to run against it."""

    route: AffectedRoute
    probes: list[ProbeName] = field(default_factory=lambda: list(DEFAULT_PROBES))


@dataclass(slots=True)
class ExecutionPlan:
    """A full v0 plan: every route, sequentially."""

    routes: list[RoutePlan] = field(default_factory=list)
    parallel: bool = False  # v0: always sequential


def plan(
    affected_routes: Iterable[AffectedRoute],
    *,
    probes: Iterable[ProbeName] | None = None,
) -> ExecutionPlan:
    """Build the v0 plan: every route runs every default probe in order."""
    probe_list = list(probes) if probes is not None else list(DEFAULT_PROBES)
    return ExecutionPlan(
        routes=[
            RoutePlan(route=r, probes=list(probe_list))
            for r in affected_routes
        ],
        parallel=False,
    )


__all__ = ["DEFAULT_PROBES", "ExecutionPlan", "RoutePlan", "plan"]
