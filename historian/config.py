"""Strict runtime configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from importlib.resources import files
from pathlib import Path
from typing import Any

from .errors import ConfigError


CONFIG_ENV_VAR = "HISTORIAN_CONFIG_PATH"


def _env_name(field_name: str) -> str:
    return f"HISTORIAN_{field_name.upper()}"


def _xdg_config_home() -> Path:
    return Path(os.getenv("XDG_CONFIG_HOME", "~/.config")).expanduser()


def _xdg_data_home() -> Path:
    return Path(os.getenv("XDG_DATA_HOME", "~/.local/share")).expanduser()


#: Filename of the packaged template, relative to the ``historian`` package.
CONFIG_TEMPLATE_NAME = "config.example.json"


def default_config_path() -> Path:
    """Return the config path Historian loads by default for installed users.

    This is the XDG-style path (``${XDG_CONFIG_HOME:-~/.config}/historian/config.json``),
    not ``./config.json``. ``historian config init`` writes here so it lands at the
    standard location a non-clone user will load from.
    """
    return _xdg_config_home() / "historian" / "config.json"


def read_config_template() -> str:
    """Return the text of the packaged config template."""
    try:
        return files("historian").joinpath(CONFIG_TEMPLATE_NAME).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Packaged config template not found: {CONFIG_TEMPLATE_NAME}. "
            "Your historian installation may be incomplete; reinstall with "
            "'uv tool install --force git+https://github.com/randileeharper/historian'."
        ) from exc


def write_default_config(target: Path | None = None, *, force: bool = False) -> Path:
    """Write the packaged template config to *target* (default: XDG path).

    Returns the path written. Refuses to overwrite an existing file unless
    *force* is True.
    """
    if target is None:
        target = default_config_path()
    target = Path(target).expanduser()
    if target.is_file() and not force:
        raise ConfigError(f"Config file already exists: {target}. Use --force to overwrite.")
    template = read_config_template()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template, encoding="utf-8")
    return target


def _coerce(raw: str, default: Any) -> Any:
    if isinstance(default, bool):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"Could not interpret boolean value {raw!r}.")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


@dataclass(slots=True)
class Settings:
    loaded_config_path: str | None = field(default=None, init=False)
    http_host: str = "127.0.0.1"
    http_port: int = 8760
    public_base_url: str = "http://127.0.0.1:8760"
    database_path: str = str(_xdg_data_home() / "historian" / "historian.db")
    resolver_backend: str = "openai_compatible"
    resolver_base_url: str = "http://localhost:11434/v1"
    resolver_model: str = "gemma4:latest"
    resolver_api_key: str = ""
    resolver_include_reasoning: bool = False
    debug_enabled: bool = False
    debug_log_path: str = str(_xdg_data_home() / "historian" / "debug.log")
    resolver_debug_log_path: str = str(_xdg_data_home() / "historian" / "resolver.log")
    cli_token_path: str = str(_xdg_config_home() / "historian" / "cli-token")
    request_timeout_seconds: float = 60.0
    resolver_max_retries: int = 3
    verify_tls: bool = True
    log_level: str = "INFO"
    max_search_results: int = 50
    max_records_per_model_call: int = 50
    max_query_records: int = 1000
    max_evidence_characters: int = 32000
    max_regex_patterns: int = 3
    max_regex_length: int = 256
    max_regex_candidates: int = 5000
    regex_timeout_seconds: float = 0.05
    max_event_bytes: int = 1048576
    max_batch_events: int = 100

    @classmethod
    def resolve_config_path(cls, explicit: str | None = None) -> Path | None:
        candidates: list[Path] = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        elif os.getenv(CONFIG_ENV_VAR):
            candidates.append(Path(os.environ[CONFIG_ENV_VAR]).expanduser())
        else:
            candidates.extend([Path.cwd() / "config.json", _xdg_config_home() / "historian" / "config.json"])
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        if explicit:
            raise ConfigError(f"Config file not found: {candidates[0]}")
        return None

    @classmethod
    def load(cls, explicit: str | None = None) -> "Settings":
        path = cls.resolve_config_path(explicit)
        data: dict[str, Any] = {}
        if path:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"Could not parse config file {path}: {exc}") from exc
            if not isinstance(data, dict):
                raise ConfigError(f"Config file {path} must contain an object.")

        configurable = {item.name for item in fields(cls) if item.init}
        unknown = sorted(set(data) - configurable)
        if unknown:
            raise ConfigError(f"Unknown config fields: {', '.join(unknown)}")

        values: dict[str, Any] = {}
        defaults = cls()
        for item in fields(cls):
            if not item.init:
                continue
            default = getattr(defaults, item.name)
            value = data.get(item.name, default)
            env_name = _env_name(item.name)
            if env_name in os.environ:
                value = _coerce(os.environ[env_name], default)
            values[item.name] = value
        settings = cls(**values)
        settings.loaded_config_path = str(path.resolve()) if path else None
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.http_host:
            raise ConfigError("http_host cannot be empty.")
        if not 1 <= self.http_port <= 65535:
            raise ConfigError("http_port must be between 1 and 65535.")
        if not self.public_base_url:
            raise ConfigError("public_base_url cannot be empty.")
        if self.resolver_backend not in {"openai_compatible", "fake"}:
            raise ConfigError("resolver_backend must be openai_compatible or fake.")
        if self.resolver_backend == "openai_compatible" and not self.resolver_model:
            raise ConfigError("resolver_model is required.")
        if self.resolver_max_retries < 0 or self.resolver_max_retries > 10:
            raise ConfigError("resolver_max_retries must be between 0 and 10.")
        for name in (
            "max_search_results",
            "max_records_per_model_call",
            "max_query_records",
            "max_evidence_characters",
            "max_regex_patterns",
            "max_regex_length",
            "max_regex_candidates",
            "max_event_bytes",
            "max_batch_events",
        ):
            if int(getattr(self, name)) <= 0:
                raise ConfigError(f"{name} must be positive.")
        if self.regex_timeout_seconds <= 0 or self.regex_timeout_seconds > 1:
            raise ConfigError("regex_timeout_seconds must be greater than 0 and at most 1.")
        if self.log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ConfigError("log_level is invalid.")
        if self.debug_enabled:
            if not self.debug_log_path.strip():
                raise ConfigError("debug_log_path cannot be empty when debug_enabled is true.")
            if not self.resolver_debug_log_path.strip():
                raise ConfigError("resolver_debug_log_path cannot be empty when debug_enabled is true.")

    @property
    def expanded_database_path(self) -> Path:
        return Path(self.database_path).expanduser()

    @property
    def expanded_debug_log_path(self) -> Path:
        return Path(self.debug_log_path).expanduser()

    @property
    def expanded_resolver_debug_log_path(self) -> Path:
        return Path(self.resolver_debug_log_path).expanduser()

    @property
    def expanded_cli_token_path(self) -> Path:
        return Path(self.cli_token_path).expanduser()

    def sanitized(self) -> dict[str, Any]:
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name not in {"resolver_api_key"}
        }
        payload["has_resolver_api_key"] = bool(self.resolver_api_key)
        payload["database_path"] = str(self.expanded_database_path)
        payload["debug_log_path"] = str(self.expanded_debug_log_path)
        payload["resolver_debug_log_path"] = str(self.expanded_resolver_debug_log_path)
        payload["cli_token_path"] = str(self.expanded_cli_token_path)
        payload["has_cli_token"] = self.expanded_cli_token_path.is_file()
        return payload
