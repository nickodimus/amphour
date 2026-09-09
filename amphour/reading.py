"""One observation from one device."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class Reading:
    """A set of values read from a device at one instant.

    `source` is the configured device name. It becomes a Prometheus label and
    an InfluxDB tag, which is what lets two devices reporting the same field -
    two independent measurements of one battery, say - be compared in a single
    query rather than living under separate metric names.

    `values` are numeric and scaled into the units declared by the driver's
    fields. `text` holds string-valued readings that some protocols carry
    (Victron VE.Direct reports `Alarm` as OFF/ON and a model name as text);
    these are kept separate because they cannot be a Prometheus gauge value.
    """

    source: str
    values: dict[str, float] = field(default_factory=dict)
    text: dict[str, str] = field(default_factory=dict)
    taken_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __str__(self) -> str:
        parts = []
        for key, fmt in (
            ("battery_voltage", "{:.2f}V"),
            ("battery_state_of_charge", "{:.0f}%"),
            ("pv_power", "pv={:.0f}W"),
            ("battery_current", "{:+.2f}A"),
        ):
            if key in self.values:
                parts.append(fmt.format(self.values[key]))
        state = self.text.get("charging_state") or self.text.get("alarm")
        if state:
            parts.append(f"[{state}]")
        return f"{self.source}: " + " ".join(parts) if parts else f"{self.source}: (no values)"
