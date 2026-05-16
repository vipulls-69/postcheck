"""Auth session bridge (v0).

Playwright represents a saved authenticated session as a ``storage_state``
JSON file with two top-level arrays:

* ``cookies`` — list of cookie objects (name, value, domain, …).
* ``origins`` — per-origin localStorage / sessionStorage entries.

In v0 we only need to *load* such a file (so headless / CI runs can attach
with an existing authenticated session) and to *save* one when the caller
wants to capture a manually-authenticated session for later replay. v1 adds
an interactive flow that drives the browser through login.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import ConfigError

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext


_REQUIRED_KEYS = ("cookies", "origins")


def _validate_schema(data: Any, source: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError(
            f"storage_state at {source} must be a JSON object",
            context={"source": source, "got": type(data).__name__},
        )
    for key in _REQUIRED_KEYS:
        if key not in data:
            raise ConfigError(
                f"storage_state at {source} missing required key {key!r}",
                context={"source": source, "missing": key},
            )
        if not isinstance(data[key], list):
            raise ConfigError(
                f"storage_state {key!r} at {source} must be a list",
                context={"source": source, "key": key, "got": type(data[key]).__name__},
            )
    return data


def load_storage_state(path: Path) -> dict[str, Any]:
    """Read and validate a Playwright ``storage_state`` JSON file.

    Raises
    ------
    ConfigError
        If the file is missing, not valid JSON, or fails schema validation.
    """
    if not path.is_file():
        raise ConfigError(
            f"storage_state file not found: {path}",
            context={"path": str(path)},
        )
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except OSError as exc:
        raise ConfigError(
            f"Could not read storage_state file {path}: {exc}",
            context={"path": str(path)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON in storage_state file {path}: {exc.msg} "
            f"(line {exc.lineno}, col {exc.colno})",
            context={"path": str(path)},
        ) from exc
    return _validate_schema(data, str(path))


async def save_storage_state(context: BrowserContext, path: Path) -> dict[str, Any]:
    """Persist ``context``'s cookies + origins to ``path``.

    Returns the saved dict for inspection. Intended as a manual helper in v0;
    v1's interactive auth flow will call into this.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    state = await context.storage_state(path=str(path))
    return _validate_schema(state, str(path))


__all__ = ["load_storage_state", "save_storage_state"]
