"""Tests for the layered config loader in ``postcheck.core.config``."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from postcheck.core.config import (
    ConfigLayer,
    get_global_config_path,
    load_config,
    load_config_with_provenance,
)

_POSTCHECK_ENV_VARS = (
    "POSTCHECK_ADAPTER",
    "POSTCHECK_BASE_URL",
    "POSTCHECK_CHROME_DEBUG_PORT",
    "POSTCHECK_CHROME_PROFILE_DIR",
    "POSTCHECK_TIMEOUT_MS",
    "POSTCHECK_EXCLUDE_GLOBS",
    "POSTCHECK_LAUNCH_MODE",
    "POSTCHECK_LAUNCH_HEADLESS",
    "POSTCHECK_NETWORK__RESOURCE_TYPES",
    "POSTCHECK_NETWORK__IGNORE_PATTERNS",
    "POSTCHECK_NETWORK__FOCUS_PATTERNS",
    "POSTCHECK_NETWORK__TREAT_CROSS_ORIGIN_AS",
    "POSTCHECK_DATABASE_URL",
    "POSTCHECK_REDIS_URL",
    "ANTHROPIC_API_KEY",
    "XDG_CONFIG_HOME",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _POSTCHECK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _write_global(tmp_path: Path, payload: dict) -> Path:
    g = tmp_path / "globalcfg" / "postcheck" / "config.json"
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text(json.dumps(payload), encoding="utf-8")
    return g


def _write_project(project: Path, payload: dict) -> Path:
    d = project / ".postcheck"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "config.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_layer_merge_order_correct(tmp_path, monkeypatch):
    # GLOBAL sets timeout_ms; PROJECT overrides; ENV trumps both; FLAG trumps env.
    g_path = _write_global(tmp_path, {"timeout_ms": 1000})
    project = tmp_path / "proj"
    project.mkdir()
    _write_project(project, {"timeout_ms": 2000})
    monkeypatch.setenv("POSTCHECK_TIMEOUT_MS", "3000")

    s = load_config(project, cli_overrides={"timeout_ms": 4000}, global_path=g_path)
    assert s.timeout_ms == 4000

    s = load_config(project, global_path=g_path)
    assert s.timeout_ms == 3000  # env wins over project

    monkeypatch.delenv("POSTCHECK_TIMEOUT_MS")
    s = load_config(project, global_path=g_path)
    assert s.timeout_ms == 2000  # project wins over global

    (project / ".postcheck" / "config.json").unlink()
    s = load_config(project, global_path=g_path)
    assert s.timeout_ms == 1000  # global wins over default


def test_provenance_tracking_returns_right_layer(tmp_path, monkeypatch):
    g_path = _write_global(tmp_path, {"timeout_ms": 1000, "launch_mode": "launch"})
    project = tmp_path / "proj"
    project.mkdir()
    _write_project(project, {"base_url": "http://localhost:5173"})
    monkeypatch.setenv("POSTCHECK_NETWORK__TREAT_CROSS_ORIGIN_AS", "high")

    settings, prov = load_config_with_provenance(
        project, cli_overrides={"timeout_ms": 9999}, global_path=g_path
    )
    assert settings.timeout_ms == 9999
    assert prov["timeout_ms"] is ConfigLayer.FLAG
    assert prov["launch_mode"] is ConfigLayer.GLOBAL
    assert prov["base_url"] is ConfigLayer.PROJECT
    assert prov["network.treat_cross_origin_as"] is ConfigLayer.ENV
    assert prov["chrome_debug_port"] is ConfigLayer.DEFAULT


def test_global_only_field_in_project_file_warns_and_skips(tmp_path, caplog):
    project = tmp_path / "proj"
    project.mkdir()
    _write_project(project, {"launch_mode": "launch", "base_url": "http://x"})

    # No global file → global_path points elsewhere.
    g_path = tmp_path / "nope.json"
    s = load_config(project, global_path=g_path)
    assert s.launch_mode == "attach"  # default — project value dropped
    assert s.base_url == "http://x"  # project-allowed, kept


def test_project_only_field_in_global_file_warns_and_skips(tmp_path):
    g_path = _write_global(tmp_path, {"base_url": "http://from-global"})
    s = load_config(project_root=None, global_path=g_path)
    # base_url is PROJECT-only → dropped from global file.
    assert s.base_url == "http://localhost:3000"


def test_xdg_config_home_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    expected = tmp_path / "xdg" / "postcheck" / "config.json"
    assert get_global_config_path() == expected


def test_xdg_config_home_default_path(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    import sys

    if sys.platform != "win32":
        expected = Path.home() / ".config" / "postcheck" / "config.json"
        assert get_global_config_path() == expected


def test_loader_handles_missing_global_file_gracefully(tmp_path):
    g_path = tmp_path / "definitely_not_there.json"
    assert not g_path.exists()
    s = load_config(project_root=None, global_path=g_path)
    assert s.adapter == "auto"


def test_loader_handles_missing_project_dir_gracefully(tmp_path):
    project = tmp_path / "no_postcheck_dir"
    project.mkdir()
    s = load_config(project, global_path=tmp_path / "nope.json")
    assert s.adapter == "auto"


def test_either_layer_field_global_then_project(tmp_path):
    """An EITHER field set in global is overridden by the project file."""
    g_path = _write_global(tmp_path, {"launch_headless": False})
    project = tmp_path / "proj"
    project.mkdir()
    _write_project(project, {"launch_headless": True})

    s, prov = load_config_with_provenance(project, global_path=g_path)
    assert s.launch_headless is True
    assert prov["launch_headless"] is ConfigLayer.PROJECT
