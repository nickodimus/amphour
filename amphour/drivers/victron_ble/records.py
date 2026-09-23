"""What each Victron Instant Readout record carries.

Field names are the shared vocabulary from amphour/fields.py, not private
names: a SmartShunt read over BLE reports `battery_voltage` in `volts`, the
same as the same shunt read over VE.Direct and the same as an EG4 pack. That
is what lets one query compare a cable reading against a radio reading of the
same battery, which is exactly how the battery monitor record below was
verified.

CONFIDENCE. Only the battery monitor record has been cross-checked against
real hardware, field for field, against the same device read over VE.Direct at
the same moment (2026-09-22, SmartShunt 500A/50mV):

    field                BLE        VE.Direct
    battery_voltage      52.630 V   52.623 V
    battery_state_of_charge 89.2 %  89.2 %
    consumed_amp_hours   -22.6      -22.609
    battery_current      -2.818 A   -2.952 A     (moving load, sampled apart)
    time_to_go_minutes   2936       2921         (same)

Those ship `confirmed`. Nothing else here has been near hardware, so nothing
else is decoded yet - an unverified guess exported as a number is worse than
an honest silence, because a wrong number gets trusted.
"""

from __future__ import annotations

from typing import Final

from ...fields import Field

BATTERY_MONITOR_FIELDS: Final[tuple[Field, ...]] = (
    Field(
        "battery_voltage",
        "volts",
        "confirmed",
        "Main battery voltage, cross-checked against the same shunt over VE.Direct",
    ),
    Field(
        "battery_current",
        "amperes",
        "confirmed",
        "Battery current, positive charging and negative discharging",
    ),
    Field(
        "battery_state_of_charge",
        "percent",
        "confirmed",
        "State of charge as the shunt itself computes it",
    ),
    Field(
        "consumed_amp_hours",
        "amp_hours",
        "confirmed",
        "Amp hours drawn since the last full charge, negative. 0.1 Ah resolution "
        "over BLE where VE.Direct carries more decimals",
    ),
    Field(
        "time_to_go_minutes",
        "minutes",
        "confirmed",
        "Estimated minutes of run time left at the present load; absent from the "
        "reading entirely when the shunt reports it as unknown",
    ),
    Field(
        "alarm_reason",
        "bitfield",
        "confirmed",
        "Active alarm bitfield; zero when no alarm is active",
    ),
    Field(
        "aux_input_type",
        "state",
        "probable",
        "Which auxiliary input the aux field describes (0 starter voltage, "
        "1 midpoint voltage, 2 temperature, 3 disabled). Reads 3 on a shunt "
        "with no aux wired, which is coherent but has not been exercised "
        "against a configured aux input",
    ),
)

FIELDS: Final[tuple[Field, ...]] = BATTERY_MONITOR_FIELDS

AUX_INPUT_TYPES: Final[dict[int, str]] = {
    0: "starter voltage",
    1: "midpoint voltage",
    2: "temperature",
    3: "disabled",
}
