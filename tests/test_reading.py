from __future__ import annotations

import pytest

from amphour.drivers.renogy import decode, protocol
from tests.conftest import OLD_TO_NEW


def test_decode_agrees_with_the_previous_app_on_every_captured_frame(all_frames):
    """The strongest check in the suite.

    `frame_hex` is the raw bytes the device put on the air. `old_app_log` is
    what the PREVIOUS implementation reported for that same instant, read
    straight out of its own log file by tools/extract_fixtures.py - which
    deliberately does not decode anything, so the two sides of this comparison
    come from different programs.

    Agreement means this rewrite reproduces behaviour that was actually in
    production, rather than a decoder agreeing with itself.
    """
    compared = 0
    for entry in all_frames:
        reading = decode(bytes.fromhex(entry["frame_hex"]))
        for old_name, new_name in OLD_TO_NEW.items():
            assert reading.values[new_name] == pytest.approx(entry["old_app_log"][old_name]), (
                f"{new_name} at {entry['wall_clock']}"
            )
            compared += 1
    assert compared == len(all_frames) * len(OLD_TO_NEW)


def test_decode_rejects_a_corrupt_frame_rather_than_returning_a_reading(night_capture):
    frame = bytearray(bytes.fromhex(night_capture[0]["frame_hex"]))
    frame[6] ^= 0xFF
    with pytest.raises(protocol.CrcError):
        decode(bytes(frame))


def test_scaled_values_are_not_polluted_by_float_error(night_capture):
    """115 * 0.1 is 11.500000000000002 in binary floating point."""
    reading = decode(bytes.fromhex(night_capture[0]["frame_hex"]))
    assert reading.values["battery_voltage"] == 11.5
    assert str(reading.values["battery_voltage"]) == "11.5"


def test_every_value_is_a_float_not_an_int(all_frames):
    """Scale-1 registers used to come out as bare ints. The InfluxDB line
    protocol types those as integer fields, while the VE.Direct driver emits
    floats under the same names, and Influx rejects the second type it sees
    for a field until the shard rolls. A dict[str, float] must mean it."""
    for entry in all_frames:
        reading = decode(bytes.fromhex(entry["frame_hex"]))
        for name, value in reading.values.items():
            assert type(value) is float, f"{name} is {type(value).__name__}"


def test_charging_state_maps_to_a_name(night_capture):
    reading = decode(bytes.fromhex(night_capture[0]["frame_hex"]))
    assert reading.text["charging_state"] == "deactivated"


def test_unknown_charging_state_is_reported_not_hidden(night_capture):
    frame = bytearray(bytes.fromhex(night_capture[0]["frame_hex"]))
    frame[68] = 0x63  # 99, not a documented stage
    frame[-2:] = protocol.crc16_modbus(bytes(frame[:-2])).to_bytes(2, "little")
    reading = decode(bytes(frame))
    assert reading.text["charging_state"] == "unknown_99"


def test_every_declared_field_is_produced(all_frames):
    from amphour.drivers.renogy.registers import REGISTERS

    reading = decode(bytes.fromhex(all_frames[0]["frame_hex"]))
    assert set(reading.values) == {r.name for r in REGISTERS}


def test_daily_min_max_voltage_brackets_the_live_reading(all_frames):
    """min <= now <= max, on every frame of both captures.

    This is the triangulation behind calling 0x010B/0x010C 'probable', and
    daylight data strengthened it: the pair reset overnight to today's own
    figures (11.3/11.8 -> 9.8/12.5), which is exactly what a daily min/max
    must do.

    A sibling hypothesis about 0x010D was disconfirmed by that same capture -
    it did NOT reset - so this test exists to catch a register-map change that
    breaks the one which held up.
    """
    for entry in all_frames:
        v = decode(bytes.fromhex(entry["frame_hex"])).values
        assert (
            v["battery_voltage_min_today"] <= v["battery_voltage"] <= v["battery_voltage_max_today"]
        ), entry["wall_clock"]


# --- daylight coverage ------------------------------------------------------
#
# The night capture had the array dark, so every PV and charging field was a
# legitimate zero and those code paths were never actually exercised. These
# tests assert the gap is closed, so that if the daylight fixture is ever lost
# or replaced with dark data the suite says so instead of quietly going back to
# testing only half the range.

PREVIOUSLY_DARK = (
    "pv_voltage",
    "pv_current",
    "pv_power",
    "battery_charging_current",
)


def test_a_daylight_fixture_exists(day_capture):
    assert day_capture, (
        "no daylight fixture: PV and charging paths are untested. "
        "Capture one with the array producing - see tools/extract_fixtures.py"
    )


def test_daylight_exercises_the_fields_the_dark_capture_could_not(day_capture):
    if not day_capture:
        pytest.skip("no daylight fixture")
    seen_nonzero = {name: False for name in PREVIOUSLY_DARK}
    for entry in day_capture:
        values = decode(bytes.fromhex(entry["frame_hex"])).values
        for name in PREVIOUSLY_DARK:
            if values[name] != 0:
                seen_nonzero[name] = True
    still_zero = [name for name, seen in seen_nonzero.items() if not seen]
    assert not still_zero, f"never non-zero in the daylight fixture: {still_zero}"


def test_daylight_exercises_a_real_charging_state(day_capture):
    """At night the controller reports 'deactivated' (0), which tells us
    nothing about whether the state mapping works."""
    if not day_capture:
        pytest.skip("no daylight fixture")
    states = {decode(bytes.fromhex(e["frame_hex"])).text["charging_state"] for e in day_capture}
    assert states - {"deactivated"}, f"only saw 'deactivated': {states}"
    assert not any(s.startswith("unknown_") for s in states), states


def test_pv_power_tracks_pv_voltage_times_current(day_capture):
    """The controller reports all three independently; they should agree.

    This is a genuine cross-check of three separate register offsets against
    each other, and it is only possible with the array producing.
    """
    if not day_capture:
        pytest.skip("no daylight fixture")
    for entry in day_capture:
        v = decode(bytes.fromhex(entry["frame_hex"])).values
        if v["pv_power"] == 0:
            continue
        implied = v["pv_voltage"] * v["pv_current"]
        # the controller rounds each field independently, so allow a few percent
        assert implied == pytest.approx(v["pv_power"], rel=0.05), (
            f"{entry['wall_clock']}: {v['pv_voltage']}V * {v['pv_current']}A "
            f"= {implied:.1f} but pv_power reports {v['pv_power']}"
        )
