"""Configuration: TOML in, validated dataclasses out.

tomllib is stdlib from 3.11, which is this project's floor, so config costs no
dependency.

Devices are a list, and each entry names a driver plus whatever that driver
needs. Unknown keys are an error rather than a silent default, because a
misspelled option that gets dropped is how a setting appears to be applied
without being applied.

Secrets may come from the environment instead of the file:

    AMPHOUR_INFLUX_USERNAME
    AMPHOUR_INFLUX_PASSWORD

The environment wins over the file. A config file gets copied, backed up and
pasted into terminals; an environment variable is easier to keep out of all of
those.
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
    name: str
    driver: str
    options: dict[str, Any] = field(default_factory=dict)


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
    devices: tuple[DeviceConfig, ...]
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    influxdb: InfluxConfig = field(default_factory=InfluxConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _known(cls: Any, data: dict[str, Any], section: str) -> dict[str, Any]:
    allowed = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(
            f"[{section}] has unknown option(s): {', '.join(sorted(unknown))}. "
            f"Known options: {', '.join(sorted(allowed))}"
        )
    return data


def _devices(raw: dict[str, Any]) -> tuple[DeviceConfig, ...]:
    entries = raw.get("device", [])
    if isinstance(entries, dict):
        raise ConfigError("[device] must be a list of tables - write [[device]], not [device]")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("at least one [[device]] section is required")

    devices: list[DeviceConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"[[device]] #{index + 1} must be a table")
        options = dict(entry)
        name = options.pop("name", None)
        driver = options.pop("driver", None)
        if not name:
            raise ConfigError(f"[[device]] #{index + 1} needs a name")
        if not driver:
            raise ConfigError(f"[[device]] {name!r} needs a driver")
        if name in seen:
            raise ConfigError(
                f"two devices are both named {name!r}; names label the metrics and must be unique"
            )
        seen.add(str(name))
        devices.append(DeviceConfig(name=str(name), driver=str(driver), options=options))
    return tuple(devices)


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
    devices = _devices(raw)

    influx_raw = dict(_section(raw, "influxdb"))
    tags = influx_raw.pop("tags", {})
    if not isinstance(tags, dict):
        raise ConfigError("[influxdb.tags] must be a table of string keys and values")
    influx_raw = _known(InfluxConfig, influx_raw, "influxdb")

    env_user = os.environ.get("AMPHOUR_INFLUX_USERNAME")
    env_pass = os.environ.get("AMPHOUR_INFLUX_PASSWORD")
    if env_user is not None:
        influx_raw["username"] = env_user
    if env_pass is not None:
        influx_raw["password"] = env_pass
    for key in ("username", "password", "retention_policy"):
        if influx_raw.get(key) == "":
            influx_raw[key] = None

    influx = InfluxConfig(**influx_raw, tags={str(k): str(v) for k, v in tags.items()})
    if influx.enabled and not (influx.url and influx.database):
        raise ConfigError("[influxdb] enabled requires both url and database")

    config = Config(
        devices=devices,
        prometheus=PrometheusConfig(
            **_known(PrometheusConfig, _section(raw, "prometheus"), "prometheus")
        ),
        influxdb=influx,
        logging=LoggingConfig(**_known(LoggingConfig, _section(raw, "logging"), "logging")),
    )
    if not config.prometheus.enabled and not config.influxdb.enabled:
        raise ConfigError("no sink enabled: readings would go nowhere")
    return config
