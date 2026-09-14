"""Turn a verified Renogy frame into a Reading."""

from __future__ import annotations

from datetime import UTC, datetime

from ...reading import Reading
from . import protocol
from .registers import CHARGING_STATE, REGISTERS, Register


def _value(frame: bytes, r: Register) -> float:
    if r.kind == "u16":
        raw: float = protocol.u16(frame, r.register)
    elif r.kind == "u32":
        raw = protocol.u32(frame, r.register)
    elif r.kind == "temp_hi":
        raw = protocol.temperature(protocol.u16(frame, r.register) >> 8)
    elif r.kind == "temp_lo":
        raw = protocol.temperature(protocol.u16(frame, r.register) & 0xFF)
    elif r.kind == "state":
        # charging stage is the low byte; the high byte is not the stage
        raw = protocol.u16(frame, r.register) & 0x00FF
    elif r.kind == "bits":
        raw = protocol.u16(frame, r.register)
    else:  # pragma: no cover - the Kind literal makes this unreachable
        raise ValueError(f"unhandled register kind {r.kind!r} for {r.name}")

    if r.scale == 1:
        # float(), not the bare int. Reading.values promises floats, and the
        # InfluxDB line protocol types a Python int as an INTEGER field. The
        # shunt driver scales the same field names to float, and Influx holds
        # one type per field per shard: whichever device wrote first after a
        # shard rolled over won, and the other's points were rejected with a
        # type conflict for the rest of the week (2026-09-13, shard 860).
        return float(raw)
    # 0.1 and 0.01 scaling on ints reintroduces binary float noise
    # (115 * 0.1 -> 11.500000000000002); round to the scale's own precision.
    digits = 1 if r.scale == 0.1 else 2 if r.scale == 0.01 else 6
    return round(raw * r.scale, digits)


def charging_state_name(code: int) -> str:
    return CHARGING_STATE.get(code, f"unknown_{code}")


def decode(frame: bytes, *, source: str = "renogy", verify: bool = True) -> Reading:
    """Decode a device response into a Reading.

    Verifies length, function code, byte count and CRC before touching a single
    field, so a corrupt frame can never be mistaken for data. `verify=False`
    exists only for tests that deliberately feed corrupt frames past the check.
    """
    if verify:
        protocol.verify_frame(frame)
    values = {r.name: _value(frame, r) for r in REGISTERS}
    text = {"charging_state": charging_state_name(int(values["charging_state"]))}
    return Reading(source=source, values=values, text=text, taken_at=datetime.now(UTC))
