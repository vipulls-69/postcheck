"""Tests for ``postcheck.core.logging`` — redaction + sink wiring."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from postcheck.core.logging import (
    configure_logging,
    get_logger,
    redact_value,
)


def _reset_logging() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_redact_value_scrubs_bearer_token() -> None:
    raw = "Authorization: Bearer abc123XYZ_LONG_TOKEN_DEFGHIJK"
    out = redact_value(raw)
    assert "abc123XYZ_LONG_TOKEN_DEFGHIJK" not in out
    assert "<redacted>" in out


def test_redact_value_scrubs_jwt_in_nested_dict() -> None:
    payload = {
        "headers": {
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.AbCd-_xyz"
        },
        "list": ["sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa", "ok"],
    }
    out = redact_value(payload)
    assert "<redacted>" in out["headers"]["Authorization"]
    assert out["list"][0] == "<redacted>"
    assert out["list"][1] == "ok"


def test_configure_logging_writes_json_and_redacts(tmp_path: Path) -> None:
    _reset_logging()
    path = configure_logging(tmp_path, verbose=False)
    assert path is not None
    assert path == tmp_path / ".postcheck" / "postcheck.log"

    log = get_logger("test")
    log.info(
        "network.request",
        url="https://api.example.com/v1/users",
        headers={"Authorization": "Bearer ghp_abcdefghijklmnopqrstuvwx"},
        body="api_key='sk-abcdefghijklmnopqrst'",
    )

    # Force flush.
    for h in logging.getLogger().handlers:
        h.flush()

    content = path.read_text(encoding="utf-8")
    assert "<redacted>" in content
    assert "ghp_abcdefghijklmnopqrstuvwx" not in content
    assert "sk-abcdefghijklmnopqrst" not in content
    # Plain fields preserved.
    assert "api.example.com" in content

    # Each line is valid JSON.
    for line in content.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        assert "event" in parsed
        assert "timestamp" in parsed
        assert parsed["level"] == "info"


def test_configure_logging_silent_stderr_by_default(
    tmp_path: Path, capsys: object
) -> None:
    _reset_logging()
    configure_logging(tmp_path, verbose=False)
    log = get_logger("test")
    log.info("hello", foo="bar")
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    # Silent on stderr unless --verbose.
    assert captured.err == ""


def test_configure_logging_verbose_pretty_stderr(
    tmp_path: Path, capsys: object
) -> None:
    _reset_logging()
    configure_logging(tmp_path, verbose=True)
    log = get_logger("test")
    log.info("hello_verbose", foo="bar")
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "hello_verbose" in captured.err
    assert "foo" in captured.err


def test_configure_logging_without_project_root_no_file(tmp_path: Path) -> None:
    _reset_logging()
    path = configure_logging(None, verbose=False)
    assert path is None
    # Sanity: log doesn't blow up.
    get_logger("x").info("noop")
