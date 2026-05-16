"""Unit tests for ``StorageProbe`` using a stub page (no Playwright launch)."""
from __future__ import annotations

from typing import Any

import pytest

from postcheck.probes import shared
from postcheck.probes.storage_probe import StorageProbe


@pytest.fixture(autouse=True)
def _clean_context():
    shared.reset_interaction_context()
    yield
    shared.reset_interaction_context()


class StubPage:
    """Minimal stand-in for ``playwright.async_api.Page``.

    ``add_init_script`` records the script. ``evaluate`` pops the next
    queued result so tests can simulate what the in-page array would
    contain after a sequence of operations.
    """

    def __init__(self) -> None:
        self.init_scripts: list[str] = []
        self._eval_queue: list[Any] = []
        self.eval_calls: list[str] = []
        self.fail_evaluate: Exception | None = None

    def queue_evaluate(self, value: Any) -> None:
        self._eval_queue.append(value)

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def evaluate(self, script: str) -> Any:
        self.eval_calls.append(script)
        if self.fail_evaluate is not None:
            raise self.fail_evaluate
        if not self._eval_queue:
            return []
        return self._eval_queue.pop(0)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_attach_installs_init_script_once():
    page = StubPage()
    probe = StorageProbe()
    await probe.attach(page)
    await probe.attach(page)  # idempotent
    assert len(page.init_scripts) == 1
    script = page.init_scripts[0]
    # Spot-check the shim wires every API the spec calls for.
    for needle in (
        "__postcheck_storage_events",
        "wrapStorage",
        "localStorage",
        "sessionStorage",
        "JSON.stringify",
        "indexedDB",
        "QuotaExceededError",
    ):
        assert needle in script, needle


async def test_collect_events_empty_before_flush():
    probe = StorageProbe()
    assert probe.collect_events() == []


async def test_flush_without_attach_is_noop():
    probe = StorageProbe()
    await probe.flush()
    assert probe.collect_events() == []


async def test_detach_blocks_subsequent_flush():
    page = StubPage()
    page.queue_evaluate([{"kind": "storage_write", "storage": "localStorage"}])
    probe = StorageProbe()
    await probe.attach(page)
    await probe.detach()
    await probe.flush()
    assert probe.collect_events() == []
    # Evaluate was never called because we bailed early.
    assert page.eval_calls == []


async def test_flush_swallows_evaluate_errors():
    page = StubPage()
    page.fail_evaluate = RuntimeError("page closed")
    probe = StorageProbe()
    await probe.attach(page)
    await probe.flush()
    assert probe.collect_events() == []


async def test_flush_handles_non_list_return():
    page = StubPage()
    page.queue_evaluate({"not": "a list"})
    probe = StorageProbe()
    await probe.attach(page)
    await probe.flush()
    assert probe.collect_events() == []


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------


async def test_storage_write_event_built():
    page = StubPage()
    page.queue_evaluate(
        [
            {
                "kind": "storage_write",
                "storage": "localStorage",
                "operation": "setItem",
                "key": "user",
                "success": True,
                "size": 4,
                "timestamp": 1700000000000,
            }
        ]
    )
    probe = StorageProbe()
    shared.set_current_route("/profile")
    shared.set_interaction_index(2)
    await probe.attach(page)
    await probe.flush()
    [event] = probe.collect_events()
    assert event.probe == "storage"
    assert event.route == "/profile"
    assert event.interaction_index == 2
    assert event.payload["kind"] == "storage_write"
    assert event.payload["storage"] == "localStorage"
    assert event.payload["operation"] == "setItem"
    assert event.payload["key"] == "user"
    assert event.payload["success"] is True
    assert event.payload["size"] == 4
    assert event.payload["in_page_timestamp_ms"] == 1700000000000
    assert "captured_at" in event.payload


async def test_quota_error_event():
    page = StubPage()
    page.queue_evaluate(
        [
            {
                "kind": "storage_quota_error",
                "storage": "localStorage",
                "operation": "setItem",
                "key": "blob",
                "success": False,
                "error": "QuotaExceededError: ...",
                "attempted_size": 5_500_000,
            }
        ]
    )
    probe = StorageProbe()
    await probe.attach(page)
    await probe.flush()
    [event] = probe.collect_events()
    assert event.payload["kind"] == "storage_quota_error"
    assert event.payload["attempted_size"] == 5_500_000
    assert event.payload["success"] is False
    assert "QuotaExceededError" in event.payload["error"]


async def test_serialization_error_event():
    page = StubPage()
    page.queue_evaluate(
        [
            {
                "kind": "storage_serialization_error",
                "storage": "json",
                "operation": "stringify",
                "key": None,
                "success": False,
                "error": "TypeError: Converting circular structure to JSON",
            }
        ]
    )
    probe = StorageProbe()
    await probe.attach(page)
    await probe.flush()
    [event] = probe.collect_events()
    assert event.payload["kind"] == "storage_serialization_error"
    assert event.payload["storage"] == "json"
    # ``key`` was None and stripped from payload.
    assert "key" not in event.payload
    assert "circular" in event.payload["error"].lower()


async def test_unknown_kinds_dropped():
    page = StubPage()
    page.queue_evaluate(
        [
            {"kind": "storage_write", "storage": "localStorage"},
            {"kind": "totally_made_up_kind"},
            "not a dict",
            {"no": "kind"},
        ]
    )
    probe = StorageProbe()
    await probe.attach(page)
    await probe.flush()
    events = probe.collect_events()
    assert len(events) == 1
    assert events[0].payload["kind"] == "storage_write"


async def test_correlation_uses_providers_at_flush_time():
    page = StubPage()
    page.queue_evaluate([{"kind": "storage_write", "storage": "localStorage"}])
    routes = iter(["/a", "/b"])
    indices = iter([0, 7])
    probe = StorageProbe(
        interaction_provider=lambda: next(indices),
        route_provider=lambda: next(routes),
    )
    await probe.attach(page)
    await probe.flush()
    [event] = probe.collect_events()
    assert event.route == "/a"
    assert event.interaction_index == 0


async def test_indexeddb_open_failure_records_storage_write():
    page = StubPage()
    page.queue_evaluate(
        [
            {
                "kind": "storage_write",
                "storage": "indexedDB",
                "operation": "open",
                "key": "my-db",
                "success": False,
                "error": "VersionError",
            }
        ]
    )
    probe = StorageProbe()
    await probe.attach(page)
    await probe.flush()
    [event] = probe.collect_events()
    assert event.payload["storage"] == "indexedDB"
    assert event.payload["operation"] == "open"
    assert event.payload["success"] is False


async def test_collect_events_drains_buffer():
    page = StubPage()
    page.queue_evaluate([{"kind": "storage_write", "storage": "localStorage"}])
    probe = StorageProbe()
    await probe.attach(page)
    await probe.flush()
    assert len(probe.collect_events()) == 1
    assert probe.collect_events() == []
