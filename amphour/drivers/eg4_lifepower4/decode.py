"""Turn a verified EG4 LifePower4 frame into a Reading."""

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
    if r.kind == "u16":
        raw: float = protocol.u16(frame, r.offset)
    elif r.kind == "s16":
        raw = protocol.s16(frame, r.offset)
    elif r.kind == "u32":
        raw = protocol.u32(frame, r.offset)
    elif r.kind == "s8":
        raw = protocol.s8(frame, r.offset)
    else:  # pragma: no cover - Kind literal makes this unreachable
        raise ValueError(f"unhandled kind {r.kind!r} for {r.name}")
    if r.scale == 1:
        # float(), not int: Reading.values is floats, and InfluxDB types a
        # Python int as an INTEGER field, which collides with the same field
        # written as a float by another device. (The Renogy driver hit exactly
        # this — see its decode.py note.)
        return float(raw)
    digits = 1 if r.scale == 0.1 else 2 if r.scale == 0.01 else 6
    return round(raw * r.scale, digits)


def _cells(frame: bytes) -> list[float]:
    cells = []
    for i in range(CELL_COUNT):
        mv = protocol.u16(frame, CELL_START + i * 2)
        if mv:  # a zero cell is an unpopulated slot, not a dead cell at 0 V
            cells.append(round(mv * CELL_SCALE, 3))
    return cells


def _temps(frame: bytes) -> list[int]:
    temps = [protocol.s8(frame, o) for o in TEMP_OFFSETS]
    # A 0 in the tail slots means "no sensor there", not 0 C — drop them, but
    # keep at least the first so a genuinely-freezing pack still reports.
    return [temps[0]] + [t for t in temps[1:] if t != 0]


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
    if cells:
        values["cell_voltage_min"] = min(cells)
        values["cell_voltage_max"] = max(cells)
        values["cell_voltage_delta"] = round(max(cells) - min(cells), 3)

    temps = _temps(frame)
    if temps:
        values["battery_temperature"] = float(max(temps))
        values["battery_temperature_min"] = float(min(temps))

    return Reading(source=source, values=values, text={}, taken_at=datetime.now(UTC))
