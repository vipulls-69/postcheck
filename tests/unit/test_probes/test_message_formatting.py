"""Lock the per-kind message formatting of the shared probe helpers.

These helpers (``format_runtime_message`` / ``format_network_message`` /
``format_storage_message`` in :mod:`postcheck.probes.shared`) are the
*only* place that translates a probe payload into the human-readable
string that ends up in ``Bug.title``. The regression they guard against
is the one where ``runtime_console_error`` / ``runtime_console_warning``
events were emitted with no message-projection step, producing blank
bug rows in the Markdown report.

The contract every helper must honour:

* always returns a non-empty, non-whitespace string;
* pre-existing kinds (``runtime_error``, ``runtime_warning``,
  ``page_crash``, ``network_error``, ``navigation_failure``,
  ``storage_quota_error``, ``storage_serialization_error``) keep their
  v0 wire format byte-for-byte so the existing ``test_reporting``
  golden assertions still pass.
"""
from __future__ import annotations

import pytest

from postcheck.probes.shared import (
    format_network_message,
    format_runtime_message,
    format_storage_message,
)


# ---------------------------------------------------------------------------
# runtime probe
# ---------------------------------------------------------------------------


def test_format_runtime_console_error_uses_console_text() -> None:
    msg = format_runtime_message(
        "runtime_console_error",
        {"message": "TypeError: handler is not a function"},
    )
    assert msg == "console.error: TypeError: handler is not a function"


def test_format_runtime_console_error_truncates_long_text_at_200_chars() -> None:
    long_text = "x" * 500
    msg = format_runtime_message(
        "runtime_console_error", {"message": long_text}
    )
    # ``console.error: `` (16) + 200 truncated body
    assert msg.startswith("console.error: ")
    body = msg[len("console.error: ") :]
    assert len(body) == 200
    assert body.endswith("…")


def test_format_runtime_console_error_falls_back_when_text_empty() -> None:
    msg = format_runtime_message(
        "runtime_console_error",
        {
            "message": "",
            "location": {"url": "http://x/app.js", "line": 42},
        },
    )
    assert "runtime_console_error" in msg
    assert "http://x/app.js:42" in msg
    assert msg.strip() == msg  # non-empty, no trailing whitespace


def test_format_runtime_console_error_falls_back_when_text_and_location_missing() -> None:
    msg = format_runtime_message("runtime_console_error", {})
    assert msg.strip() != ""
    assert "runtime_console_error" in msg


def test_format_runtime_console_warning_distinguished_from_error() -> None:
    err = format_runtime_message(
        "runtime_console_error", {"message": "broken"}
    )
    warn = format_runtime_message(
        "runtime_console_warning", {"message": "broken"}
    )
    assert err.startswith("console.error:")
    assert warn.startswith("console.warn:")
    assert err != warn


def test_format_runtime_pageerror_unchanged_from_v1_behavior() -> None:
    """Regression guard: ``runtime_error`` keeps its v0 wire format.

    The :mod:`bug_aggregator` golden tests assert ``"TypeError" in
    bug.title`` against a payload like this — the prefix must remain
    ``"Runtime error: "`` and the message must survive untruncated up
    to 140 chars.
    """
    payload = {
        "kind": "runtime_error",
        "message": "TypeError: x is undefined",
        "stack": "Error\n    at handler (foo.tsx:42:9)",
    }
    msg = format_runtime_message("runtime_error", payload)
    assert msg == "Runtime error: TypeError: x is undefined"


def test_format_runtime_page_crash_returns_fixed_string() -> None:
    assert format_runtime_message("page_crash", {}) == "Page crashed"


def test_format_runtime_warning_keeps_140_char_limit() -> None:
    payload = {"message": "y" * 500}
    msg = format_runtime_message("runtime_warning", payload)
    body = msg[len("Console warning: ") :]
    assert len(body) == 140
    assert body.endswith("…")


# ---------------------------------------------------------------------------
# network probe
# ---------------------------------------------------------------------------


def test_format_network_aborted_includes_url_and_method() -> None:
    msg = format_network_message(
        "network_aborted",
        {
            "method": "POST",
            "url": "https://api.example.com/save",
            "error": "net::ERR_ABORTED",
        },
    )
    assert "POST" in msg
    assert "https://api.example.com/save" in msg
    assert "Network request aborted" in msg


def test_format_network_timeout_includes_url() -> None:
    msg = format_network_message(
        "network_timeout",
        {"method": "GET", "url": "/api/slow", "error": "net::ERR_TIMED_OUT"},
    )
    assert msg == "Network request timed out: GET /api/slow (net::ERR_TIMED_OUT)"


def test_format_network_dns_error_includes_error_token() -> None:
    msg = format_network_message(
        "network_dns_error",
        {"method": "GET", "url": "http://nope.invalid/", "error": "net::ERR_NAME_NOT_RESOLVED"},
    )
    assert "DNS resolution failed" in msg
    assert "ERR_NAME_NOT_RESOLVED" in msg


def test_format_network_connection_error_includes_url() -> None:
    msg = format_network_message(
        "network_connection_error",
        {"method": "GET", "url": "http://127.0.0.1:9/", "error": "net::ERR_CONNECTION_REFUSED"},
    )
    assert "Network connection failed" in msg
    assert "http://127.0.0.1:9/" in msg


def test_format_network_error_keeps_v1_wire_format() -> None:
    """Regression guard for the pre-existing ``network_error`` kind."""
    msg = format_network_message(
        "network_error",
        {
            "method": "GET",
            "url": "/api/widgets",
            "status": 500,
            "resource_type": "fetch",
        },
    )
    assert msg == "Network error: GET /api/widgets → 500"


# ---------------------------------------------------------------------------
# storage probe
# ---------------------------------------------------------------------------


def test_format_storage_idb_version_error_includes_db_name_and_versions() -> None:
    msg = format_storage_message(
        "storage_idb_version_error",
        {
            "database": "probe-stress-db",
            "store": "users",
            "old_version": 2,
            "new_version": 1,
            "error_name": "VersionError",
        },
    )
    assert "IndexedDB upgrade failed" in msg
    assert "probe-stress-db" in msg
    assert "users" in msg
    assert "2" in msg and "1" in msg
    assert "VersionError" in msg


def test_format_storage_idb_quota_error_includes_db_name() -> None:
    msg = format_storage_message(
        "storage_idb_quota_error",
        {"database": "huge-db", "error_name": "QuotaExceededError"},
    )
    assert "IndexedDB quota exceeded" in msg
    assert "huge-db" in msg


def test_format_storage_idb_blocked_includes_db_name() -> None:
    msg = format_storage_message(
        "storage_idb_blocked", {"database": "myDb"}
    )
    assert "IndexedDB open blocked" in msg
    assert "myDb" in msg


def test_format_storage_idb_serialization_error_includes_db_and_store() -> None:
    msg = format_storage_message(
        "storage_idb_serialization_error",
        {"database": "myDb", "store": "items", "error_name": "DataCloneError"},
    )
    assert "IndexedDB value not cloneable" in msg
    assert "myDb" in msg
    assert "items" in msg


def test_format_storage_quota_error_keeps_v1_wire_format() -> None:
    """Regression guard for the pre-existing localStorage quota kind."""
    msg = format_storage_message(
        "storage_quota_error",
        {"storage": "localStorage", "key": "blob"},
    )
    assert msg == "Storage quota exceeded writing localStorage['blob']"


def test_format_storage_serialization_error_keeps_v1_wire_format() -> None:
    msg = format_storage_message(
        "storage_serialization_error",
        {"error": "TypeError: Converting circular structure to JSON"},
    )
    assert msg.startswith("Storage serialization failed: ")
    assert "circular" in msg


# ---------------------------------------------------------------------------
# Contract: every helper must always return a non-empty, non-whitespace
# string. This is the property that, had it been enforced before, would
# have caught the original blank-row regression.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn,kind",
    [
        (format_runtime_message, "runtime_error"),
        (format_runtime_message, "runtime_warning"),
        (format_runtime_message, "runtime_console_error"),
        (format_runtime_message, "runtime_console_warning"),
        (format_runtime_message, "page_crash"),
        (format_network_message, "network_error"),
        (format_network_message, "navigation_failure"),
        (format_network_message, "network_aborted"),
        (format_network_message, "network_timeout"),
        (format_network_message, "network_dns_error"),
        (format_network_message, "network_connection_error"),
        (format_storage_message, "storage_quota_error"),
        (format_storage_message, "storage_serialization_error"),
        (format_storage_message, "storage_idb_version_error"),
        (format_storage_message, "storage_idb_quota_error"),
        (format_storage_message, "storage_idb_blocked"),
        (format_storage_message, "storage_idb_serialization_error"),
    ],
)
def test_formatter_never_returns_empty_string_on_empty_payload(fn, kind) -> None:
    out = fn(kind, {})
    assert isinstance(out, str)
    assert out.strip() != ""
