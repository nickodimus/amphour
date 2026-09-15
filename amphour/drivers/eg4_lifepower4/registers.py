"""The EG4 LifePower4 register map, as one table.

Decoding, Prometheus metrics and InfluxDB fields are all generated from this
list — there is no second copy of these names anywhere, per the same rule the
Renogy map documents.

CONFIDENCE — every field here is `unverified`. The byte offsets and scalings are
taken from the community `tuxntoast/eg4-ll` driver (which reads these packs live
on a Cerbo GX) and the EG4 LifePower4 communication protocol document, but
nothing here has been cross-checked against a frame captured from Sky's own two
packs. Because `unverified` fields are not exported by default, THIS DRIVER
PUBLISHES NOTHING until either:

  1. a live capture confirms each field against the EG4 app / EG4 BMS_Tools
     readout for the same instant, promoting it to `confirmed`, or
  2. config sets `include_unverified = true` to watch the raw map while you
     verify it — never build an alarm on an unverified field.

This is the same discipline the Renogy map used: honest about which parts are
still hypotheses. The `battery_*` names are the SHARED vocabulary (fields.py):
an EG4 `battery_state_of_charge` lands on the same metric as the SmartShunt's,
so one query compares the pack BMS's SoC against the shunt's coulomb count.

Byte offsets are into the whole Modbus reply; the data area starts at byte 3, so
offset 3 is register 0x0000. Cell voltages are a contiguous u16 block; min/max/
delta and the temperature aggregates are derived in decode.py, not read directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from ...fields import Field

Kind = Literal["u16", "s16", "u32", "s8"]


@dataclass(frozen=True, slots=True)
class Register:
    """One EG4 holding value and how to turn its bytes into a Field value."""

    name: str
    offset: int          # byte offset into the reply frame
    kind: Kind
    scale: float
    field: Field


# --- scalar direct-read registers -----------------------------------------
# Offsets from tuxntoast/eg4-ll read_cell_details(); scalings likewise. All
# `unverified` pending a capture from Sky's packs.
REGISTERS: Final[tuple[Register, ...]] = (
    Register("battery_voltage", 3, "u16", 0.01,
             Field("battery_voltage", "volts", "unverified",
                   "Pack terminal voltage reported by the EG4 BMS")),
    Register("battery_current", 5, "s16", 0.01,
             Field("battery_current", "amps", "unverified",
                   "Pack current, + charging / - discharging, from the BMS")),
    Register("battery_state_of_charge", 51, "u16", 1.0,
             Field("battery_state_of_charge", "percent", "unverified",
                   "Pack state of charge as the BMS computes it (compare vs the shunt)")),
    Register("consumed_amp_hours", 45, "u16", 1.0,
             Field("consumed_amp_hours", "amp_hours", "unverified",
                   "Remaining capacity reported by the BMS (offset 45; scale unconfirmed)")),
    Register("charge_cycles", 61, "u32", 1.0,
             Field("charge_cycles", "count", "unverified",
                   "BMS cycle count")),
    Register("controller_temperature", 39, "s16", 1.0,
             Field("controller_temperature", "celsius", "unverified",
                   "BMS/MOSFET temperature (offset 39; scale unconfirmed — may be tenths)")),
)

# --- cell block (derived in decode.py) ------------------------------------
# 16 series cells on a 48 V LiFePO4 pack. Each cell is one big-endian u16 in
# millivolts, contiguous from CELL_START. tuxntoast reads the count from the
# frame; 16 is hard-set here until a capture confirms the block length/offset.
CELL_START: Final = 7
CELL_COUNT: Final = 16
CELL_SCALE: Final = 0.001  # mV -> V

# --- pack temperatures (derived) ------------------------------------------
# Up to four s8 cell-group temperatures in the tail of the frame (bytes 69..72).
TEMP_OFFSETS: Final = (69, 70, 71, 72)

# Fields produced by decode.py from the cell/temperature blocks. Shared names
# (battery_temperature) so the EG4 pack temp sits on the SmartShunt/Renogy metric.
DERIVED_FIELDS: Final[tuple[Field, ...]] = (
    Field("cell_voltage_min", "volts", "unverified", "Lowest series-cell voltage"),
    Field("cell_voltage_max", "volts", "unverified", "Highest series-cell voltage"),
    Field("cell_voltage_delta", "volts", "unverified",
          "Max minus min cell voltage — the pack's balance, the number that matters most"),
    Field("battery_temperature", "celsius", "unverified",
          "Warmest reported cell-group temperature"),
    Field("battery_temperature_min", "celsius", "unverified",
          "Coolest reported cell-group temperature"),
)

# The full field tuple the driver declares (registry + sinks read this).
FIELDS: Final[tuple[Field, ...]] = tuple(r.field for r in REGISTERS) + DERIVED_FIELDS
