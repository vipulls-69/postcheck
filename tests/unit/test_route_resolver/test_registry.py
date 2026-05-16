"""Tests for the route adapter registry."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from postcheck.analysis.route_resolver.adapters.plain_html import PlainHtmlAdapter
from postcheck.analysis.route_resolver.adapters.react_vite import ReactViteAdapter
from postcheck.analysis.route_resolver.registry import detect_adapter
from postcheck.core.errors import AdapterDetectionError

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


async def test_detects_react_vite_for_example_app():
    adapter = await detect_adapter(EXAMPLES / "react_vite_app")
    assert isinstance(adapter, ReactViteAdapter)


async def test_detects_plain_html_for_static_site():
    adapter = await detect_adapter(EXAMPLES / "plain_html_site")
    assert isinstance(adapter, PlainHtmlAdapter)


async def test_explicit_override_skips_detection(tmp_path):
    # tmp_path has no html and no vite — auto would fail, but override wins.
    adapter = await detect_adapter(tmp_path, override="plain_html")
    assert isinstance(adapter, PlainHtmlAdapter)


async def test_unknown_override_raises(tmp_path):
    with pytest.raises(AdapterDetectionError) as exc:
        await detect_adapter(tmp_path, override="nonsense")
    assert "nonsense" in str(exc.value)


async def test_no_match_raises(tmp_path):
    with pytest.raises(AdapterDetectionError):
        await detect_adapter(tmp_path)


async def test_react_takes_precedence_over_plain_html(tmp_path):
    # A vite project that also has a stray .html somewhere should still detect
    # as react_vite first.
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18"}}), encoding="utf-8"
    )
    (tmp_path / "vite.config.ts").write_text("export default {}\n", encoding="utf-8")
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    adapter = await detect_adapter(tmp_path)
    assert isinstance(adapter, ReactViteAdapter)
