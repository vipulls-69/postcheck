"""Playwright CDP attach (v0).

Connects to a user-controlled Chrome instance over the Chrome DevTools
Protocol via Playwright's ``connect_over_cdp``. We never launch Chrome
ourselves — the user is expected to be running a session they have already
authenticated, with ``--remote-debugging-port=<port>`` and a dedicated
``--user-data-dir=...`` (Chrome locks the default profile).

We always create a *new* browser context rather than reusing an existing
one, so probes never interact with the user's real tabs. When the caller
supplies ``storage_state_path``, the new context is initialised with that
state (cookies + origins) so headless / CI runs can resume an existing
authenticated session.

Failures raise :class:`CDPAttachError`, whose message embeds the exact
relaunch command for macOS, Linux, and Windows.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import async_playwright
from playwright.async_api import Error as PlaywrightError

from ..core.errors import CDPAttachError
from .session_bridge import load_storage_state

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Playwright


@dataclass(slots=True)
class CDPAttachConfig:
    """Inputs for :func:`attach`.

    Attributes
    ----------
    port:
        Chrome remote-debugging port (used in error messages and to derive
        ``endpoint`` when the latter is not provided).
    endpoint:
        Full CDP endpoint URL. Defaults to ``http://localhost:<port>``.
    profile_name:
        Profile directory basename to surface in error messages. Purely
        informational — we never touch the profile ourselves.
    storage_state_path:
        Optional path to a Playwright ``storage_state`` JSON file. When set,
        the new context is created with the cookies / origins it contains.
    viewport:
        Optional ``(width, height)`` for the new context.
    """

    port: int = 9222
    endpoint: str | None = None
    profile_name: str = ".postcheck-chrome-profile"
    storage_state_path: Path | None = None
    viewport: tuple[int, int] | None = None


@dataclass(slots=True)
class CDPSession:
    """Active CDP session — used to keep the Playwright runtime alive.

    Call :meth:`close` (or use ``async with``) to release the connection
    without closing the user's Chrome.
    """

    playwright: Playwright
    browser: Browser
    context: BrowserContext

    async def close(self) -> None:
        # Close our context first; the underlying Chrome stays running.
        try:
            await self.context.close()
        finally:
            try:
                await self.browser.close()
            finally:
                await self.playwright.stop()

    async def __aenter__(self) -> CDPSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


async def attach(config: CDPAttachConfig | None = None) -> CDPSession:
    """Attach to a running Chrome over CDP and return a fresh context.

    Parameters
    ----------
    config:
        Connection settings. Defaults to ``CDPAttachConfig()`` (port 9222,
        ``http://localhost:9222``, no storage state).

    Returns
    -------
    CDPSession
        Holds the live Playwright runtime, the attached :class:`Browser`,
        and the freshly created :class:`BrowserContext`. Call ``close()``
        when done.

    Raises
    ------
    CDPAttachError
        If the CDP endpoint cannot be reached, embedding per-OS relaunch
        commands.
    """
    cfg = config or CDPAttachConfig()
    endpoint = cfg.endpoint or f"http://localhost:{cfg.port}"

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(endpoint)
    except PlaywrightError as exc:
        await playwright.stop()
        raise CDPAttachError(
            port=cfg.port,
            endpoint=endpoint,
            cause=str(exc),
            profile_name=cfg.profile_name,
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        await playwright.stop()
        raise CDPAttachError(
            port=cfg.port,
            endpoint=endpoint,
            cause=repr(exc),
            profile_name=cfg.profile_name,
        ) from exc

    context_kwargs: dict[str, object] = {}
    if cfg.storage_state_path is not None:
        # Validate and normalize via session_bridge so a malformed file fails
        # loudly here rather than silently producing an empty context.
        load_storage_state(cfg.storage_state_path)
        context_kwargs["storage_state"] = str(cfg.storage_state_path)
    if cfg.viewport is not None:
        w, h = cfg.viewport
        context_kwargs["viewport"] = {"width": w, "height": h}

    try:
        context = await browser.new_context(**context_kwargs)
    except PlaywrightError as exc:
        await browser.close()
        await playwright.stop()
        raise CDPAttachError(
            port=cfg.port,
            endpoint=endpoint,
            cause=f"new_context failed: {exc}",
            profile_name=cfg.profile_name,
        ) from exc

    return CDPSession(playwright=playwright, browser=browser, context=context)


@dataclass(slots=True)
class LaunchConfig:
    """Inputs for :func:`launch_browser`.

    Attributes
    ----------
    headless:
        Whether to launch Chromium without a visible window.
    args:
        Extra command-line flags forwarded to Chromium.
    storage_state_path:
        Optional Playwright ``storage_state`` JSON to seed the new context.
    viewport:
        Optional ``(width, height)`` for the new context.
    """

    headless: bool = True
    args: list[str] | None = None
    storage_state_path: Path | None = None
    viewport: tuple[int, int] | None = None


async def launch_browser(config: LaunchConfig | None = None) -> CDPSession:
    """Launch a Playwright-managed Chromium and return a fresh context.

    Mirrors :func:`attach`'s return shape so callers can treat the two
    modes interchangeably. Use this in environments where attaching to a
    user-controlled Chrome isn't possible (Codespaces, CI, headless
    servers).
    """
    cfg = config or LaunchConfig()
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(
            headless=cfg.headless,
            args=list(cfg.args) if cfg.args else [],
        )
    except Exception:
        await playwright.stop()
        raise

    context_kwargs: dict[str, object] = {}
    if cfg.storage_state_path is not None:
        load_storage_state(cfg.storage_state_path)
        context_kwargs["storage_state"] = str(cfg.storage_state_path)
    if cfg.viewport is not None:
        w, h = cfg.viewport
        context_kwargs["viewport"] = {"width": w, "height": h}

    try:
        context = await browser.new_context(**context_kwargs)
    except Exception:
        await browser.close()
        await playwright.stop()
        raise

    return CDPSession(playwright=playwright, browser=browser, context=context)


__all__ = ["CDPAttachConfig", "CDPSession", "LaunchConfig", "attach", "launch_browser"]
