"""Integration tests for ``postcheck.browser.cdp_attach.launch_browser``.

Unlike the CDP attach tests, these always run: they don't require a
pre-running Chrome, so they're safe in CI and Codespaces. They do
require the Playwright Chromium browser binary to be installed
(``python -m playwright install chromium``).
"""
from __future__ import annotations

import pytest

from postcheck.browser import get_browser
from postcheck.browser.cdp_attach import CDPSession, LaunchConfig, launch_browser
from postcheck.core.config import load_settings


async def test_launch_browser_navigates_about_blank_and_closes(tmp_path):
    session = await launch_browser(LaunchConfig(headless=True))
    try:
        assert isinstance(session, CDPSession)
        assert session.browser.is_connected()

        page = await session.context.new_page()
        try:
            response = await page.goto("about:blank")
            # ``about:blank`` returns no response object in Playwright; the
            # navigation succeeds when ``goto`` completes without raising.
            assert response is None or response.ok
            assert page.url == "about:blank"
        finally:
            await page.close()
    finally:
        await session.close()

    assert not session.browser.is_connected()


async def test_get_browser_dispatches_to_launch_mode(tmp_path):
    settings = load_settings(
        tmp_path,
        launch_mode="launch",
        launch_headless=True,
        launch_args=[],
    )
    assert settings.launch_mode == "launch"

    session = await get_browser(settings)
    try:
        assert session.browser.is_connected()
        page = await session.context.new_page()
        try:
            await page.goto("about:blank")
            assert page.url == "about:blank"
        finally:
            await page.close()
    finally:
        await session.close()


async def test_get_browser_attach_mode_still_requires_running_chrome(tmp_path):
    """Sanity-check: the dispatcher honours ``attach`` (the default) and
    surfaces a typed error when no Chrome is reachable."""
    from postcheck.core.errors import CDPAttachError

    settings = load_settings(
        tmp_path,
        launch_mode="attach",
        chrome_debug_port=1,  # privileged port — guaranteed unreachable
    )
    with pytest.raises(CDPAttachError):
        await get_browser(settings)
