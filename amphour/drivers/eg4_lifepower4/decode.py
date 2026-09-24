"""Turn a verified EG4 LifePower4 V2 frame into a Reading."""

from __future__ import annotations

from datetime import UTC, datetime

from ...reading import Reading
from . import protocol
from .registers import (
    CELL_COUNT,
    CELL_SCALE,
    CELL_START,
    REGISTERS,
    TEMP_OFFSETS,
    Register,
)


def _scalar(frame: bytes, r: Register) -> float:
    raw: float = protocol.u16(frame, r.offset) if r.kind == "u16" else protocol.s16(frame, r.offset)
    if r.scale == 1:
        # float(), not int: Reading.values is floats, and InfluxDB types a
        # Python int as an INTEGER field, which collides with the same field
        # written as a float by another device. (The Renogy driver hit exactly
        # this — see its decode.py note.)
        return float(raw)
    digits = 2 if r.scale == 0.01 else 6
    return round(raw * r.scale, digits)


def _cells(frame: bytes) -> list[float]:
    # All 16 cells are populated on a V2 pack; a zero here would be a genuinely
    # dead/shorted cell, so unlike the older map we do NOT silently drop zeros.
    return [
        round(protocol.u16(frame, CELL_START + i * 2) * CELL_SCALE, 3) for i in range(CELL_COUNT)
    ]


def _temps(frame: bytes) -> list[int]:
    # s16, degrees C direct (no -50 offset). 0 C is a valid reading, not a
    # missing sensor, so nothing is dropped.
    return [protocol.s16(frame, o) for o in TEMP_OFFSETS]


def decode(frame: bytes, *, address: int, source: str = "eg4", verify: bool = True) -> Reading:
    """Decode one pack's response into a Reading.

    Verifies length, function, byte count, address and CRC before touching a
    field, so a corrupt frame can never be mistaken for data. `verify=False`
    exists only for tests that deliberately feed corrupt frames.
    """
    if verify:
        protocol.verify_frame(frame, address=address)

    values: dict[str, float] = {r.name: _scalar(frame, r) for r in REGISTERS}

    cells = _cells(frame)
    for i, cell in enumerate(cells, start=1):
        values[f"cell_voltage_{i:02d}"] = cell
    values["cell_voltage_min"] = min(cells)
    values["cell_voltage_max"] = max(cells)
    values["cell_voltage_delta"] = round(max(cells) - min(cells), 3)

    temps = _temps(frame)
    values["battery_temperature"] = float(max(temps))
    values["battery_temperature_min"] = float(min(temps))

    return Reading(source=source, values=values, text={}, taken_at=datetime.now(UTC))
