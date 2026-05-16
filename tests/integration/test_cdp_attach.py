"""Integration tests for ``postcheck.browser.cdp_attach``.

The failure-path test (no Chrome running) is always run. The success-path
test is gated behind ``POSTCHECK_RUN_CHROME_TESTS=1`` plus a reachable
Chrome on the configured debug port — see ``tests/README.md``.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from postcheck.browser.cdp_attach import CDPAttachConfig, attach
from postcheck.browser.session_bridge import load_storage_state
from postcheck.core.errors import CDPAttachError, ConfigError


def _free_port() -> int:
    """Bind ephemeral port, close, and return the (likely-free) number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chrome_reachable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


requires_chrome = pytest.mark.requires_chrome


# ---------------------------------------------------------------------------
# Failure path — always runs.
# ---------------------------------------------------------------------------


async def test_attach_raises_with_per_os_relaunch_commands():
    port = _free_port()
    cfg = CDPAttachConfig(port=port)
    with pytest.raises(CDPAttachError) as exc_info:
        await attach(cfg)

    err = exc_info.value
    msg = str(err)

    # The message must surface the user's port and per-OS commands.
    assert str(port) in msg
    for label in ("macOS:", "Linux:", "Windows:"):
        assert label in msg
    # Each command embeds the debug port and a dedicated user-data-dir flag.
    assert f"--remote-debugging-port={port}" in msg
    assert "--user-data-dir" in msg

    # Structured context exposes all three commands by key.
    cmds = err.relaunch_commands
    assert set(cmds) == {"macos", "linux", "windows"}
    for cmd in cmds.values():
        assert f"--remote-debugging-port={port}" in cmd
        assert "--user-data-dir" in cmd

    assert err.code == "cdp_attach_error"
    assert err.context["port"] == port
    assert err.context["endpoint"].endswith(f":{port}")


async def test_attach_with_invalid_storage_state_raises_config_error(tmp_path):
    # Even before contacting Chrome, a malformed storage_state should fail
    # via the session_bridge validator.
    bad = tmp_path / "state.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_storage_state(bad)


async def test_attach_with_missing_storage_state_file_raises_config_error(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(ConfigError):
        load_storage_state(missing)


# ---------------------------------------------------------------------------
# Success path — requires a manually-launched Chrome. See tests/README.md.
# ---------------------------------------------------------------------------


_RUN_CHROME = os.environ.get("POSTCHECK_RUN_CHROME_TESTS") == "1"
_PORT = int(os.environ.get("POSTCHECK_CHROME_PORT", "9222"))


@requires_chrome
@pytest.mark.skipif(
    not _RUN_CHROME or not _chrome_reachable(_PORT),
    reason=(
        "Set POSTCHECK_RUN_CHROME_TESTS=1 and run Chrome with "
        "--remote-debugging-port — see tests/README.md."
    ),
)
async def test_attach_returns_working_session_against_real_chrome():
    cfg = CDPAttachConfig(port=_PORT)
    session = await attach(cfg)
    try:
        page = await session.context.new_page()
        await page.goto("about:blank")
        assert await page.evaluate("1 + 1") == 2
        await page.close()
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Storage-state happy path (no browser required).
# ---------------------------------------------------------------------------


def test_load_storage_state_accepts_valid_schema(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(
        '{"cookies": [], "origins": []}', encoding="utf-8"
    )
    out = load_storage_state(state)
    assert out == {"cookies": [], "origins": []}


def test_load_storage_state_rejects_missing_keys(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"cookies": []}', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_storage_state(state)
    assert "origins" in str(exc.value)
