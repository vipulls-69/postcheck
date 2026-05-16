"""``PostcheckError`` hierarchy (v0).

All exceptions raised by core code are subclasses of :class:`PostcheckError`.
Each carries a ``code`` and structured ``context`` so wrappers (API, CLI, MCP)
can render them consistently and structured-log them without parsing strings.
"""
from __future__ import annotations

from typing import Any


class PostcheckError(Exception):
    """Base class for every error raised by postcheck core code."""

    code: str = "postcheck_error"

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context or {})

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict suitable for structured logging."""
        return {
            "code": self.code,
            "type": type(self).__name__,
            "message": self.message,
            "context": self.context,
        }


class ConfigError(PostcheckError):
    """Invalid or missing configuration."""

    code = "config_error"


class AdapterDetectionError(PostcheckError):
    """No route adapter matched the project, or detection was ambiguous."""

    code = "adapter_detection_error"


class AnalysisError(PostcheckError):
    """Failure in static analysis (diff, AST, impact mapping)."""

    code = "analysis_error"


class ScenarioExecutionError(PostcheckError):
    """A scenario could not be executed end-to-end (not a probed bug)."""

    code = "scenario_execution_error"


def _relaunch_commands(port: int, profile_name: str) -> dict[str, str]:
    """Per-OS Chrome relaunch commands with a dedicated profile dir.

    A dedicated profile dir is required because Chrome locks the default one.
    """
    return {
        "macos": (
            '"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" '
            f"--remote-debugging-port={port} "
            f'--user-data-dir="$HOME/{profile_name}"'
        ),
        "linux": (
            f"google-chrome --remote-debugging-port={port} "
            f'--user-data-dir="$HOME/{profile_name}"'
        ),
        "windows": (
            '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            f"--remote-debugging-port={port} "
            f'--user-data-dir="%USERPROFILE%\\{profile_name}"'
        ),
    }


class CDPAttachError(PostcheckError):
    """Could not attach to a Chrome DevTools Protocol endpoint.

    The exception message embeds the relaunch command for macOS, Linux, and
    Windows so the user can copy-paste a working invocation directly.
    """

    code = "cdp_attach_error"

    def __init__(
        self,
        port: int,
        *,
        endpoint: str | None = None,
        cause: str | None = None,
        profile_name: str = ".postcheck-chrome-profile",
    ) -> None:
        commands = _relaunch_commands(port, profile_name)
        endpoint = endpoint or f"http://localhost:{port}"
        cause_line = f"Underlying error: {cause}\n\n" if cause else ""
        message = (
            f"Could not attach to Chrome DevTools Protocol at {endpoint}.\n\n"
            f"{cause_line}"
            "Relaunch Chrome with remote debugging enabled, using a dedicated\n"
            "user-data-dir (Chrome locks the default profile):\n\n"
            f"  macOS:   {commands['macos']}\n"
            f"  Linux:   {commands['linux']}\n"
            f"  Windows: {commands['windows']}\n"
        )
        super().__init__(
            message,
            context={
                "port": port,
                "endpoint": endpoint,
                "profile_name": profile_name,
                "cause": cause,
                "relaunch_commands": commands,
            },
        )
        self.port = port
        self.endpoint = endpoint
        self.relaunch_commands = commands


__all__ = [
    "AdapterDetectionError",
    "AnalysisError",
    "CDPAttachError",
    "ConfigError",
    "PostcheckError",
    "ScenarioExecutionError",
]
