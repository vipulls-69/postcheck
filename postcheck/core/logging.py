"""Structured logging setup (v0).

Defaults to **silent** — the CLI is for humans reading terminals, not log
files. ``configure_logging`` opts in to:

- A ``.postcheck/postcheck.log`` JSON sink (one event per line) when a
  project root is provided.
- Pretty stderr output at DEBUG level when ``verbose=True``.

Every log event passes through :func:`redact_secrets`, which scrubs strings
matching any :data:`REDACTION_PATTERNS` regex (Bearer tokens, JWTs, API keys,
etc.) so request bodies / headers can't leak into the log file.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import structlog

from .config import REDACTION_PATTERNS

_COMPILED_REDACTIONS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in REDACTION_PATTERNS
)

_REDACTED = "<redacted>"


def redact_value(value: Any) -> Any:
    """Recursively redact secret-looking substrings from ``value``."""
    if isinstance(value, str):
        out = value
        for pat in _COMPILED_REDACTIONS:
            out = pat.sub(_REDACTED, out)
        return out
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        coerced = [redact_value(v) for v in value]
        return type(value)(coerced) if isinstance(value, tuple) else coerced
    return value


def _redact_processor(
    logger: logging.Logger,  # noqa: ARG001
    method_name: str,  # noqa: ARG001
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """structlog processor that scrubs secrets from the event dict."""
    return {k: redact_value(v) for k, v in event_dict.items()}


def configure_logging(
    project_root: Path | None = None,
    *,
    verbose: bool = False,
) -> Path | None:
    """Set up structlog for this process.

    Returns the path to the JSON log file if one was created, else ``None``.

    - ``verbose=False`` (default): silent on stderr (only WARNING+ from third
      parties), but still tee everything to the JSON file when ``project_root``
      is provided.
    - ``verbose=True``: pretty stderr at DEBUG plus the JSON file sink.
    """
    json_path: Path | None = None
    handlers: list[logging.Handler] = []

    if project_root is not None:
        log_dir = project_root / ".postcheck"
        log_dir.mkdir(parents=True, exist_ok=True)
        json_path = log_dir / "postcheck.log"
        file_handler = logging.FileHandler(json_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        # File handler emits the formatted JSON line produced by structlog
        # (the formatter is set by ProcessorFormatter below).
        handlers.append(file_handler)

    stderr_handler = logging.StreamHandler()
    if verbose:
        stderr_handler.setLevel(logging.DEBUG)
    else:
        # Suppress all stderr logging unless verbose.
        stderr_handler.setLevel(logging.CRITICAL + 1)
    handlers.append(stderr_handler)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_processor,
    ]

    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    pretty_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )
    for h in handlers:
        if isinstance(h, logging.FileHandler):
            h.setFormatter(file_formatter)
        else:
            h.setFormatter(pretty_formatter)

    root = logging.getLogger()
    # Wipe any handlers a prior call (e.g. tests) installed.
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(logging.DEBUG if (verbose or project_root) else logging.WARNING)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if (verbose or project_root) else logging.WARNING
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    return json_path


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger. Safe to call before configure_logging."""
    return structlog.get_logger(name)


__all__ = ["configure_logging", "get_logger", "redact_value"]
