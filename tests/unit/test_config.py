"""Tests for ``postcheck.core.config``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from postcheck.core.config import (
    DEFAULT_EXCLUDE_GLOBS,
    DEFAULT_NETWORK_IGNORE_PATTERNS,
    DEFAULT_RESOURCE_TYPES,
    REDACTION_PATTERNS,
    Settings,
    load_settings,
)
from postcheck.core.errors import ConfigError

_POSTCHECK_ENV_VARS = (
    "POSTCHECK_ADAPTER",
    "POSTCHECK_BASE_URL",
    "POSTCHECK_CHROME_DEBUG_PORT",
    "POSTCHECK_CHROME_PROFILE_DIR",
    "POSTCHECK_TIMEOUT_MS",
    "POSTCHECK_EXCLUDE_GLOBS",
    "POSTCHECK_NETWORK__RESOURCE_TYPES",
    "POSTCHECK_NETWORK__IGNORE_PATTERNS",
    "POSTCHECK_NETWORK__FOCUS_PATTERNS",
    "POSTCHECK_NETWORK__TREAT_CROSS_ORIGIN_AS",
    "POSTCHECK_DATABASE_URL",
    "POSTCHECK_REDIS_URL",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _POSTCHECK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


def test_defaults_load_when_no_file_or_env(tmp_path):
    s = load_settings(tmp_path)
    assert s.adapter == "auto"
    assert s.base_url == "http://localhost:3000"
    assert s.chrome_debug_port == 9222
    assert s.timeout_ms == 30_000
    assert s.exclude_globs == list(DEFAULT_EXCLUDE_GLOBS)
    assert s.network.resource_types == list(DEFAULT_RESOURCE_TYPES)
    assert s.network.ignore_patterns == list(DEFAULT_NETWORK_IGNORE_PATTERNS)
    assert s.network.treat_cross_origin_as == "medium"
    assert s.database_url is None
    assert s.redis_url is None
    assert s.anthropic_api_key is None


def test_default_exclude_globs_contain_expected_entries():
    globs = set(DEFAULT_EXCLUDE_GLOBS)
    assert "**/package-lock.json" in globs
    assert "**/yarn.lock" in globs
    assert "**/pnpm-lock.yaml" in globs
    assert "**/node_modules/**" in globs
    assert "**/dist/**" in globs
    assert "**/build/**" in globs
    assert "**/.next/**" in globs
    assert "**/generated/**" in globs


def test_default_network_ignores_hmr_and_telemetry():
    joined = "\n".join(DEFAULT_NETWORK_IGNORE_PATTERNS)
    assert "@vite/client" in joined
    assert "google-analytics" in joined
    assert "segment" in joined
    assert "sentry" in joined


def test_redaction_patterns_cover_common_secret_shapes():
    import re

    samples = {
        "Authorization: Bearer abcdef1234567890ABCDEF": True,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signaturepart": True,
        "api_key='abcdef0123456789'": True,
        "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaa": True,
        "AKIAIOSFODNN7EXAMPLE": True,
        "ghp_aaaaaaaaaaaaaaaaaaaaaaaa": True,
        "sk_live_abcdefghijklmnop1234": True,
        "just a normal sentence with no secrets": False,
    }
    for sample, should_match in samples.items():
        hits = any(re.search(p, sample) for p in REDACTION_PATTERNS)
        assert hits is should_match, sample


def _write_config(root: Path, payload: dict) -> Path:
    cfg_dir = root / ".postcheck"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_json_file_overrides_defaults(tmp_path):
    # chrome_debug_port is GLOBAL-only and is silently dropped from a project file
    # (with a structlog warning). Project-level fields below still apply.
    _write_config(
        tmp_path,
        {
            "adapter": "react_vite",
            "base_url": "http://localhost:5173",
            "timeout_ms": 12000,
            "exclude_globs": ["**/custom/**"],
            "network": {
                "ignore_patterns": ["custom-ignore"],
                "treat_cross_origin_as": "low",
            },
        },
    )
    s = load_settings(tmp_path)
    assert s.adapter == "react_vite"
    assert s.base_url == "http://localhost:5173"
    assert s.timeout_ms == 12000
    assert s.exclude_globs == ["**/custom/**"]
    assert s.network.ignore_patterns == ["custom-ignore"]
    assert s.network.treat_cross_origin_as == "low"
    # Unspecified network field falls back to default
    assert s.network.resource_types == list(DEFAULT_RESOURCE_TYPES)


def test_env_vars_override_json(tmp_path, monkeypatch):
    # chrome_debug_port is global-only at the file layer but still settable via env.
    _write_config(
        tmp_path,
        {"adapter": "react_vite", "timeout_ms": 12000},
    )
    monkeypatch.setenv("POSTCHECK_ADAPTER", "plain_html")
    monkeypatch.setenv("POSTCHECK_CHROME_DEBUG_PORT", "9444")
    monkeypatch.setenv("POSTCHECK_NETWORK__TREAT_CROSS_ORIGIN_AS", "high")

    s = load_settings(tmp_path)
    assert s.adapter == "plain_html"
    assert s.chrome_debug_port == 9444
    # JSON value still wins over default for fields not in env
    assert s.timeout_ms == 12000
    assert s.network.treat_cross_origin_as == "high"


def test_secrets_loaded_from_env_only(tmp_path, monkeypatch):
    # JSON-supplied secrets should NOT be honoured (env-only); we just verify env works.
    monkeypatch.setenv("POSTCHECK_DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("POSTCHECK_REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    s = load_settings(tmp_path)
    assert s.database_url == "postgresql+asyncpg://u:p@h/db"
    assert s.redis_url == "redis://h:6379/0"
    assert s.anthropic_api_key == "sk-ant-test"


def test_invalid_adapter_raises_config_error(tmp_path):
    _write_config(tmp_path, {"adapter": "svelte"})
    with pytest.raises(ConfigError) as exc:
        load_settings(tmp_path)
    assert "adapter" in str(exc.value).lower()
    assert exc.value.code == "config_error"
    assert "errors" in exc.value.context


def test_invalid_port_raises_config_error(tmp_path, monkeypatch):
    # chrome_debug_port is global-only; project file gets dropped. Use env to
    # exercise the validation path.
    monkeypatch.setenv("POSTCHECK_CHROME_DEBUG_PORT", "99999")
    with pytest.raises(ConfigError) as exc:
        load_settings(tmp_path)
    msg = str(exc.value).lower()
    assert "chrome_debug_port" in msg or "65535" in msg


def test_negative_timeout_raises_config_error(tmp_path):
    _write_config(tmp_path, {"timeout_ms": -1})
    with pytest.raises(ConfigError):
        load_settings(tmp_path)


def test_invalid_cross_origin_policy_raises_config_error(tmp_path):
    _write_config(tmp_path, {"network": {"treat_cross_origin_as": "yolo"}})
    with pytest.raises(ConfigError) as exc:
        load_settings(tmp_path)
    assert "treat_cross_origin_as" in str(exc.value).lower()


def test_unknown_top_level_field_in_file_is_warned_and_skipped(tmp_path):
    # Forward-compatibility: unknown keys in persisted config files are warned
    # and ignored, not fatal. (See CLAUDE.md, Configuration model.)
    _write_config(tmp_path, {"mystery": 1})
    s = load_settings(tmp_path)
    assert s.adapter == "auto"


def test_malformed_json_raises_config_error(tmp_path):
    cfg_dir = tmp_path / ".postcheck"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_settings(tmp_path)
    assert "Invalid JSON" in str(exc.value)
    assert exc.value.context["path"].endswith("config.json")


def test_non_object_json_raises_config_error(tmp_path):
    cfg_dir = tmp_path / ".postcheck"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_settings(tmp_path)
    assert "JSON object" in str(exc.value)


def test_init_overrides_beat_env(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTCHECK_ADAPTER", "plain_html")
    s = load_settings(tmp_path, adapter="react_vite")
    assert s.adapter == "react_vite"


def test_settings_class_defaults_when_no_project_root(monkeypatch, tmp_path):
    # Run with cwd set to an empty dir so no .postcheck/config.json is found.
    monkeypatch.chdir(tmp_path)
    s = load_settings()
    assert isinstance(s, Settings)
    assert s.adapter == "auto"
