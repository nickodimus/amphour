"""The BT-1 register map, as one table.

Decoding, Prometheus metric definitions and InfluxDB fields are all generated
from this list. There is deliberately no second copy of these names anywhere in
the codebase: a hand-maintained parallel list is where transcription errors live.

Confidence is recorded per field, because this map was reverse engineered and
being honest about which parts are guesses is part of the data:

  confirmed   decoded from a real captured frame AND cross-checked against the
              value the previous app independently reported for the same instant
  probable    not independently cross-checked, but triangulates against a
              confirmed field in a physically coherent way
  unverified  consistent with the published Renogy register layout and with the
              captured bytes, but nothing yet rules out another reading

Only `confirmed` and `probable` fields are exported by default. Everything else
needs `include_unverified = true` in config, so nobody builds an alarm on a
field that is still a hypothesis.

The night capture that anchors this (2026-09-09 00:06-00:13) had the array dark
and the controller's load terminals unused, so every load, discharge and PV
field read zero. That is coherent - the bank's loads are wired direct, not
through the controller - but it means the zero fields are UNEXERCISED. A
daylight capture is needed before the PV and charging-state paths can be called
confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

Confidence = Literal["confirmed", "probable", "unverified"]
Kind = Literal["u16", "u32", "temp_hi", "temp_lo", "state", "bits"]


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    register: int
    kind: Kind
    scale: float
    unit: str
    confidence: Confidence
    help: str


CHARGING_STATE: Final[dict[int, str]] = {
    0: "deactivated",
    1: "activated",
    2: "mppt",
    3: "equalizing",
    4: "boost",
    5: "floating",
    6: "current_limiting",
}

FIELDS: Final[tuple[Field, ...]] = (
    Field(
        "battery_state_of_charge",
        0x0100,
        "u16",
        1,
        "percent",
        "confirmed",
        "Battery state of charge",
    ),
    Field("battery_voltage", 0x0101, "u16", 0.1, "volts", "confirmed", "Battery terminal voltage"),
    Field(
        "battery_charging_current",
        0x0102,
        "u16",
        0.01,
        "amperes",
        "confirmed",
        "Current flowing into the battery from the controller",
    ),
    Field(
        "controller_temperature",
        0x0103,
        "temp_hi",
        1,
        "celsius",
        "confirmed",
        "Charge controller internal temperature",
    ),
    Field(
        "battery_temperature",
        0x0103,
        "temp_lo",
        1,
        "celsius",
        "confirmed",
        "Battery temperature sensor",
    ),
    Field(
        "load_voltage",
        0x0104,
        "u16",
        0.1,
        "volts",
        "confirmed",
        "Voltage at the controller's load terminals",
    ),
    Field(
        "load_current",
        0x0105,
        "u16",
        0.01,
        "amperes",
        "confirmed",
        "Current drawn through the controller's load terminals",
    ),
    Field(
        "load_power",
        0x0106,
        "u16",
        1,
        "watts",
        "confirmed",
        "Power drawn through the controller's load terminals",
    ),
    Field("pv_voltage", 0x0107, "u16", 0.1, "volts", "confirmed", "Solar array voltage"),
    Field("pv_current", 0x0108, "u16", 0.01, "amperes", "confirmed", "Solar array current"),
    Field("pv_power", 0x0109, "u16", 1, "watts", "confirmed", "Solar array power"),
    Field(
        "register_0x010a",
        0x010A,
        "u16",
        1,
        "raw",
        "unverified",
        "Unidentified register; read zero across the whole night capture",
    ),
    Field(
        "battery_voltage_min_today",
        0x010B,
        "u16",
        0.1,
        "volts",
        "probable",
        "Lowest battery voltage seen today",
    ),
    Field(
        "battery_voltage_max_today",
        0x010C,
        "u16",
        0.1,
        "volts",
        "probable",
        "Highest battery voltage seen today",
    ),
    Field(
        "register_0x010d",
        0x010D,
        "u16",
        1,
        "raw",
        "unverified",
        "Unidentified. Read as 'peak charging current today' (scale 0.01) on the "
        "night of 2026-09-09, because 802 W / 56.53 A gives 14.19 V - a plausible "
        "absorption voltage. A daylight capture the next morning DISCONFIRMED that: "
        "the value did not reset overnight (5653 -> 5616) while "
        "charging_power_max_today did (802 -> 691 W); 56.16 A is inconsistent with "
        "the 22.76 A actually observed that morning; and the voltage ratio no longer "
        "lands anywhere sensible (12.30 V). The original match was a coincidence of "
        "the right shape. Exported raw",
    ),
    Field(
        "discharging_current_max_today",
        0x010E,
        "u16",
        0.01,
        "amperes",
        "unverified",
        "Peak discharging current today",
    ),
    Field(
        "charging_power_max_today",
        0x010F,
        "u16",
        1,
        "watts",
        "confirmed",
        "Peak charging power today",
    ),
    Field(
        "discharging_power_max_today",
        0x0110,
        "u16",
        1,
        "watts",
        "confirmed",
        "Peak discharging power today",
    ),
    Field(
        "charging_amp_hours_today",
        0x0111,
        "u16",
        1,
        "amp_hours",
        "confirmed",
        "Charge delivered to the battery today",
    ),
    Field(
        "discharging_amp_hours_today",
        0x0112,
        "u16",
        1,
        "amp_hours",
        "confirmed",
        "Charge drawn from the battery today",
    ),
    Field(
        "power_generation_today",
        0x0113,
        "u16",
        1,
        "watt_hours",
        "confirmed",
        "Energy generated today",
    ),
    Field(
        "power_consumption_today",
        0x0114,
        "u16",
        1,
        "watt_hours",
        "unverified",
        "Energy consumed through the load terminals today",
    ),
    Field(
        "total_operating_days",
        0x0115,
        "u16",
        1,
        "days",
        "unverified",
        "Days the controller has been in service",
    ),
    Field(
        "total_over_discharges",
        0x0116,
        "u16",
        1,
        "count",
        "unverified",
        "Cumulative over-discharge events",
    ),
    Field(
        "total_full_charges",
        0x0117,
        "u16",
        1,
        "count",
        "unverified",
        "Cumulative full-charge events",
    ),
    Field(
        "total_charging_amp_hours",
        0x0118,
        "u32",
        1,
        "amp_hours",
        "unverified",
        "Lifetime charge delivered to the battery",
    ),
    Field(
        "total_discharging_amp_hours",
        0x011A,
        "u32",
        1,
        "amp_hours",
        "unverified",
        "Lifetime charge drawn through the load terminals",
    ),
    Field(
        "power_generation_total",
        0x011C,
        "u32",
        1,
        "watt_hours",
        "confirmed",
        "Lifetime energy generated",
    ),
    Field(
        "power_consumption_total",
        0x011E,
        "u32",
        1,
        "watt_hours",
        "unverified",
        "Lifetime energy consumed through the load terminals",
    ),
    Field(
        "charging_state",
        0x0120,
        "state",
        1,
        "state",
        "confirmed",
        "Controller charging stage; see CHARGING_STATE",
    ),
    Field(
        "register_0x0121",
        0x0121,
        "bits",
        1,
        "bitfield",
        "unverified",
        "Sits immediately after charging state. First guessed to be fault/alarm bits, "
        "but across 30 captured frames it tracks charging_state exactly: state 0 -> 4 "
        "(11/11 frames), state 2 -> 1 (19/19 frames), no exceptions. That is a "
        "companion status word, not an independent fault register. Only two charging "
        "states have been observed, so this is suggestive, not proven. Exported raw "
        "and deliberately not decoded into named faults",
    ),
)

BY_NAME: Final[dict[str, Field]] = {f.name: f for f in FIELDS}

DEFAULT_EXPORTED: Final[frozenset[str]] = frozenset(
    f.name for f in FIELDS if f.confidence in ("confirmed", "probable")
)
