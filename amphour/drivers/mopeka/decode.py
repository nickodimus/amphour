"""Decoding one Mopeka Pro Check advertisement.

Ten bytes of manufacturer data under company id 0x0059. The layout below was
read off real advertisements captured from demmastar on 2026-09-22 (see
tests/fixtures/mopeka-pro-check-2026-09-22.json) and checked field by field
against the ESPHome `mopeka_pro_check` component, which is an independent
implementation of the same decode:

    byte 0      sensor type
    byte 1      battery, low 7 bits, in units of 1/32 V
    byte 2      temperature, low 7 bits, degrees C biased by +40
    byte 3-4    raw level in the low 14 bits, read quality in the top 2 bits
    byte 5-7    the low three octets of the sender's MAC
    byte 8-9    not used by the reference decode

Bytes 8 and 9 drift between consecutive advertisements and the reference
implementation ignores them entirely, so this does too. Guessing at them would
add a field nobody can check.

Nothing here touches bleak, so the whole decode is testable from captured
bytes without a radio.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .fields import (
    ADVERTISEMENT_LEN,
    LPG_COEFFICIENTS,
    SENSOR_TYPES,
)

# CR2032 scale used by the reference implementation: 2.20V reads empty and
# 2.85V reads full.
_BATTERY_EMPTY_V: Final = 2.2
_BATTERY_RANGE_V: Final = 0.65


class ProtocolError(Exception):
    """An advertisement was not a Pro Check reading this driver can decode."""


@dataclass(frozen=True, slots=True)
class Advertisement:
    """One decoded Pro Check advertisement.

    `raw_level` is the uncalibrated echo count. It becomes a distance only
    once the puck's own temperature is applied, which is why distance is a
    method here rather than a field decoded in isolation.
    """

    sensor_type: int
    battery_volts: float
    battery_percent: int
    temperature_c: int
    raw_level: int
    quality: int
    mac_tail: bytes

    @property
    def distance_mm(self) -> int:
        """Distance to the liquid surface, temperature corrected.

        Truncated to a whole millimetre, matching the reference
        implementation's cast rather than rounding, so the two agree exactly
        on values that straddle a boundary.
        """
        a, b, c = LPG_COEFFICIENTS
        t = self.temperature_c + 40  # back to the raw biased value the coefficients expect
        return int(self.raw_level * (a + b * t + c * t * t))

    def level_percent(self, empty_mm: int, full_mm: int) -> int:
        """Fill percentage for a tank of the given geometry."""
        if self.distance_mm >= full_mm:
            return 100
        if self.distance_mm > empty_mm:
            return int((100.0 / (full_mm - empty_mm)) * (self.distance_mm - empty_mm))
        return 0


def decode(data: bytes, *, address: str | None = None) -> Advertisement:
    """Decode manufacturer data, raising ProtocolError if it is not ours.

    When `address` is given, the three MAC octets embedded in the payload are
    checked against it. That check is free and it catches the one failure a
    MAC filter alone cannot: a malformed or mis-attributed advertisement that
    arrives under the right address.
    """
    if len(data) != ADVERTISEMENT_LEN:
        raise ProtocolError(f"expected {ADVERTISEMENT_LEN} bytes, got {len(data)}")

    sensor_type = data[0]
    if sensor_type not in SENSOR_TYPES:
        raise ProtocolError(
            f"unsupported sensor type 0x{sensor_type:02x}; "
            f"this driver decodes bottom-up LPG pucks only "
            f"({', '.join(f'0x{t:02x}' for t in sorted(SENSOR_TYPES))})"
        )

    mac_tail = bytes(data[5:8])
    if address is not None:
        expected = bytes.fromhex(address.replace(":", ""))[3:]
        if mac_tail != expected:
            raise ProtocolError(
                f"payload carries MAC tail {mac_tail.hex(':')} "
                f"but the advertisement came from {address}"
            )

    volts = (data[1] & 0x7F) / 32.0
    percent = (volts - _BATTERY_EMPTY_V) / _BATTERY_RANGE_V * 100.0

    raw = (data[4] * 256) + data[3]

    return Advertisement(
        sensor_type=sensor_type,
        battery_volts=round(volts, 3),
        battery_percent=max(0, min(100, int(percent))),
        temperature_c=(data[2] & 0x7F) - 40,
        raw_level=raw & 0x3FFF,
        quality=data[4] >> 6,
        mac_tail=mac_tail,
    )
