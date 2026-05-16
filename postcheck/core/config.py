"""Pydantic Settings for postcheck (v0).

Load order (highest precedence first):

1. Environment variables prefixed ``POSTCHECK_`` (nested delimiter ``__``,
   e.g. ``POSTCHECK_NETWORK__IGNORE_PATTERNS='["foo"]'``).
2. ``.postcheck/config.json`` in the project root, if present.
3. Hard-coded defaults defined here.

Secrets — database URL, Redis URL, Anthropic API key — are read from
environment variables only and never accepted from the JSON file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .errors import ConfigError

AdapterChoice = Literal["auto", "plain_html", "react_vite"]
CrossOriginPolicy = Literal["high", "medium", "low", "ignore"]


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


class NetworkSettings(BaseSettings):
    """Three-layer filter for the network probe (v0)."""

    model_config = SettingsConfigDict(extra="forbid")

    resource_types: list[str] = Field(default_factory=lambda: list(DEFAULT_RESOURCE_TYPES))
    ignore_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_NETWORK_IGNORE_PATTERNS)
    )
    focus_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_NETWORK_FOCUS_PATTERNS)
    )
    treat_cross_origin_as: CrossOriginPolicy = "medium"


# ---------------------------------------------------------------------------
# JSON file source
# ---------------------------------------------------------------------------


class _JsonFileSource(PydanticBaseSettingsSource):
    """Reads ``.postcheck/config.json`` from a project root if present."""

    def __init__(self, settings_cls: type[BaseSettings], project_root: Path) -> None:
        super().__init__(settings_cls)
        self._path = project_root / ".postcheck" / "config.json"
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if not self._path.is_file():
            self._data = {}
            return self._data
        try:
            raw = self._path.read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"Invalid JSON in {self._path}: {exc.msg} (line {exc.lineno}, col {exc.colno})",
                context={"path": str(self._path)},
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigError(
                f"{self._path} must contain a JSON object at the top level",
                context={"path": str(self._path), "got": type(parsed).__name__},
            )
        self._data = parsed
        return self._data

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # noqa: ARG002
        data = self._load()
        if field_name in data:
            return data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._load()


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
    )

    # Adapter selection
    adapter: AdapterChoice = "auto"

    # Browser
    base_url: str = "http://localhost:3000"
    chrome_debug_port: int = Field(default=9222, ge=1, le=65535)
    chrome_profile_dir: str = "$HOME/.postcheck-chrome-profile"

    # Launch vs. attach. ``attach`` (the default) connects to a user-controlled
    # Chrome over CDP. ``launch`` spawns a Playwright-managed browser — used in
    # environments where pre-running Chrome isn't possible (Codespaces, CI,
    # headless servers).
    launch_mode: Literal["attach", "launch"] = "attach"
    launch_headless: bool = True
    launch_args: list[str] = Field(default_factory=list)

    # Timeouts
    timeout_ms: int = Field(default=30_000, ge=0)

    # Filtering
    exclude_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))

    # Probes
    network: NetworkSettings = Field(default_factory=NetworkSettings)

    # Secrets — env-only
    database_url: str | None = Field(default=None)
    redis_url: str | None = Field(default=None)
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: init kwargs > env > .postcheck/config.json > defaults.
        json_source = getattr(cls, "_json_source", None)
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if json_source is not None:
            sources.append(json_source)
        sources.append(dotenv_settings)
        sources.append(file_secret_settings)
        return tuple(sources)


def load_settings(project_root: Path | str | None = None, **overrides: Any) -> Settings:
    """Load ``Settings`` for ``project_root``.

    Raises:
        ConfigError: if the JSON file is malformed or any field fails validation.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()

    class _Bound(Settings):
        pass

    _Bound._json_source = _JsonFileSource(_Bound, root)  # type: ignore[attr-defined]
    try:
        return _Bound(**overrides)
    except ConfigError:
        raise
    except ValidationError as exc:
        raise ConfigError(
            f"Invalid postcheck configuration: {exc.error_count()} error(s).\n{exc}",
            context={"errors": exc.errors(include_url=False)},
        ) from exc


__all__ = [
    "DEFAULT_EXCLUDE_GLOBS",
    "DEFAULT_NETWORK_FOCUS_PATTERNS",
    "DEFAULT_NETWORK_IGNORE_PATTERNS",
    "DEFAULT_RESOURCE_TYPES",
    "REDACTION_PATTERNS",
    "AdapterChoice",
    "CrossOriginPolicy",
    "NetworkSettings",
    "Settings",
    "load_settings",
]
