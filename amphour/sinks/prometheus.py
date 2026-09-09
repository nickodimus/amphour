"""Prometheus sink: one gauge per field name, labelled by device.

Every metric carries a `device` label holding the configured device name. That
is the point of the shared field vocabulary described in amphour/fields.py: two
devices reporting `battery_voltage` land on ONE metric with two label values,
so a single query compares them. Two independent measurements of the same
battery disagreeing is a thing you want to see, not something to hide behind
separate metric names.

Metric names are `<prefix><field name>` with no unit suffix appended. That
departs from the Prometheus convention of suffixing base units, deliberately:
the field names already carry their quantity (`battery_voltage`, `pv_power`),
so suffixing yields `battery_voltage_volts`. Units live in HELP. The payoff is
that every metric name maps one-to-one onto a driver's declared field.

Text-valued readings (a model string, an alarm state) are not exported here -
a Prometheus gauge takes a number, and encoding strings as labels invites
unbounded cardinality. They go to InfluxDB. The numeric equivalents are
exported: `alarm_reason` and `charging_state` are numbers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from prometheus_client import CollectorRegistry, Gauge, start_http_server

from ..device import Device
from ..fields import exported
from ..reading import Reading

log = logging.getLogger(__name__)


class PrometheusSink:
    name = "prometheus"

    def __init__(
        self,
        devices: Sequence[Device],
        *,
        port: int = 5000,
        addr: str = "0.0.0.0",
        prefix: str = "amphour_",
        include_unverified: bool = False,
        registry: CollectorRegistry | None = None,
        start_server: bool = True,
    ) -> None:
        self.prefix = prefix
        self.registry = registry if registry is not None else CollectorRegistry()
        self._allowed: dict[str, set[str]] = {}
        self._gauges: dict[str, Gauge] = {}

        for device in devices:
            allowed = exported(device.fields, include_unverified=include_unverified)
            self._allowed[device.name] = allowed
            for spec in device.fields:
                if spec.name not in allowed or spec.name in self._gauges:
                    continue
                self._gauges[spec.name] = Gauge(
                    f"{prefix}{spec.name}",
                    f"{spec.help} [{spec.unit}, confidence: {spec.confidence}]",
                    ["device"],
                    registry=self.registry,
                )

        # Health of the collector itself. The predecessor had no equivalent:
        # when its link died the metrics simply stopped updating at their last
        # value, which a dashboard cannot tell apart from a becalmed system.
        # An explicit read timestamp makes staleness alertable per device.
        self._last_read = Gauge(
            f"{prefix}last_reading_timestamp_seconds",
            "Unix time of the last reading accepted from this device",
            ["device"],
            registry=self.registry,
        )
        self._readings = Gauge(
            f"{prefix}readings_total",
            "Readings accepted from this device since start",
            ["device"],
            registry=self.registry,
        )
        self._failures = Gauge(
            f"{prefix}failures_total",
            "Connection or read failures for this device since start",
            ["device"],
            registry=self.registry,
        )
        self._counts: dict[str, int] = {}
        self._fails: dict[str, int] = {}
        for device in devices:
            self._readings.labels(device=device.name).set(0)
            self._failures.labels(device=device.name).set(0)

        if start_server:
            start_http_server(port, addr=addr, registry=self.registry)
            log.info("prometheus metrics on http://%s:%d/metrics", addr, port)

    async def publish(self, reading: Reading) -> None:
        allowed = self._allowed.get(reading.source)
        for name, value in reading.values.items():
            if allowed is not None and name not in allowed:
                continue
            gauge = self._gauges.get(name)
            if gauge is not None:
                gauge.labels(device=reading.source).set(value)
        self._counts[reading.source] = self._counts.get(reading.source, 0) + 1
        self._readings.labels(device=reading.source).set(self._counts[reading.source])
        self._last_read.labels(device=reading.source).set(time.time())

    def record_failure(self, device: str) -> None:
        self._fails[device] = self._fails.get(device, 0) + 1
        self._failures.labels(device=device).set(self._fails[device])

    async def aclose(self) -> None:
        # prometheus_client's HTTP server has no clean shutdown hook worth
        # using here; the process exiting is the shutdown.
        return None
