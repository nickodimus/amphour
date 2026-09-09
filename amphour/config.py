"""Configuration: TOML in, validated dataclasses out.

tomllib is stdlib from Python 3.11, which is the floor for this project, so
config costs no dependency.

Secrets may be supplied by environment variable instead of living in the file:

    AMPHOUR_INFLUX_USERNAME
    AMPHOUR_INFLUX_PASSWORD

The environment wins over the file when both are present. This is what lets a
deployment keep credentials out of a file that might be copied, backed up, or
pasted into a terminal.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Configuration is missing something required, or a value is nonsense."""


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    address: str
    alias: str | None = None
    adapter: str | None = None


@dataclass(frozen=True, slots=True)
class PollConfig:
    interval: float = 30.0
    connect_timeout: float = 30.0
    response_timeout: float = 10.0
    backoff_initial: float = 5.0
    backoff_max: float = 300.0


@dataclass(frozen=True, slots=True)
class PrometheusConfig:
    enabled: bool = True
    port: int = 5000
    addr: str = "0.0.0.0"
    metric_prefix: str = "amphour_"
    include_unverified: bool = False


@dataclass(frozen=True, slots=True)
class InfluxConfig:
    enabled: bool = False
    url: str = ""
    database: str = ""
    measurement: str = "amphour"
    username: str | None = None
    password: str | None = None
    retention_policy: str | None = None
    batch_size: int = 60
    flush_interval: float = 60.0
    timeout: float = 10.0
    include_unverified: bool = False
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    file: str | None = None
    keep_days: int = 14


@dataclass(frozen=True, slots=True)
class Config:
    device: DeviceConfig
    poll: PollConfig = field(default_factory=PollConfig)
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    influxdb: InfluxConfig = field(default_factory=InfluxConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _known(cls: Any, data: dict[str, Any], section: str) -> dict[str, Any]:
    """Reject unknown keys rather than silently ignoring a typo.

    A misspelled option that is quietly dropped is how a setting appears to be
    applied without being applied.
    """
    allowed = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(
            f"[{section}] has unknown option(s): {', '.join(sorted(unknown))}. "
            f"Known options: {', '.join(sorted(allowed))}"
        )
    return data


def load(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"no config file at {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return from_dict(raw)


def from_dict(raw: dict[str, Any]) -> Config:
    device_raw = _section(raw, "device")
    if not device_raw.get("address"):
        raise ConfigError("[device] address is required (the BLE MAC address)")
    device = DeviceConfig(**_known(DeviceConfig, device_raw, "device"))

    influx_raw = dict(_section(raw, "influxdb"))
    tags = influx_raw.pop("tags", {})
    if not isinstance(tags, dict):
        raise ConfigError("[influxdb.tags] must be a table of string keys and values")
    influx_raw = _known(InfluxConfig, influx_raw, "influxdb")

    # environment beats file, so credentials need not be written down
    env_user = os.environ.get("AMPHOUR_INFLUX_USERNAME")
    env_pass = os.environ.get("AMPHOUR_INFLUX_PASSWORD")
    if env_user is not None:
        influx_raw["username"] = env_user
    if env_pass is not None:
        influx_raw["password"] = env_pass
    # empty strings in TOML mean "not set"
    for key in ("username", "password", "retention_policy"):
        if influx_raw.get(key) == "":
            influx_raw[key] = None

    influx = InfluxConfig(**influx_raw, tags={str(k): str(v) for k, v in tags.items()})
    if influx.enabled and not (influx.url and influx.database):
        raise ConfigError("[influxdb] enabled requires both url and database")

    config = Config(
        device=device,
        poll=PollConfig(**_known(PollConfig, _section(raw, "poll"), "poll")),
        prometheus=PrometheusConfig(
            **_known(PrometheusConfig, _section(raw, "prometheus"), "prometheus")
        ),
        influxdb=influx,
        logging=LoggingConfig(**_known(LoggingConfig, _section(raw, "logging"), "logging")),
    )
    if config.poll.interval <= 0:
        raise ConfigError("[poll] interval must be greater than zero")
    if not config.prometheus.enabled and not config.influxdb.enabled:
        raise ConfigError("no sink enabled: readings would go nowhere")
    return config
