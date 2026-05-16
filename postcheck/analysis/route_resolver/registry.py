"""Adapter detection and dispatch (v0).

``detect_adapter`` tries each registered adapter's ``detect()`` method in
order, returning the first that matches. An explicit override (e.g. from
``Settings.adapter``) skips detection entirely.

Adapter ordering is most-specific first: framework adapters are tried before
file-system fallbacks so a Vite + React project is not mis-classified as a
plain HTML site.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...core.errors import AdapterDetectionError
from .adapters.plain_html import PlainHtmlAdapter
from .adapters.react_vite import ReactViteAdapter

if TYPE_CHECKING:
    from .contract import RouteAdapter


_REGISTRY: tuple[type, ...] = (ReactViteAdapter, PlainHtmlAdapter)
_BY_NAME: dict[str, type] = {cls().name: cls for cls in _REGISTRY}


async def detect_adapter(
    project_root: Path, *, override: str = "auto"
) -> RouteAdapter:
    """Return a constructed adapter for ``project_root``.

    Parameters
    ----------
    project_root:
        Root of the project under analysis.
    override:
        Either ``"auto"`` (run detection) or the ``name`` attribute of a
        registered adapter (``"plain_html"``, ``"react_vite"``).

    Raises
    ------
    AdapterDetectionError
        If ``override`` is unknown, or detection finds no match.
    """
    if override != "auto":
        cls = _BY_NAME.get(override)
        if cls is None:
            raise AdapterDetectionError(
                f"Unknown adapter override {override!r}",
                context={"override": override, "available": sorted(_BY_NAME)},
            )
        return cls()

    tried: list[str] = []
    for cls in _REGISTRY:
        adapter = cls()
        tried.append(adapter.name)
        if await adapter.detect(project_root):
            return adapter

    raise AdapterDetectionError(
        f"No route adapter matched project at {project_root}",
        context={"project_root": str(project_root), "tried": tried},
    )


__all__ = ["detect_adapter"]
