"""Mopeka Pro Check field table, tank geometry and the read-quality scale.

Field NAMES are the shared vocabulary described in amphour/fields.py. Propane
is new vocabulary in this repo, so these names are the ones later drivers will
have to match.

Two naming decisions worth the words:

`sensor_battery_voltage` is deliberately NOT `battery_voltage`. The puck runs
on a CR2032; `battery_voltage` everywhere else in this repo means the house
bank. They share a unit and nothing else, and merging a 3V coin cell into the
same metric as a 12V bank is exactly the confusion fields.py warns about. This
follows the precedent already set for `battery_current` in the VE.Direct
driver: same units, different quantities, different names.

`tank_distance` is published alongside `tank_level` because the puck measures
distance and NOTHING ELSE. The percentage is an interpretation of that
distance through a tank's empty/full geometry - change the tank type and the
same physical reading becomes a different percentage. Publishing only the
percentage would throw away the measurement and keep the opinion.

Confidences were set on 2026-09-22 against two independent sources: the
ESPHome `mopeka_pro_check` component's own decode (2026.8.2, read from source)
and the Mopeka phone app read off the Studio tank at the same time. The app
reported 75%, 59F and a full battery; this decode of the advertisement
captured minutes earlier gives 75%, 15C and 100%. Fields that the app showed a
number for are `confirmed`; fields that only triangulate against those are
`probable`.
"""

from __future__ import annotations

from typing import Final

from ...fields import Field

MANUFACTURER_ID: Final = 0x0059
ADVERTISEMENT_LEN: Final = 10

# Sensor type byte. Values from the ESPHome component's SensorType enum; the
# four accepted here are the four IT accepts for a Pro Check, not the whole
# enum - TOP_DOWN_AIR_ABOVE (0x04) and BOTTOM_UP_WATER (0x05) are defined there
# but rejected, because the level maths below is bottom-up LPG only.
SENSOR_TYPES: Final[dict[int, str]] = {
    0x03: "STANDARD_BOTTOM_UP",
    0x06: "LIPPERT_BOTTOM_UP",
    0x08: "PLUS_BOTTOM_UP",
    0x0C: "PRO_UNIVERSAL",
}

# Read quality, from the top two bits of byte 4. A puck that cannot see the
# liquid surface reports ZERO, which is how a dead ultrasonic transducer
# announces itself: the retired Heat puck advertises perfectly and reads ZERO
# forever. See tests/fixtures/mopeka-pro-check-2026-09-22.json.
QUALITY_ZERO: Final = 0
QUALITY_LOW: Final = 1
QUALITY_MEDIUM: Final = 2
QUALITY_HIGH: Final = 3

QUALITY_NAMES: Final[dict[int, str]] = {
    QUALITY_ZERO: "zero",
    QUALITY_LOW: "low",
    QUALITY_MEDIUM: "medium",
    QUALITY_HIGH: "high",
}
QUALITY_BY_NAME: Final[dict[str, int]] = {v: k for k, v in QUALITY_NAMES.items()}

# Empty/full ultrasonic distance in mm per standard tank, from the ESPHome
# component's CONF_SUPPORTED_TANKS_MAP. "Empty" is not zero: there is always
# some distance between the puck and the bottom of the liquid column.
TANKS: Final[dict[str, tuple[int, int]]] = {
    "20LB_V": (38, 254),
    "30LB_V": (38, 381),
    "40LB_V": (38, 508),
    "EUROPE_6KG": (38, 336),
    "EUROPE_11KG": (38, 366),
    "EUROPE_14KG": (38, 467),
}

# Propane coefficients, quoted in the ESPHome source as "magic numbers provided
# by Mopeka". They convert the raw echo count to millimetres and are strongly
# temperature dependent - the speed of sound in LPG vapour is not a constant,
# which is why the puck reports its own temperature at all.
LPG_COEFFICIENTS: Final[tuple[float, float, float]] = (0.573045, -0.002822, -0.00000535)

FIELDS: Final[tuple[Field, ...]] = (
    Field(
        "tank_level",
        "percent",
        "confirmed",
        "Tank fill, derived from the measured distance and the tank's "
        "empty/full geometry. Absent rather than zero when the read quality "
        "is too low to trust",
    ),
    Field(
        "tank_distance",
        "millimeters",
        "probable",
        "Ultrasonic distance from the puck to the liquid surface. This is the "
        "quantity actually measured; tank_level is derived from it",
    ),
    Field(
        "tank_read_quality",
        "enum",
        "probable",
        "Signal quality 0-3 (zero, low, medium, high). A puck that cannot see "
        "the liquid surface reads zero",
    ),
    Field(
        "tank_ignored_reads",
        "count",
        "probable",
        "Consecutive advertisements rejected for low read quality, reset by "
        "the first good one. A number that climbs and never resets means a "
        "puck that is mounted wrong, or a dead transducer",
    ),
    Field(
        "sensor_temperature",
        "celsius",
        "confirmed",
        "Temperature reported by the puck itself, and the input to the distance calculation",
    ),
    Field(
        "sensor_battery_voltage",
        "volts",
        "probable",
        "CR2032 cell voltage in the puck. Not the house bank - see this module's docstring",
    ),
    Field(
        "sensor_battery_percent",
        "percent",
        "confirmed",
        "CR2032 charge, scaled from 2.20V empty to 2.85V full and clamped",
    ),
)
