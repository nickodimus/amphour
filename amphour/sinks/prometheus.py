"""Prometheus sink: gauges generated from the register table.

Metric names are `<prefix><field name>` with no unit suffix appended. That
deviates from the strict Prometheus convention of suffixing base units, and it
is deliberate: the field names already carry their quantity (battery_voltage,
pv_power), so suffixing produces battery_voltage_volts. The units live in HELP.
The payoff is that every metric name maps one-to-one onto a row of
registers.FIELDS, so the two can never drift apart.

Set `metric_prefix = "solarmon_"` in config to keep dashboards built against
the predecessor working through a cutover.
"""

from __future__ import annotations

import logging
import time

from prometheus_client import CollectorRegistry, Gauge, start_http_server

from ..reading import Reading
from ..registers import DEFAULT_EXPORTED, FIELDS

log = logging.getLogger(__name__)


class PrometheusSink:
    name = "prometheus"

    def __init__(
        self,
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
        exported = {f.name for f in FIELDS} if include_unverified else set(DEFAULT_EXPORTED)
        self._gauges: dict[str, Gauge] = {}
        for f in FIELDS:
            if f.name not in exported:
                continue
            self._gauges[f.name] = Gauge(
                f"{prefix}{f.name}",
                f"{f.help} [{f.unit}, confidence: {f.confidence}]",
                registry=self.registry,
            )

        # Health of the collector itself. The predecessor had no equivalent:
        # when its BLE link died the metrics simply stopped updating at their
        # last value, which a dashboard cannot distinguish from a becalmed
        # system. An explicit read timestamp makes staleness alertable.
        self._last_read = Gauge(
            f"{prefix}last_successful_read_timestamp_seconds",
            "Unix time of the last frame that decoded cleanly",
            registry=self.registry,
        )
        self._reads = Gauge(
            f"{prefix}reads_total",
            "Frames decoded successfully since start",
            registry=self.registry,
        )
        self._failures = Gauge(
            f"{prefix}read_failures_total",
            "Read attempts that errored since start",
            registry=self.registry,
        )
        self._read_count = 0
        self._failure_count = 0

        if start_server:
            start_http_server(port, addr=addr, registry=self.registry)
            log.info("prometheus metrics on http://%s:%d/metrics", addr, port)

    async def publish(self, reading: Reading) -> None:
        for name, gauge in self._gauges.items():
            value = reading.values.get(name)
            if value is not None:
                gauge.set(value)
        self._read_count += 1
        self._reads.set(self._read_count)
        self._last_read.set(time.time())

    def record_failure(self) -> None:
        self._failure_count += 1
        self._failures.set(self._failure_count)

    async def aclose(self) -> None:
        # prometheus_client's HTTP server has no clean shutdown hook worth
        # using here; the process exiting is the shutdown.
        return None
