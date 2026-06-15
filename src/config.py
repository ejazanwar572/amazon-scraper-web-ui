"""Configuration management for the Amazon product scraper.

Loads from config.yaml with environment variable overrides using the
``AMZSCRAPER_`` prefix.  For example, ``AMZSCRAPER_HTTP_TIMEOUT=60``
overrides ``http.timeout``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "HttpConfig",
    "ProxyConfig",
    "QueueConfig",
    "StorageConfig",
    "ScrapingConfig",
    "MonitoringConfig",
    "ScraperConfig",
    "load_config",
]

# ---------------------------------------------------------------------------
# Nested configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass
class HttpConfig:
    timeout: int = 30
    max_retries: int = 3
    retry_backoff: float = 1.5
    concurrent_requests: int = 50


@dataclass
class ProxyConfig:
    provider: str = "file"
    proxy_list_file: str = "proxies.txt"
    session_duration_sec: int = 300
    rotation_strategy: str = "round_robin"


@dataclass
class QueueConfig:
    backend: str = "file"
    redis_url: str = "redis://localhost:6379/0"
    bloom_filter_capacity: int = 1_000_000
    bloom_error_rate: float = 0.001


@dataclass
class StorageConfig:
    backend: str = "sqlite"
    db_path: str = "data/products.db"
    pg_dsn: str = ""


@dataclass
class ScrapingConfig:
    marketplace: str = "in"
    request_delay_min: float = 1.0
    request_delay_max: float = 3.0
    user_agent_rotation: bool = True


@dataclass
class MonitoringConfig:
    log_level: str = "INFO"
    stats_interval_sec: int = 60
    alert_webhook_url: str = ""


# Mapping of section name → dataclass type
_SECTION_MAP: dict[str, type] = {
    "http": HttpConfig,
    "proxy": ProxyConfig,
    "queue": QueueConfig,
    "storage": StorageConfig,
    "scraping": ScrapingConfig,
    "monitoring": MonitoringConfig,
}


@dataclass
class ScraperConfig:
    http: HttpConfig = field(default_factory=HttpConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    scraping: ScrapingConfig = field(default_factory=ScrapingConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce(value: str, target_type: type) -> Any:
    """Coerce a string env-var value to the expected field type."""
    if target_type is bool:
        return value.lower() in ("1", "true", "yes")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


def _apply_env_overrides(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merge ``AMZSCRAPER_<SECTION>_<KEY>`` env vars into *raw*."""
    prefix = "AMZSCRAPER_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, field_name = parts
        if section not in _SECTION_MAP:
            continue
        # Validate that the field actually exists on the target dataclass
        dc_fields = {f.name: f for f in fields(_SECTION_MAP[section])}
        if field_name not in dc_fields:
            continue
        target_type = dc_fields[field_name].type
        # Resolve stringified type annotations
        type_map = {"int": int, "float": float, "str": str, "bool": bool}
        if isinstance(target_type, str):
            target_type = type_map.get(target_type, str)
        raw.setdefault(section, {})[field_name] = _coerce(value, target_type)
    return raw


def _build_section(section_cls: type, data: dict[str, Any] | None) -> Any:
    """Instantiate a config section dataclass from a raw dict."""
    if data is None:
        return section_cls()
    known = {f.name for f in fields(section_cls)}
    filtered = {k: v for k, v in data.items() if k in known}
    return section_cls(**filtered)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(path: str | None = None) -> ScraperConfig:
    """Load configuration from YAML, then overlay environment variables.

    Parameters
    ----------
    path:
        Path to the YAML config file.  When *None* the loader looks for
        ``config.yaml`` in the current working directory.

    Returns
    -------
    ScraperConfig
        Fully resolved configuration object.
    """
    config_path = Path(path) if path else Path("config.yaml")

    raw: dict[str, dict[str, Any]] = {}
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
            if isinstance(loaded, dict):
                raw = loaded

    raw = _apply_env_overrides(raw)

    return ScraperConfig(
        http=_build_section(HttpConfig, raw.get("http")),
        proxy=_build_section(ProxyConfig, raw.get("proxy")),
        queue=_build_section(QueueConfig, raw.get("queue")),
        storage=_build_section(StorageConfig, raw.get("storage")),
        scraping=_build_section(ScrapingConfig, raw.get("scraping")),
        monitoring=_build_section(MonitoringConfig, raw.get("monitoring")),
    )
