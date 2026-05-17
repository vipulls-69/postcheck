"""Reporter regression guards.

The original ``runtime_console_*`` regression rendered bug rows with no
content after the ``- **[severity]** `` prefix — the title was empty and
the detail block was a single bullet with just whitespace. This module
locks the structural invariants of the Markdown output so a future probe
that emits an unknown ``kind`` (or a malformed payload) can't slip past
unnoticed.

The headline assertion (``test_to_markdown_has_no_empty_lines_after_probe_prefix``)
synthesises a Bug for *every* event kind the bug aggregator recognises,
renders the report, and walks every line: bullet rows ending in
``"  -"`` or starting with ``- **[`` but with empty title content fail
the test.
"""
from __future__ import annotations

import re

from postcheck.core.types import Bug
from postcheck.probes.shared import (
    format_network_message,
    format_runtime_message,
    format_storage_message,
)
from postcheck.reporting.bug_aggregator import aggregate_bugs
from postcheck.core.types import ProbeEvent
from postcheck.reporting.reporter import to_markdown


# Every event kind the v0 aggregator recognises. Synthesised payloads
# carry the minimum fields each kind's formatter inspects, plus a few
# extras so the title contains something distinctive.
_KIND_FIXTURES: list[tuple[str, str, dict[str, object]]] = [
    ("runtime", "runtime_error", {"message": "TypeError: x"}),
    ("runtime", "runtime_warning", {"message": "deprecated"}),
    ("runtime", "runtime_console_error", {"message": "log says broken"}),
    ("runtime", "runtime_console_warning", {"message": "log says careful"}),
    ("runtime", "page_crash", {}),
    ("network", "network_error", {
        "method": "GET", "url": "/x", "status": 500,
    }),
    ("network", "navigation_failure", {
        "method": "GET", "url": "/x", "error": "net::ERR_FAILED",
    }),
    ("network", "network_aborted", {
        "method": "POST", "url": "/a", "error": "net::ERR_ABORTED",
    }),
    ("network", "network_timeout", {
        "method": "GET", "url": "/t", "error": "net::ERR_TIMED_OUT",
    }),
    ("network", "network_dns_error", {
        "method": "GET", "url": "/d", "error": "net::ERR_NAME_NOT_RESOLVED",
    }),
    ("network", "network_connection_error", {
        "method": "GET", "url": "/c", "error": "net::ERR_CONNECTION_REFUSED",
    }),
    ("storage", "storage_quota_error", {
        "storage": "localStorage", "key": "blob",
    }),
    ("storage", "storage_serialization_error", {
        "error": "TypeError: Converting circular structure",
    }),
    ("storage", "storage_idb_version_error", {
        "database": "db1", "store": "items",
        "old_version": 2, "new_version": 1,
    }),
    ("storage", "storage_idb_quota_error", {"database": "db1"}),
    ("storage", "storage_idb_blocked", {"database": "db1"}),
    ("storage", "storage_idb_serialization_error", {
        "database": "db1", "store": "items",
    }),
    ("ui", "ui_no_change", {
        "selector": {"strategy": "test_id", "value": "save"},
    }),
    ("ui", "ui_overlay_blocks", {
        "selector": {"strategy": "test_id", "value": "submit"},
        "phase": "before",
        "cover_descriptor": "div#modal",
    }),
]


def _events_for_all_kinds() -> list[ProbeEvent]:
    out: list[ProbeEvent] = []
    for probe, kind, extra in _KIND_FIXTURES:
        payload = {"kind": kind, **extra}
        out.append(ProbeEvent(probe=probe, route="/r", payload=payload))
    return out


def _bug_per_kind() -> list[Bug]:
    return aggregate_bugs(_events_for_all_kinds())


# ---------------------------------------------------------------------------
# Headline regression guard
# ---------------------------------------------------------------------------

_BULLET_RE = re.compile(r"^- \*\*\[[^\]]+\]\*\*\s+(.*?)\s+_\(confidence:")
_SUB_BULLET_RE = re.compile(r"^  - (.*)$")


def test_to_markdown_has_no_empty_lines_after_probe_prefix() -> None:
    """Every rendered bullet must have non-whitespace content.

    Covers two failure modes the original regression exhibited:

    1. Top-level row ``- **[low]**   _(confidence: ...)_`` with no title.
    2. Sub-bullet ``  -`` rendered for a whitespace-only detail line.
    """
    bugs = _bug_per_kind()
    # Every aggregator-recognised kind should produce exactly one bug
    # (i.e. nothing was silently dropped because the formatter couldn't
    # build a non-empty title).
    assert len(bugs) == len(_KIND_FIXTURES), [
        (k, [b.title for b in bugs]) for _, k, _ in _KIND_FIXTURES
    ]

    md = to_markdown(bugs, run_metadata={"run_id": "x", "project": "p"})
    offenders: list[tuple[int, str]] = []
    for i, line in enumerate(md.splitlines(), start=1):
        # Orphan sub-bullet: literally ``  -`` or ``  - `` with nothing
        # after the dash.
        if line.rstrip() == "  -":
            offenders.append((i, line))
            continue
        m_bullet = _BULLET_RE.match(line)
        if m_bullet is not None and m_bullet.group(1).strip() == "":
            offenders.append((i, line))
            continue
        m_sub = _SUB_BULLET_RE.match(line)
        if m_sub is not None and m_sub.group(1).strip() == "":
            offenders.append((i, line))
            continue
    assert offenders == [], (
        "Markdown report contains blank bullet rows:\n"
        + "\n".join(f"  line {i}: {ln!r}" for i, ln in offenders)
    )


# ---------------------------------------------------------------------------
# Per-helper title plumbing — confirms the aggregator delegates to the
# shared formatters rather than re-inventing the format inline.
# ---------------------------------------------------------------------------


def test_aggregator_uses_format_runtime_message_for_console_error() -> None:
    [bug] = aggregate_bugs(
        [
            ProbeEvent(
                probe="runtime",
                route="/r",
                payload={
                    "kind": "runtime_console_error",
                    "message": "broken thing",
                },
            )
        ]
    )
    expected = format_runtime_message(
        "runtime_console_error", {"message": "broken thing"}
    )
    assert bug.title == expected
    assert bug.title.strip() != ""


def test_aggregator_uses_format_network_message_for_aborted() -> None:
    payload = {
        "kind": "network_aborted",
        "method": "POST",
        "url": "/api/x",
        "error": "net::ERR_ABORTED",
    }
    [bug] = aggregate_bugs(
        [ProbeEvent(probe="network", route="/r", payload=payload)]
    )
    assert bug.title == format_network_message("network_aborted", payload)


def test_aggregator_uses_format_storage_message_for_idb_version_error() -> None:
    payload = {
        "kind": "storage_idb_version_error",
        "database": "db1",
        "store": "items",
        "old_version": 2,
        "new_version": 1,
    }
    [bug] = aggregate_bugs(
        [ProbeEvent(probe="storage", route="/r", payload=payload)]
    )
    assert bug.title == format_storage_message(
        "storage_idb_version_error", payload
    )
    assert "db1" in bug.title
