"""The EG4 LifePower4 V2 register map, as one table.

Decoding, Prometheus metrics and InfluxDB fields are all generated from this
list — there is no second copy of these names anywhere, the same rule the Renogy
map documents.

CONFIDENCE — every field here is `confirmed`. The offsets and scalings were read
live from Sky's packs (Modbus FC 0x03, address 0x40/0x3F) on 2026-09-22 and
cross-checked field-for-field against the EG4 BMS Tools V1.0 readout for the same
instant (pack 53.6 V, SOC 76%, 16 cells ~3.35 V, temps 20-23 C, 100 Ah). So
these are cross-checked against an independent source — the bar for `confirmed`.

The `battery_*` names are the SHARED vocabulary (fields.py): an EG4
`battery_state_of_charge` lands on the same metric as the SmartShunt's, so one
query compares the pack BMS's SoC against the shunt's coulomb count.

Byte offsets are into the whole Modbus reply; the data area starts at byte 3, so
offset 3 is register 0x0000. The 16 cell voltages are a contiguous u16 block;
min/max/delta and the temperature aggregates are derived in decode.py.

NOT decoded here (backlog): the alarm / protection / warning status bitfields
(registers ~0x19-0x2E), the ~30 flags BMS Tools shows down the right of its
screen. They read all-clear on a healthy pack; mapping each bit wants a capture
that catches a real alarm, so it is a separate pass — do not guess them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from ...fields import Field

Kind = Literal["u16", "s16"]


@dataclass(frozen=True, slots=True)
class Register:
    """One EG4 holding value and how to turn its bytes into a Field value."""

    name: str
    offset: int          # byte offset into the reply frame
    kind: Kind
    scale: float
    field: Field


# --- scalar direct-read registers -----------------------------------------
# offset = 3 (data start) + register_index * 2.
REGISTERS: Final[tuple[Register, ...]] = (
    Register("battery_voltage", 3, "u16", 0.01,
             Field("battery_voltage", "volts", "confirmed",
                   "Pack terminal voltage reported by the EG4 BMS (reg 0x0000)")),
    Register("battery_current", 5, "s16", 0.01,
             Field("battery_current", "amperes", "confirmed",
                   "Pack current, + charging / - discharging, from the BMS (reg 0x0001)")),
    Register("battery_state_of_charge", 45, "u16", 1.0,
             Field("battery_state_of_charge", "percent", "confirmed",
                   "Pack SoC from the BMS (reg 0x0015; compare against the shunt)")),
    Register("battery_state_of_health", 47, "u16", 1.0,
             Field("battery_state_of_health", "percent", "confirmed",
                   "Pack state of health from the BMS (reg 0x0016)")),
    Register("battery_capacity", 49, "u16", 1.0,
             Field("battery_capacity", "amp_hours", "confirmed",
                   "Full/rated capacity reported by the BMS (reg 0x0017)")),
    Register("battery_remaining_capacity", 51, "u16", 1.0,
             Field("battery_remaining_capacity", "amp_hours", "confirmed",
                   "Remaining capacity reported by the BMS (reg 0x0018)")),
    Register("battery_temperature_pcb", 39, "s16", 1.0,
             Field("battery_temperature_pcb", "celsius", "confirmed",
                   "BMS board temperature (reg 0x0012)")),
    Register("battery_temperature_ambient", 41, "s16", 1.0,
             Field("battery_temperature_ambient", "celsius", "confirmed",
                   "Ambient temperature at the pack (reg 0x0013)")),
    Register("battery_temperature_cell", 43, "s16", 1.0,
             Field("battery_temperature_cell", "celsius", "confirmed",
                   "Cell-group temperature at the pack (reg 0x0014)")),
)

# --- cell block (derived in decode.py) ------------------------------------
# 16 series cells on a 48 V LiFePO4 pack, each a big-endian u16 in millivolts,
# contiguous from CELL_START (register 0x0002).
CELL_START: Final = 7            # byte offset of register 0x0002
CELL_COUNT: Final = 16
CELL_SCALE: Final = 0.001        # mV -> V

# Per-cell fields, emitted individually from the cell block in decode.py (so the
# wall can draw a 16-bar cell chart, the way the EG4 app shows it) alongside the
# min/max/delta summary below. Zero-padded so cell_voltage_01..16 sort in order.
CELL_FIELDS: Final[tuple[Field, ...]] = tuple(
    Field(f"cell_voltage_{i:02d}", "volts", "confirmed", f"Series cell {i} voltage")
    for i in range(1, CELL_COUNT + 1)
)

# --- pack temperatures (derived) ------------------------------------------
# Three cell-group temperatures at registers 0x0012..0x0014 (PCB, ambient, cell),
# big-endian s16, degrees C direct (no offset). battery_temperature is the
# warmest of them — the shared metric.
TEMP_OFFSETS: Final = (39, 41, 43)

# Fields produced by decode.py from the cell/temperature blocks. Shared names
# (battery_temperature) so the EG4 pack temp sits on the SmartShunt/Renogy metric.
DERIVED_FIELDS: Final[tuple[Field, ...]] = (
    Field("cell_voltage_min", "volts", "confirmed", "Lowest series-cell voltage"),
    Field("cell_voltage_max", "volts", "confirmed", "Highest series-cell voltage"),
    Field("cell_voltage_delta", "volts", "confirmed",
          "Max minus min cell voltage — the pack's balance, the number that matters most"),
    Field("battery_temperature", "celsius", "confirmed", "Warmest reported pack temperature"),
    Field("battery_temperature_min", "celsius", "confirmed", "Coolest reported pack temperature"),
)

# The full field tuple the driver declares (registry + sinks read this).
FIELDS: Final[tuple[Field, ...]] = (
    tuple(r.field for r in REGISTERS) + CELL_FIELDS + DERIVED_FIELDS
)
