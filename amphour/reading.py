"""Turning a verified frame into a Reading."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from typing import Final

from . import protocol
from .registers import CHARGING_STATE, FIELDS, Field


@dataclass(frozen=True, slots=True)
class Reading:
    """One complete poll of the controller.

    `values` is keyed by the field names in registers.FIELDS. Scaling has been
    applied; units are as declared in the field table.
    """

    values: dict[str, float] = dc_field(default_factory=dict)
    taken_at: datetime = dc_field(default_factory=lambda: datetime.now(UTC))

    @property
    def charging_state(self) -> str:
        """Human-readable charging stage, or 'unknown_<n>' for a code we don't know."""
        code = int(self.values.get("charging_state", -1))
        return CHARGING_STATE.get(code, f"unknown_{code}")

    def __str__(self) -> str:
        v = self.values
        return (
            f"{v.get('battery_voltage', float('nan')):.1f}V "
            f"{v.get('battery_state_of_charge', float('nan')):.0f}% "
            f"pv={v.get('pv_power', float('nan')):.0f}W "
            f"chg={v.get('battery_charging_current', float('nan')):.2f}A "
            f"[{self.charging_state}]"
        )


def _decode_field(frame: bytes, f: Field) -> float:
    if f.kind == "u16":
        raw: float = protocol.u16(frame, f.register)
    elif f.kind == "u32":
        raw = protocol.u32(frame, f.register)
    elif f.kind == "temp_hi":
        raw = protocol.temperature(protocol.u16(frame, f.register) >> 8)
    elif f.kind == "temp_lo":
        raw = protocol.temperature(protocol.u16(frame, f.register) & 0xFF)
    elif f.kind == "state":
        # charging stage is the low byte; the high byte is not the stage
        raw = protocol.u16(frame, f.register) & 0x00FF
    elif f.kind == "bits":
        raw = protocol.u16(frame, f.register)
    else:  # pragma: no cover - the Kind literal makes this unreachable
        raise ValueError(f"unhandled field kind {f.kind!r} for {f.name}")

    if f.scale == 1:
        return raw
    # 0.1 and 0.01 scaling on ints reintroduces binary float noise
    # (115 * 0.1 -> 11.500000000000002); round to the scale's own precision.
    digits = 1 if f.scale == 0.1 else 2 if f.scale == 0.01 else 6
    return round(raw * f.scale, digits)


def decode(frame: bytes, *, verify: bool = True) -> Reading:
    """Decode a device response into a Reading.

    Verifies length, function code, byte count and CRC first. `verify=False`
    exists only for tests that deliberately feed corrupt frames past the check.
    """
    if verify:
        protocol.verify_frame(frame)
    return Reading(values={f.name: _decode_field(frame, f) for f in FIELDS})


__all__: Final = ["Reading", "decode"]
