"""Browser automation: CDP attach, scenario runner, target locator."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .cdp_attach import CDPAttachConfig, CDPSession, LaunchConfig, attach, launch_browser

if TYPE_CHECKING:
    from ..core.config import Settings


async def get_browser(
    settings: "Settings",
    *,
    cdp_endpoint: str | None = None,
    storage_state_path: Path | None = None,
) -> CDPSession:
    """Return a :class:`CDPSession` selected by ``settings.launch_mode``.

    Hides the attach-vs-launch distinction from callers. ``attach``
    connects to a user-controlled Chrome over CDP; ``launch`` spawns a
    Playwright-managed Chromium (for Codespaces, CI, headless servers).
    """
    if settings.launch_mode == "launch":
        return await launch_browser(
            LaunchConfig(
                headless=settings.launch_headless,
                args=list(settings.launch_args),
                storage_state_path=storage_state_path,
            )
        )
    return await attach(
        CDPAttachConfig(
            port=settings.chrome_debug_port,
            endpoint=cdp_endpoint or None,
            storage_state_path=storage_state_path,
        )
    )


__all__ = [
    "CDPAttachConfig",
    "CDPSession",
    "LaunchConfig",
    "attach",
    "get_browser",
    "launch_browser",
]
