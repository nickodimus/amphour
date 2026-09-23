"""Decode a decrypted Victron Instant Readout record into a Reading.

The battery monitor record is 15 bytes and is NOT byte aligned past the first
eight: current, consumed amp hours and state of charge are bit fields packed
across byte boundaries, little-endian.

    [0:2]   u16  time to go, minutes          0xFFFF means "unknown"
    [2:4]   i16  voltage, 0.01 V
    [4:6]   u16  alarm bitfield
    [6:8]   u16  aux value (meaning set by aux input type below)
    [8:11]  u24  bits 0-1  aux input type
                 bits 2-23 current, signed 22-bit, 0.001 A
    [11:15] u32  bits 0-19  consumed amp hours, 0.1 Ah (magnitude; drawn is negative)
                 bits 20-29 state of charge, 0.1 %

The current field is the easy one to get wrong: it is the UPPER 22 bits of the
u24, with the aux input type in the low two. Reading it as the low 22 bits
gives a plausible-looking number with the wrong magnitude - it produced
-12.353 A against a true -2.9 A during development, which is exactly the kind
of wrong that survives a casual glance.

A sentinel means the device is not reporting that field, not that the field is
zero. Sentinels are dropped from the reading rather than exported, so a gauge
goes absent instead of publishing a fiction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from ...reading import Reading
from . import protocol
from .records import AUX_INPUT_TYPES

BATTERY_MONITOR_LEN: Final = 15
TTG_UNKNOWN: Final = 0xFFFF
CURRENT_UNKNOWN: Final = 0x3FFFFF
SOC_UNKNOWN: Final = 0x3FF
CONSUMED_UNKNOWN: Final = 0xFFFFF


def _signed(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def decode_battery_monitor(plaintext: bytes, *, source: str) -> Reading:
    """Decode a battery monitor record (type 0x02)."""
    if len(plaintext) < BATTERY_MONITOR_LEN:
        raise protocol.ProtocolError(
            f"battery monitor record is {len(plaintext)} bytes, need {BATTERY_MONITOR_LEN}"
        )

    values: dict[str, float] = {}
    text: dict[str, str] = {}

    ttg = int.from_bytes(plaintext[0:2], "little")
    if ttg != TTG_UNKNOWN:
        values["time_to_go_minutes"] = float(ttg)

    values["battery_voltage"] = int.from_bytes(plaintext[2:4], "little", signed=True) / 100.0
    values["alarm_reason"] = float(int.from_bytes(plaintext[4:6], "little"))

    packed = int.from_bytes(plaintext[8:11], "little")
    aux_type = packed & 0x03
    values["aux_input_type"] = float(aux_type)
    text["aux_input_type"] = AUX_INPUT_TYPES.get(aux_type, f"unknown ({aux_type})")

    current = (packed >> 2) & 0x3FFFFF
    if current != CURRENT_UNKNOWN:
        values["battery_current"] = _signed(current, 22) / 1000.0

    tail = int.from_bytes(plaintext[11:15], "little")
    consumed = tail & 0xFFFFF
    if consumed != CONSUMED_UNKNOWN:
        # Reported as a magnitude; amp hours DRAWN are negative, matching what
        # the VE.Direct driver publishes for the same shunt.
        values["consumed_amp_hours"] = -(consumed / 10.0)

    soc = (tail >> 20) & 0x3FF
    if soc != SOC_UNKNOWN:
        values["battery_state_of_charge"] = soc / 10.0

    return Reading(source=source, values=values, text=text, taken_at=datetime.now(UTC))
