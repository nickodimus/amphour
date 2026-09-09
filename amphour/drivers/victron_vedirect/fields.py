"""VE.Direct label map.

Every entry below was cross-checked on 2026-09-09 against the `description`
and `units` an independent implementation (the Node-RED victron-vedirect node)
recorded for the same labels from the same physical device, which is why they
are marked `confirmed` rather than taken on trust from the specification.

Field NAMES are the shared vocabulary described in amphour/fields.py, not the
raw VE.Direct labels: `V` becomes `battery_voltage` because that is what the
Renogy driver calls the same physical quantity, so one query compares them.

Note `battery_current` is deliberately NOT the same field as the Renogy
driver's `battery_charging_current`. A shunt measures net current into and out
of the bank, signed; a charge controller measures only what it is itself
delivering. Same units, different quantities, so different names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from ...fields import Confidence, Field

Kind = Literal["number", "text"]


@dataclass(frozen=True, slots=True)
class Label:
    """One VE.Direct label and how to interpret its text value."""

    label: str
    name: str
    kind: Kind
    scale: float
    unit: str
    confidence: Confidence
    help: str

    def as_field(self) -> Field:
        return Field(self.name, self.unit, self.confidence, self.help)


LABELS: Final[tuple[Label, ...]] = (
    # --- instantaneous ---
    Label("V", "battery_voltage", "number", 0.001, "volts", "confirmed", "Main (battery) voltage"),
    Label(
        "I",
        "battery_current",
        "number",
        0.001,
        "amperes",
        "confirmed",
        "Net battery current, positive charging and negative discharging",
    ),
    Label(
        "P",
        "battery_power",
        "number",
        1,
        "watts",
        "confirmed",
        "Instantaneous power into or out of the battery",
    ),
    Label(
        "CE",
        "consumed_amp_hours",
        "number",
        0.001,
        "amp_hours",
        "confirmed",
        "Charge consumed since the last full charge",
    ),
    Label(
        "SOC",
        "battery_state_of_charge",
        "number",
        0.1,
        "percent",
        "confirmed",
        "State of charge, coulomb-counted rather than derived from voltage",
    ),
    Label(
        "TTG",
        "time_to_go_minutes",
        "number",
        1,
        "minutes",
        "confirmed",
        "Estimated time until the battery is empty; -1 means infinite",
    ),
    Label("AR", "alarm_reason", "number", 1, "bitfield", "confirmed", "Alarm reason bitfield"),
    Label("MON", "dc_monitor_mode", "number", 1, "enum", "confirmed", "DC monitor mode"),
    Label("Alarm", "alarm", "text", 1, "state", "confirmed", "Alarm condition active"),
    Label(
        "BMV",
        "model",
        "text",
        1,
        "text",
        "confirmed",
        "Model description (deprecated by Victron in favour of PID)",
    ),
    Label("FW", "firmware_version", "text", 1, "text", "confirmed", "Firmware version, 16 bit"),
    Label("PID", "product_id", "text", 1, "text", "confirmed", "Product ID"),
    # --- lifetime history ---
    Label(
        "H1",
        "deepest_discharge",
        "number",
        0.001,
        "amp_hours",
        "confirmed",
        "Depth of the deepest discharge",
    ),
    Label(
        "H2",
        "last_discharge",
        "number",
        0.001,
        "amp_hours",
        "confirmed",
        "Depth of the last discharge",
    ),
    Label(
        "H3",
        "average_discharge",
        "number",
        0.001,
        "amp_hours",
        "confirmed",
        "Depth of the average discharge",
    ),
    Label("H4", "charge_cycles", "number", 1, "count", "confirmed", "Number of charge cycles"),
    Label("H5", "full_discharges", "number", 1, "count", "confirmed", "Number of full discharges"),
    Label(
        "H6",
        "cumulative_amp_hours_drawn",
        "number",
        0.001,
        "amp_hours",
        "confirmed",
        "Cumulative amp hours drawn",
    ),
    Label(
        "H7",
        "battery_voltage_min_lifetime",
        "number",
        0.001,
        "volts",
        "confirmed",
        "Minimum main battery voltage ever seen",
    ),
    Label(
        "H8",
        "battery_voltage_max_lifetime",
        "number",
        0.001,
        "volts",
        "confirmed",
        "Maximum main battery voltage ever seen",
    ),
    Label(
        "H9",
        "seconds_since_full_charge",
        "number",
        1,
        "seconds",
        "confirmed",
        "Seconds since the last full charge. A shunt that syncs to 100% on a "
        "worn battery will show a small value here while reporting an "
        "implausibly high state of charge",
    ),
    Label(
        "H10",
        "automatic_syncs",
        "number",
        1,
        "count",
        "confirmed",
        "Number of automatic synchronisations",
    ),
    Label(
        "H11",
        "low_voltage_alarms",
        "number",
        1,
        "count",
        "confirmed",
        "Number of low main voltage alarms",
    ),
    Label(
        "H12",
        "high_voltage_alarms",
        "number",
        1,
        "count",
        "confirmed",
        "Number of high main voltage alarms",
    ),
    Label(
        "H15",
        "aux_voltage_min_lifetime",
        "number",
        0.001,
        "volts",
        "confirmed",
        "Minimum auxiliary battery voltage",
    ),
    Label(
        "H16",
        "aux_voltage_max_lifetime",
        "number",
        0.001,
        "volts",
        "confirmed",
        "Maximum auxiliary battery voltage",
    ),
    Label(
        "H17",
        "discharged_energy",
        "number",
        0.01,
        "kilowatt_hours",
        "confirmed",
        "Cumulative discharged energy",
    ),
    Label(
        "H18",
        "charged_energy",
        "number",
        0.01,
        "kilowatt_hours",
        "confirmed",
        "Cumulative charged energy",
    ),
)

BY_LABEL: Final[dict[str, Label]] = {label.label: label for label in LABELS}
FIELDS: Final[tuple[Field, ...]] = tuple(label.as_field() for label in LABELS)
