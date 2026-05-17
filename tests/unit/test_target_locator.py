"""Tests for ``postcheck.browser.target_locator``.

The tests use a small ``StubPage`` that mimics the Playwright ``Page`` API
surface our locator actually calls (``get_by_test_id``, ``get_by_role``,
``get_by_label``, ``get_by_text``, ``locator``) and a ``StubLocator`` with
``count()``/``first``. This lets us exercise the routing of typed
:class:`Selector` -> Playwright API on environments where Playwright cannot
launch a real browser (Python 3.14 + Playwright 1.59 has a regression that
SIGKILLs the headless shell on launch on macOS).

Selector engine semantics — i.e. *whether* a ``data-testid`` matches a given
DOM tree — are Playwright's responsibility, not ours; we only need to prove
that we pick the right API for each strategy and assemble the right return
value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from postcheck.browser.target_locator import (
    SELECTOR_PRIORITY,
    LocatedTarget,
    locate,
)
from postcheck.core.types import AffectedRoute, Selector, Symbol


# ---------------------------------------------------------------------------
# Stub Page / Locator
# ---------------------------------------------------------------------------


@dataclass
class StubLocator:
    """Mimics the subset of ``playwright.async_api.Locator`` we touch."""

    key: str  # e.g. "test_id:save"
    matches: int
    fingerprint: str | None = None  # set per-test to drive dedup grouping

    @property
    def first(self) -> StubLocator:
        return StubLocator(
            key=self.key,
            matches=min(self.matches, 1),
            fingerprint=self.fingerprint,
        )

    async def count(self) -> int:
        return self.matches

    async def evaluate(self, _js: str, *_args: Any) -> str | None:
        # The target_locator only calls evaluate() to compute a fingerprint.
        # Tests opt in by setting ``fingerprint``; otherwise return None
        # so the no-fingerprint / standalone code path is exercised
        # (preserves the v0 "every selector = its own target" behaviour
        # for stubs that don't care about dedup).
        return self.fingerprint


@dataclass
class StubPage:
    """Stub of ``playwright.async_api.Page`` covering ``get_by_*`` + ``locator``."""

    matches: dict[str, int] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    # Optional: map selector key -> fingerprint string. Tests that exercise
    # the symbol/fingerprint grouping path populate this; tests that don't
    # care leave it empty so evaluate() returns None and we fall through to
    # the "standalone" code path.
    fingerprints: dict[str, str | None] = field(default_factory=dict)

    def _make(self, key: str) -> StubLocator:
        self.calls.append(key)
        return StubLocator(
            key=key,
            matches=self.matches.get(key, 0),
            fingerprint=self.fingerprints.get(key),
        )

    def get_by_test_id(self, value: str) -> StubLocator:
        return self._make(f"test_id:{value}")

    def get_by_role(self, role: str, *, name: str | None = None) -> StubLocator:
        suffix = f":{name}" if name else ""
        return self._make(f"role:{role}{suffix}")

    def get_by_label(self, value: str) -> StubLocator:
        return self._make(f"label:{value}")

    def get_by_text(self, value: str) -> StubLocator:
        return self._make(f"text:{value}")

    def locator(self, value: str) -> StubLocator:
        return self._make(f"locator:{value}")


def _route(*selectors: Selector, symbols: list[Symbol] | None = None) -> AffectedRoute:
    return AffectedRoute(
        route="/test",
        reason="direct",
        confidence="high",
        changed_symbols=symbols or [],
        suspected_selectors=list(selectors),
    )


def _sym(name: str) -> Symbol:
    return Symbol(
        name=name, kind="function", file=Path("src/x.ts"), start_line=1, end_line=2
    )


# ---------------------------------------------------------------------------
# Strategy dispatch — every Selector strategy hits the right Playwright API.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selector,expected_call",
    [
        (Selector(strategy="test_id", value="save"), "test_id:save"),
        (Selector(strategy="role", value="button"), "role:button"),
        (Selector(strategy="role", value="button:Save changes"), "role:button:Save changes"),
        (Selector(strategy="label", value="Email"), "label:Email"),
        (Selector(strategy="text", value="Hello"), "text:Hello"),
        (Selector(strategy="css", value="button.primary"), "locator:button.primary"),
        (Selector(strategy="xpath", value="//h1"), "locator:xpath=//h1"),
        (Selector(strategy="xpath", value="xpath=//h1"), "locator:xpath=//h1"),
    ],
)
async def test_strategy_dispatch(selector, expected_call):
    page = StubPage(matches={expected_call: 1})
    res = await locate(page, _route(selector))
    assert page.calls == [expected_call]
    assert res.failures == []
    [target] = res.targets
    assert isinstance(target, LocatedTarget)
    assert target.selector is selector


# ---------------------------------------------------------------------------
# Match counting
# ---------------------------------------------------------------------------


async def test_selector_with_count_zero_does_not_match():
    page = StubPage(matches={"test_id:save": 0})
    res = await locate(page, _route(Selector(strategy="test_id", value="save")))
    assert res.targets == []
    [fail] = res.failures
    assert fail.reason == "no_selector_matched"


async def test_first_matching_selector_kept_when_some_miss():
    page = StubPage(matches={"test_id:save": 2})
    res = await locate(
        page,
        _route(
            Selector(strategy="test_id", value="ghost"),  # 0 matches
            Selector(strategy="test_id", value="save"),  # 2 matches
        ),
    )
    assert res.failures == []
    [target] = res.targets
    assert target.selector.value == "save"
    # `.first` collapses the locator to a single match.
    assert isinstance(target.locator, StubLocator)
    assert target.locator.matches == 1


async def test_multiple_selectors_each_match_emits_one_target_each():
    page = StubPage(matches={"test_id:save": 1, "test_id:cancel": 1})
    res = await locate(
        page,
        _route(
            Selector(strategy="test_id", value="save"),
            Selector(strategy="test_id", value="cancel"),
        ),
    )
    assert res.failures == []
    assert [t.selector.value for t in res.targets] == ["save", "cancel"]


# ---------------------------------------------------------------------------
# Symbol propagation
# ---------------------------------------------------------------------------


async def test_symbol_propagates_to_located_target():
    sym = _sym("handleSave")
    page = StubPage(matches={"test_id:save": 1})
    res = await locate(
        page,
        _route(Selector(strategy="test_id", value="save"), symbols=[sym]),
    )
    [target] = res.targets
    assert target.symbol is sym


async def test_symbol_is_none_when_route_has_no_changed_symbols():
    page = StubPage(matches={"test_id:save": 1})
    res = await locate(page, _route(Selector(strategy="test_id", value="save")))
    [target] = res.targets
    assert target.symbol is None


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


async def test_no_selectors_provided_emits_failure():
    sym = _sym("orphanHandler")
    page = StubPage()
    res = await locate(page, _route(symbols=[sym]))

    assert res.targets == []
    assert page.calls == []
    [fail] = res.failures
    assert fail.reason == "no_selectors_provided"
    assert fail.changed_symbols == [sym]
    assert fail.attempted == []
    assert "broaden" in fail.detail


async def test_no_selector_matched_emits_failure_with_attempts():
    sym = _sym("h")
    attempts = [
        Selector(strategy="test_id", value="ghost"),
        Selector(strategy="css", value="button.does-not-exist"),
    ]
    page = StubPage()  # no matches configured -> 0 for everything
    res = await locate(page, _route(*attempts, symbols=[sym]))

    assert res.targets == []
    [fail] = res.failures
    assert fail.reason == "no_selector_matched"
    assert [s.value for s in fail.attempted] == [
        "ghost",
        "button.does-not-exist",
    ]
    assert fail.changed_symbols == [sym]
    assert "2 selector" in fail.detail


async def test_locator_count_exception_is_treated_as_no_match():
    """A bad selector string raising on ``count()`` should be skipped, not crash."""

    class ExplodingLocator(StubLocator):
        async def count(self) -> int:  # type: ignore[override]
            raise RuntimeError("invalid selector")

    class ExplodingPage(StubPage):
        def locator(self, value: str) -> Any:
            self.calls.append(f"locator:{value}")
            return ExplodingLocator(key=value, matches=0)

    page = ExplodingPage(matches={"test_id:save": 1})
    res = await locate(
        page,
        _route(
            Selector(strategy="css", value="!!bogus!!"),
            Selector(strategy="test_id", value="save"),
        ),
    )
    # The bogus CSS selector is skipped silently; the test_id then matches.
    assert res.failures == []
    [target] = res.targets
    assert target.selector.strategy == "test_id"


# ---------------------------------------------------------------------------
# Dedup: multiple selectors per symbol = one target
# ---------------------------------------------------------------------------


async def test_multiple_selectors_for_one_symbol_returns_one_target():
    """Adapter tagged 3 selectors with the same ``symbol_name``: 1 target.

    Regression: the React/Vite adapter emits both a ``test_id`` and a
    ``text`` selector per JSX handler; before this change the runner
    treated them as separate targets and clicked each button twice.
    """
    page = StubPage(
        matches={
            "test_id:save": 1,
            "role:button:Save": 1,
            "text:Save": 1,
        },
    )
    res = await locate(
        page,
        _route(
            Selector(strategy="text", value="Save", symbol_name="handleSave"),
            Selector(strategy="role", value="button:Save", symbol_name="handleSave"),
            Selector(strategy="test_id", value="save", symbol_name="handleSave"),
        ),
    )
    assert res.failures == []
    assert len(res.targets) == 1
    [target] = res.targets
    # Highest-priority strategy wins regardless of input order.
    assert target.selector.strategy == "test_id"
    # All three fallback strategies are recorded for provenance.
    assert set(target.resolved_via) == {"test_id", "role", "text"}
    assert target.ambiguity is None


async def test_selector_priority_order_respected():
    """Within a symbol group, the SELECTOR_PRIORITY-highest strategy wins."""
    # Pair every priority level against every lower-priority level and
    # confirm the higher one is selected. This locks in the constant.
    for high_idx, high in enumerate(SELECTOR_PRIORITY):
        for low in SELECTOR_PRIORITY[high_idx + 1 :]:
            page = StubPage(
                matches={
                    f"{high}:x": 1,
                    f"{low}:x": 1,
                    # role: needs the role-prefix dispatch path; using
                    # value 'x' is fine because StubPage._make stringifies
                    # whatever it got.
                },
            )
            # locator() strategy maps to "locator:x" not "css:x" — build
            # the right StubPage key per strategy.
            def _key(strat: str, val: str) -> str:
                if strat in ("css",):
                    return f"locator:{val}"
                if strat == "xpath":
                    return f"locator:xpath={val}"
                return f"{strat}:{val}"

            page = StubPage(
                matches={_key(high, "x"): 1, _key(low, "x"): 1},
            )
            res = await locate(
                page,
                _route(
                    Selector(strategy=low, value="x", symbol_name="h"),
                    Selector(strategy=high, value="x", symbol_name="h"),
                ),
            )
            assert res.failures == []
            assert len(res.targets) == 1, (high, low)
            assert res.targets[0].selector.strategy == high, (high, low)


async def test_selector_ambiguity_flagged_when_symbol_group_spans_elements():
    """If two same-symbol selectors resolve to *different* elements,
    the higher-priority one wins but ``ambiguity`` is populated so the
    adapter bug surfaces in the report.
    """
    page = StubPage(
        matches={"test_id:save": 1, "text:Save": 1},
        fingerprints={
            "test_id:save": "elem-A",
            "text:Save": "elem-B",  # different element!
        },
    )
    res = await locate(
        page,
        _route(
            Selector(strategy="text", value="Save", symbol_name="handleSave"),
            Selector(strategy="test_id", value="save", symbol_name="handleSave"),
        ),
    )
    assert res.failures == []
    assert len(res.targets) == 1
    [target] = res.targets
    assert target.selector.strategy == "test_id"  # higher priority wins
    assert target.ambiguity is not None
    assert target.ambiguity["symbol_name"] == "handleSave"
    assert (
        target.ambiguity["reason"]
        == "selectors_for_symbol_resolve_to_different_elements"
    )
    assert set(target.ambiguity["strategies"]) == {"test_id", "text"}
    assert sorted(target.ambiguity["fingerprints"]) == ["elem-A", "elem-B"]
