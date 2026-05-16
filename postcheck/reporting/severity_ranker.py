"""v1 only — see CLAUDE.md.

Will sort and re-weight bugs across a run using probe confidence, blast
radius, and (eventually) historical data on which classes of regressions
the user has accepted as bugs vs noise.
"""
from __future__ import annotations

from typing import Any


def rank_bugs(*_args: Any, **_kwargs: Any) -> Any:
    """v1 only — see CLAUDE.md."""
    raise NotImplementedError("v1 only — see CLAUDE.md")


class SeverityRanker:
    """v1 only — see CLAUDE.md."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError("v1 only — see CLAUDE.md")


__all__ = ["SeverityRanker", "rank_bugs"]
