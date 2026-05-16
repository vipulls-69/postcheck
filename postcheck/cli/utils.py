"""Shared CLI helpers (v0).

Currently provides project-root discovery: walk up from a starting
directory looking for a ``.postcheck/`` directory.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

from sqlmodel.ext.asyncio.session import AsyncSession

from ..core.errors import PostcheckError
from ..db.models import Run
from ..db.repository import find_runs_by_id_prefix, get_run


class AmbiguousRunIdError(PostcheckError):
    """Raised when a short run id prefix matches multiple runs."""


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default cwd) looking for ``.postcheck/``.

    Returns the directory containing ``.postcheck`` or ``None`` if none is
    found before the filesystem root.
    """
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".postcheck").is_dir():
            return candidate
    return None


async def resolve_run_id(
    session: AsyncSession, short_id: str, *, org_id: UUID
) -> Run | None:
    """Resolve a run id or short prefix to the matching :class:`Run`.

    - Returns ``None`` if no run matches.
    - Returns the unique match for either a full UUID or an unambiguous prefix.
    - Raises :class:`AmbiguousRunIdError` if the prefix matches multiple runs.
    """
    short_id = short_id.strip()
    if not short_id:
        return None

    # Try full-UUID parse first (cheap, exact lookup).
    try:
        full = UUID(short_id)
    except ValueError:
        full = None
    if full is not None:
        return await get_run(session, org_id=org_id, run_id=full)

    matches = await find_runs_by_id_prefix(
        session, org_id=org_id, prefix=short_id
    )
    if not matches:
        return None
    if len(matches) > 1:
        ids = ", ".join(str(r.id)[:8] for r in matches[:5])
        raise AmbiguousRunIdError(
            f"run id prefix '{short_id}' is ambiguous ({len(matches)} matches: {ids}...)"
        )
    return matches[0]


__all__ = [
    "AmbiguousRunIdError",
    "find_project_root",
    "resolve_run_id",
]
