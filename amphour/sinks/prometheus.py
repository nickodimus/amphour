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

from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server

from ..device import Device
from ..fields import Field, exported
from ..reading import Reading

log = logging.getLogger(__name__)


def _describe(declarations: list[tuple[str, Field]]) -> str:
    """HELP text for one metric, which several devices may declare.

    The shared vocabulary means two drivers land on one metric. Letting the
    first declaration's text speak for every device hides exactly the case the
    vocabulary exists to expose: the same quantity measured two ways, believed
    to different degrees. battery_state_of_charge is the live example - a
    Victron shunt counts coulombs, a Renogy controller infers from voltage, and
    on LiFePO4 those are not the same claim at all.

    When every declaration agrees, say it once; the common case stays short.
    """
    units = sorted({f.unit for _, f in declarations})
    confidences = {f.confidence for _, f in declarations}
    helps = {f.help for _, f in declarations}
    if len(declarations) == 1 or (len(units) == 1 and len(confidences) == 1 and len(helps) == 1):
        first = declarations[0][1]
        return f"{first.help} [{first.unit}, confidence: {first.confidence}]"
    unit = units[0] if len(units) == 1 else " / ".join(units)
    per_device = "; ".join(f"{device}={f.confidence}: {f.help}" for device, f in declarations)
    return f"[{unit}] declared by several devices - {per_device}"


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

        # Collect every declaration of every field BEFORE creating any metric.
        # Creating them as we go meant the first device to mention a field also
        # supplied its HELP for all of them.
        declarations: dict[str, list[tuple[str, Field]]] = {}
        for device in devices:
            allowed = exported(device.fields, include_unverified=include_unverified)
            self._allowed[device.name] = allowed
            for spec in device.fields:
                if spec.name in allowed:
                    declarations.setdefault(spec.name, []).append((device.name, spec))

        for name, decls in declarations.items():
            units = {f.unit for _, f in decls}
            if len(units) > 1:
                # One metric cannot hold two units. This is a bug in the field
                # tables, not a display problem, so it is said out loud.
                log.warning(
                    "field %r is declared with conflicting units %s by %s - "
                    "one metric cannot carry both",
                    name,
                    sorted(units),
                    ", ".join(d for d, _ in decls),
                )
            self._gauges[name] = Gauge(
                f"{prefix}{name}", _describe(decls), ["device"], registry=self.registry
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
        # Counters, not Gauges. These only ever go up, and the _total suffix is
        # the Counter convention - declaring a monotonic value as a gauge tells
        # tooling it may fall, so rate() and increase() cannot assume a reset on
        # restart means a restart. prometheus_client keeps the name as given
        # (no _total_total) and adds the companion _created series.
        self._readings = Counter(
            f"{prefix}readings_total",
            "Readings accepted from this device since start",
            ["device"],
            registry=self.registry,
        )
        self._failures = Counter(
            f"{prefix}failures_total",
            "Connection or read failures for this device since start",
            ["device"],
            registry=self.registry,
        )
        # Touch every child so each device has a series from startup. Without
        # this a device that has never succeeded is absent rather than zero,
        # and absent does not alert.
        for device in devices:
            self._readings.labels(device=device.name)
            self._failures.labels(device=device.name)

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
        self._readings.labels(device=reading.source).inc()
        self._last_read.labels(device=reading.source).set(time.time())

    def record_failure(self, device: str) -> None:
        self._failures.labels(device=device).inc()

    async def aclose(self) -> None:
        # prometheus_client's HTTP server has no clean shutdown hook worth
        # using here; the process exiting is the shutdown.
        return None
