"""Pydantic Settings + four-layer config loader for postcheck (v0).

Configuration is merged from five layers (highest precedence last):

    DEFAULT  → built-in defaults in this module
    GLOBAL   → ~/.config/postcheck/config.json (or %APPDATA%\\postcheck on Windows)
    PROJECT  → <project>/.postcheck/config.json
    ENV      → POSTCHECK_* environment variables (nested via __)
    FLAG     → cli_overrides dict passed by the CLI at runtime

Each field declares which layers it accepts via ``Field(json_schema_extra={"layers": [...]})``.
If a config file at one layer contains a field that isn't permitted there
(e.g. ``base_url`` in the global file), the value is dropped with a
``structlog`` warning — files persist across versions and we want forward
compatibility, not crashes.

Secrets — database URL, Redis URL, Anthropic API key — are env-only.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import structlog
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError

_log = structlog.get_logger(__name__)

AdapterChoice = Literal["auto", "plain_html", "react_vite"]
CrossOriginPolicy = Literal["high", "medium", "low", "ignore"]
ColorMode = Literal["auto", "always", "never"]


class ConfigLayer(str, Enum):
    """Layers in the config-resolution stack, lowest → highest precedence."""

    DEFAULT = "default"
    GLOBAL = "global"
    PROJECT = "project"
    ENV = "env"
    FLAG = "flag"


# Default layer policy: settable everywhere except DEFAULT (which is implicit).
_DEFAULT_LAYERS: tuple[ConfigLayer, ...] = (
    ConfigLayer.GLOBAL,
    ConfigLayer.PROJECT,
    ConfigLayer.ENV,
    ConfigLayer.FLAG,
)


def _layers(*items: ConfigLayer) -> list[str]:
    """Helper for ``json_schema_extra``: serialise enum values to strings."""
    return [item.value for item in items]


# Convenience layer tuples for field declarations
_GLOBAL_ONLY = (ConfigLayer.GLOBAL, ConfigLayer.ENV, ConfigLayer.FLAG)
_PROJECT_ONLY = (ConfigLayer.PROJECT, ConfigLayer.ENV, ConfigLayer.FLAG)
_EITHER = (ConfigLayer.GLOBAL, ConfigLayer.PROJECT, ConfigLayer.ENV, ConfigLayer.FLAG)
_ENV_ONLY = (ConfigLayer.ENV, ConfigLayer.FLAG)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    # lockfiles
    "**/package-lock.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
    "**/poetry.lock",
    "**/Pipfile.lock",
    "**/uv.lock",
    # generated / build outputs
    "**/generated/**",
    "**/__generated__/**",
    "**/dist/**",
    "**/build/**",
    "**/.next/**",
    "**/.turbo/**",
    "**/.svelte-kit/**",
    "**/out/**",
    # vendored
    "**/node_modules/**",
    "**/vendor/**",
)

DEFAULT_RESOURCE_TYPES: tuple[str, ...] = (
    "xhr",
    "fetch",
    "websocket",
    "eventsource",
    "document",
)

DEFAULT_NETWORK_IGNORE_PATTERNS: tuple[str, ...] = (
    # Vite / webpack HMR
    r".*/@vite/client.*",
    r".*/@react-refresh.*",
    r".*/__vite_ping.*",
    r".*\?t=\d+$",
    r".*/sockjs-node/.*",
    r".*/_next/webpack-hmr.*",
    # common telemetry
    r".*\.google-analytics\.com/.*",
    r".*\.googletagmanager\.com/.*",
    r".*\.segment\.(io|com)/.*",
    r".*\.mixpanel\.com/.*",
    r".*\.amplitude\.com/.*",
    r".*\.hotjar\.com/.*",
    r".*\.sentry\.io/.*",
    r".*\.datadoghq\.com/.*",
    r".*\.fullstory\.com/.*",
    r".*\.intercom\.io/.*",
)

DEFAULT_NETWORK_FOCUS_PATTERNS: tuple[str, ...] = ()

# Patterns whose match in any logged string indicates a likely secret.
# Used by structured-logging redactors (see CLAUDE.md "Secret handling").
REDACTION_PATTERNS: tuple[str, ...] = (
    # Authorization: Bearer <token>
    r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{16,}",
    # JSON Web Tokens (header.payload.signature)
    r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b",
    # Generic api_key / api-key / apikey assignments
    r"(?i)(api[_\-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9._\-+/=]{12,}",
    # Anthropic
    r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b",
    # OpenAI
    r"\bsk-[A-Za-z0-9]{20,}\b",
    # AWS access key id
    r"\bAKIA[0-9A-Z]{16}\b",
    # GitHub PATs
    r"\bghp_[A-Za-z0-9]{20,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    # Stripe
    r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b",
    # Slack
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
)


# ---------------------------------------------------------------------------
# Nested config models
# ---------------------------------------------------------------------------


class NetworkSettings(BaseModel):
    """Three-layer filter for the network probe (v0)."""

    model_config = {"extra": "forbid"}

    resource_types: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RESOURCE_TYPES),
        json_schema_extra={"layers": _layers(*_EITHER)},
    )
    ignore_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_NETWORK_IGNORE_PATTERNS),
        json_schema_extra={"layers": _layers(*_EITHER)},
    )
    focus_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_NETWORK_FOCUS_PATTERNS),
        json_schema_extra={"layers": _layers(*_EITHER)},
    )
    treat_cross_origin_as: CrossOriginPolicy = Field(
        default="medium",
        json_schema_extra={"layers": _layers(*_EITHER)},
    )


class ScenarioSettings(BaseModel):
    """Tunables for the per-route scenario runner.

    ``recovery_mode`` controls whether the runner ``page.reload()``’s
    between interactions so a hard error in one click doesn't poison the
    subsequent ones (e.g. a React render error after click N leaves the
    tree broken — clicks N+1 then time out).

    * ``on_failure`` (v0 default) — reload only when the previous
      interaction either raised or surfaced a hard runtime event
      (``runtime_error`` / ``page_crash``).
    * ``always`` — reload before every interaction; useful when
      interactions are known to be deeply stateful but expensive
      (roughly doubles per-route wall time).
    * ``never`` — legacy v0 behaviour, retained so tests that assert on
      exact event sequences don't have to model recovery events.
    """

    model_config = {"extra": "forbid"}

    recovery_mode: Literal["always", "on_failure", "never"] = Field(
        default="on_failure",
        json_schema_extra={"layers": _layers(*_EITHER)},
    )
    # End-of-scenario grace window for in-flight network requests. Real
    # user-controlled aborts (``setTimeout(() => ctrl.abort(), 2000)``,
    # ``AbortSignal.timeout(N)``, slow server replies that arrive after
    # the click-then-settle cycle) need a window to surface as Chromium
    # ``requestfailed`` events before the probe detaches. After this
    # budget elapses, anything still pending becomes a synthetic
    # ``network_unresolved_at_detach`` event (heuristic, not an abort).
    detach_grace_ms: int = Field(
        default=3_000,
        ge=0,
        json_schema_extra={"layers": _layers(*_EITHER)},
    )
    # How often to poll probes for "idle" during the grace window.
    detach_poll_ms: int = Field(
        default=100,
        ge=10,
        json_schema_extra={"layers": _layers(*_EITHER)},
    )


# ---------------------------------------------------------------------------
# Top-level Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Postcheck runtime configuration (v0)."""

    model_config = SettingsConfigDict(
        env_prefix="POSTCHECK_",
        env_nested_delimiter="__",
        extra="forbid",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Project-only ------------------------------------------------------
    adapter: AdapterChoice = Field(
        default="auto",
        json_schema_extra={"layers": _layers(*_PROJECT_ONLY)},
    )
    base_url: str = Field(
        default="http://localhost:3000",
        json_schema_extra={"layers": _layers(*_PROJECT_ONLY)},
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS),
        json_schema_extra={"layers": _layers(*_PROJECT_ONLY)},
    )

    # --- Global-only -------------------------------------------------------
    launch_mode: Literal["attach", "launch"] = Field(
        default="attach",
        json_schema_extra={"layers": _layers(*_GLOBAL_ONLY)},
    )
    chrome_debug_port: int = Field(
        default=9222, ge=1, le=65535,
        json_schema_extra={"layers": _layers(*_GLOBAL_ONLY)},
    )
    chrome_profile_dir: str = Field(
        default="$HOME/.postcheck-chrome-profile",
        json_schema_extra={"layers": _layers(*_GLOBAL_ONLY)},
    )
    default_adapter_override: AdapterChoice | None = Field(
        default=None,
        json_schema_extra={"layers": _layers(*_GLOBAL_ONLY)},
    )
    color_mode: ColorMode = Field(
        default="auto",
        json_schema_extra={"layers": _layers(*_GLOBAL_ONLY)},
    )
    verbose_default: bool = Field(
        default=False,
        json_schema_extra={"layers": _layers(*_GLOBAL_ONLY)},
    )

    # --- Either ------------------------------------------------------------
    launch_headless: bool = Field(
        default=True,
        json_schema_extra={"layers": _layers(*_EITHER)},
    )
    launch_args: list[str] = Field(
        default_factory=list,
        json_schema_extra={"layers": _layers(*_EITHER)},
    )
    timeout_ms: int = Field(
        default=30_000, ge=0,
        json_schema_extra={"layers": _layers(*_EITHER)},
    )
    network: NetworkSettings = Field(
        default_factory=NetworkSettings,
        json_schema_extra={"layers": _layers(*_EITHER)},
    )
    scenario: ScenarioSettings = Field(
        default_factory=ScenarioSettings,
        json_schema_extra={"layers": _layers(*_EITHER)},
    )

    # --- Env-only (secrets) ------------------------------------------------
    database_url: str | None = Field(
        default=None,
        json_schema_extra={"layers": _layers(*_ENV_ONLY)},
    )
    redis_url: str | None = Field(
        default=None,
        json_schema_extra={"layers": _layers(*_ENV_ONLY)},
    )
    anthropic_api_key: str | None = Field(
        default=None, alias="ANTHROPIC_API_KEY",
        json_schema_extra={"layers": _layers(*_ENV_ONLY)},
    )


# ---------------------------------------------------------------------------
# Layer metadata helpers
# ---------------------------------------------------------------------------


def _field_layers(model: type[BaseModel], name: str) -> tuple[ConfigLayer, ...]:
    """Allowed layers for ``model.<name>``. Falls back to default policy."""
    field = model.model_fields.get(name)
    if field is None:
        return _DEFAULT_LAYERS
    extra = field.json_schema_extra
    if isinstance(extra, Mapping) and "layers" in extra:
        return tuple(ConfigLayer(v) for v in extra["layers"])
    return _DEFAULT_LAYERS


def allowed_layers_for(key: str) -> tuple[ConfigLayer, ...]:
    """Return permitted layers for a top-level or dotted nested key.

    Unknown keys raise :class:`KeyError`.
    """
    parts = key.split(".")
    model: type[BaseModel] = Settings
    for i, part in enumerate(parts):
        if part not in model.model_fields:
            raise KeyError(key)
        if i == len(parts) - 1:
            return _field_layers(model, part)
        annotation = model.model_fields[part].annotation
        nested = _unwrap_model(annotation)
        if nested is None:
            raise KeyError(key)
        model = nested
    raise KeyError(key)


def _unwrap_model(annotation: Any) -> type[BaseModel] | None:
    """If ``annotation`` is (or contains) a BaseModel subclass, return it."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        nested = _unwrap_model(arg)
        if nested is not None:
            return nested
    return None


def all_keys() -> list[str]:
    """Return every settable dotted key (top-level + nested model fields)."""
    out: list[str] = []
    for name, field in Settings.model_fields.items():
        nested = _unwrap_model(field.annotation)
        if nested is not None:
            for sub in nested.model_fields:
                out.append(f"{name}.{sub}")
        else:
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# Global config path resolution
# ---------------------------------------------------------------------------


def get_global_config_path() -> Path:
    """Resolve the global config file path per OS conventions.

    - Windows: ``%APPDATA%\\postcheck\\config.json``
      (falls back to ``~/AppData/Roaming`` if APPDATA is unset).
    - Other platforms (Linux/macOS): ``$XDG_CONFIG_HOME/postcheck/config.json``
      or ``~/.config/postcheck/config.json``.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "postcheck" / "config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "postcheck" / "config.json"


def get_project_config_path(project_root: Path) -> Path:
    """Path to ``<project_root>/.postcheck/config.json``."""
    return Path(project_root) / ".postcheck" / "config.json"


# ---------------------------------------------------------------------------
# File / env readers
# ---------------------------------------------------------------------------


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw) if raw.strip() else {}
    except OSError as exc:
        raise ConfigError(
            f"Could not read {path}: {exc}",
            context={"path": str(path)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON in {path}: {exc.msg} (line {exc.lineno}, col {exc.colno})",
            context={"path": str(path)},
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"{path} must contain a JSON object at the top level",
            context={"path": str(path), "got": type(parsed).__name__},
        )
    return parsed


def _coerce_env_value(value: str) -> Any:
    """Heuristic env-value coercion.

    JSON-shaped values get parsed (`[...]`, `{...}`, true/false/null/numbers).
    Otherwise the raw string is returned for pydantic to coerce.
    """
    stripped = value.strip()
    if not stripped:
        return value
    if stripped[0] in "[{" or stripped in ("true", "false", "null"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


def _set_nested(out: dict[str, Any], parts: list[str], value: Any) -> None:
    cur = out
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _read_env_vars() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in os.environ.items():
        if k == "ANTHROPIC_API_KEY":
            out["anthropic_api_key"] = v
            continue
        if not k.startswith("POSTCHECK_"):
            continue
        tail = k[len("POSTCHECK_"):]
        if not tail:
            continue
        parts = [seg.lower() for seg in tail.split("__")]
        _set_nested(out, parts, _coerce_env_value(v))
    return out


# ---------------------------------------------------------------------------
# Layer validation + merging
# ---------------------------------------------------------------------------


def _filter_by_layer(
    data: Mapping[str, Any],
    layer: ConfigLayer,
    *,
    source: str,
    model: type[BaseModel] = Settings,
    prefix: str = "",
) -> dict[str, Any]:
    """Drop keys not permitted at ``layer``; emit structlog warnings."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        full = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if key not in model.model_fields:
            _log.warning(
                "config.unknown_field",
                source=source,
                key=full,
                layer=layer.value,
            )
            continue
        allowed = _field_layers(model, key)
        if layer not in allowed:
            _log.warning(
                "config.field_not_allowed_at_layer",
                source=source,
                key=full,
                layer=layer.value,
                allowed=[a.value for a in allowed],
            )
            continue
        nested = _unwrap_model(model.model_fields[key].annotation)
        if nested is not None and isinstance(value, Mapping):
            out[key] = _filter_by_layer(
                value, layer, source=source, model=nested, prefix=full
            )
        else:
            out[key] = value
    return out


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Right-biased recursive merge. Returns a new dict."""
    out = dict(base)
    for key, value in overlay.items():
        if (
            isinstance(value, Mapping)
            and isinstance(out.get(key), Mapping)
        ):
            out[key] = _deep_merge(dict(out[key]), value)
        else:
            out[key] = value
    return out


def _flatten(
    data: Mapping[str, Any], prefix: str = ""
) -> Iterable[tuple[str, Any]]:
    for k, v in data.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, Mapping):
            yield from _flatten(v, full)
        else:
            yield full, v


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def _settings_defaults_dict() -> dict[str, Any]:
    """Construct Settings with no env/file influence and dump it."""
    # Bypass env reading by passing _env_file=None and explicit init kwargs is
    # not enough; instead build by hand from each field's default.
    out: dict[str, Any] = {}
    for name, field in Settings.model_fields.items():
        default = field.get_default(call_default_factory=True)
        if isinstance(default, BaseModel):
            out[name] = default.model_dump()
        else:
            out[name] = default
    return out


def load_config_with_provenance(
    project_root: Path | str | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    *,
    global_path: Path | None = None,
) -> tuple[Settings, dict[str, ConfigLayer]]:
    """Load Settings by merging all configured layers; return provenance too.

    The provenance map keys are dotted field paths (e.g. ``network.timeout_ms``)
    pointing to the :class:`ConfigLayer` that supplied the effective value.
    """
    defaults = _settings_defaults_dict()
    provenance: dict[str, ConfigLayer] = {
        key: ConfigLayer.DEFAULT for key, _ in _flatten(defaults)
    }

    merged: dict[str, Any] = dict(defaults)

    layer_inputs: list[tuple[ConfigLayer, dict[str, Any], str]] = []

    g_path = global_path if global_path is not None else get_global_config_path()
    if g_path.is_file():
        raw = _read_json_file(g_path)
        filtered = _filter_by_layer(raw, ConfigLayer.GLOBAL, source=str(g_path))
        layer_inputs.append((ConfigLayer.GLOBAL, filtered, str(g_path)))

    if project_root is not None:
        p_path = get_project_config_path(Path(project_root))
        if p_path.is_file():
            raw = _read_json_file(p_path)
            filtered = _filter_by_layer(raw, ConfigLayer.PROJECT, source=str(p_path))
            layer_inputs.append((ConfigLayer.PROJECT, filtered, str(p_path)))

    env_raw = _read_env_vars()
    if env_raw:
        env_filtered = _filter_by_layer(env_raw, ConfigLayer.ENV, source="env")
        layer_inputs.append((ConfigLayer.ENV, env_filtered, "env"))

    if cli_overrides:
        flag_filtered = _filter_by_layer(
            dict(cli_overrides), ConfigLayer.FLAG, source="flag"
        )
        layer_inputs.append((ConfigLayer.FLAG, flag_filtered, "flag"))

    for layer, payload, _src in layer_inputs:
        merged = _deep_merge(merged, payload)
        for full_key, _value in _flatten(payload):
            provenance[full_key] = layer

    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(
            f"Invalid postcheck configuration: {exc.error_count()} error(s).\n{exc}",
            context={"errors": exc.errors(include_url=False)},
        ) from exc

    return settings, provenance


def load_config(
    project_root: Path | str | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    *,
    global_path: Path | None = None,
) -> Settings:
    """Layered loader. See :func:`load_config_with_provenance`."""
    settings, _ = load_config_with_provenance(
        project_root, cli_overrides, global_path=global_path
    )
    return settings


def load_settings(
    project_root: Path | str | None = None, **overrides: Any
) -> Settings:
    """Backward-compat shim — calls :func:`load_config` with flag overrides.

    Retained so existing callers (``orchestrator``, ``diff_analyzer``) keep
    working unchanged.
    """
    return load_config(project_root, overrides or None)


# ---------------------------------------------------------------------------
# Value coercion for `config set`
# ---------------------------------------------------------------------------


def coerce_value_for_key(key: str, raw: str) -> Any:
    """Coerce a CLI string to the type of ``Settings.<key>``.

    Supports dotted paths into nested BaseModels. Lists / dicts must be JSON.
    Booleans accept ``true|false|1|0|yes|no`` case-insensitive.

    Raises :class:`ConfigError` on a clearly wrong shape (e.g. non-JSON for
    a list field). Pydantic does the final validation on the merged config.
    """
    parts = key.split(".")
    model: type[BaseModel] = Settings
    field = None
    for i, part in enumerate(parts):
        if part not in model.model_fields:
            raise KeyError(key)
        field = model.model_fields[part]
        if i == len(parts) - 1:
            break
        nested = _unwrap_model(field.annotation)
        if nested is None:
            raise KeyError(key)
        model = nested
    assert field is not None
    annotation = field.annotation
    origin = get_origin(annotation)
    args = get_args(annotation)
    # Strip Optional[T]
    if origin is None and args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            annotation = non_none[0]
            origin = get_origin(annotation)
            args = get_args(annotation)

    # bool first (before int — bool is a subclass)
    if annotation is bool:
        low = raw.strip().lower()
        if low in {"true", "1", "yes", "on"}:
            return True
        if low in {"false", "0", "no", "off"}:
            return False
        raise ConfigError(
            f"expected boolean for '{key}' (true|false), got {raw!r}",
            context={"key": key, "expected": "bool", "got": raw},
        )
    if annotation is int:
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(
                f"expected integer for '{key}', got {raw!r} "
                f"(example: postcheck config set {key} 30000)",
                context={"key": key, "expected": "int", "got": raw},
            ) from exc
    if annotation is float:
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(
                f"expected number for '{key}', got {raw!r}",
                context={"key": key, "expected": "float", "got": raw},
            ) from exc
    if origin in (list, tuple) or annotation in (list, tuple):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"expected JSON array for '{key}', got {raw!r} "
                f'(example: postcheck config set {key} \'["**/foo.js"]\')',
                context={"key": key, "expected": "list (JSON)", "got": raw},
            ) from exc
        if not isinstance(parsed, list):
            raise ConfigError(
                f"expected JSON array for '{key}', got {type(parsed).__name__}",
                context={"key": key, "expected": "list (JSON)", "got": raw},
            )
        return parsed
    if origin is dict or annotation is dict:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"expected JSON object for '{key}', got {raw!r}",
                context={"key": key, "expected": "object (JSON)", "got": raw},
            ) from exc
        return parsed
    # Literal / string / anything else — return raw, let pydantic validate.
    return raw


def assign_nested(data: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """Return a copy of ``data`` with ``key`` (dotted) set to ``value``."""
    out = json.loads(json.dumps(data))  # cheap deep copy of JSON-safe dict
    parts = key.split(".")
    cur = out
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return out


def remove_nested(data: dict[str, Any], key: str) -> tuple[dict[str, Any], bool]:
    """Return ``(new_data, removed)``. ``removed`` is False if key was absent.

    Empty intermediate dicts are pruned for cleanliness.
    """
    out = json.loads(json.dumps(data))
    parts = key.split(".")
    chain: list[tuple[dict[str, Any], str]] = []
    cur = out
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            return out, False
        chain.append((cur, p))
        cur = cur[p]
    if parts[-1] not in cur:
        return out, False
    del cur[parts[-1]]
    # prune empty parents
    for parent, key_name in reversed(chain):
        if not parent[key_name]:
            del parent[key_name]
        else:
            break
    return out, True


__all__ = [
    "ColorMode",
    "ConfigLayer",
    "DEFAULT_EXCLUDE_GLOBS",
    "DEFAULT_NETWORK_FOCUS_PATTERNS",
    "DEFAULT_NETWORK_IGNORE_PATTERNS",
    "DEFAULT_RESOURCE_TYPES",
    "REDACTION_PATTERNS",
    "AdapterChoice",
    "CrossOriginPolicy",
    "NetworkSettings",
    "Settings",
    "all_keys",
    "allowed_layers_for",
    "assign_nested",
    "coerce_value_for_key",
    "get_global_config_path",
    "get_project_config_path",
    "load_config",
    "load_config_with_provenance",
    "load_settings",
    "remove_nested",
]
