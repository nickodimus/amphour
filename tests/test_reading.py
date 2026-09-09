from __future__ import annotations

import pytest

from amphour import protocol
from amphour.reading import decode
from tests.conftest import OLD_TO_NEW


def test_decode_agrees_with_the_previous_app_on_every_captured_frame(all_frames):
    """The strongest check in the suite.

    The fixture carries values the OLD implementation produced from the same
    frame at the same instant. Agreement across all mapped fields means this
    rewrite reproduces the behaviour that was actually in production, rather
    than merely being self-consistent.
    """
    compared = 0
    for entry in all_frames:
        reading = decode(bytes.fromhex(entry["frame_hex"]))
        for old_name, new_name in OLD_TO_NEW.items():
            assert reading.values[new_name] == pytest.approx(entry["expected"][old_name]), (
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


def test_charging_state_maps_to_a_name(night_capture):
    reading = decode(bytes.fromhex(night_capture[0]["frame_hex"]))
    assert reading.charging_state == "deactivated"


def test_unknown_charging_state_is_reported_not_hidden(night_capture):
    frame = bytearray(bytes.fromhex(night_capture[0]["frame_hex"]))
    frame[68] = 0x63  # 99, not a documented stage
    frame[-2:] = protocol.crc16_modbus(bytes(frame[:-2])).to_bytes(2, "little")
    reading = decode(bytes(frame))
    assert reading.charging_state == "unknown_99"


def test_every_declared_field_is_produced(all_frames):
    from amphour.registers import FIELDS

    reading = decode(bytes.fromhex(all_frames[0]["frame_hex"]))
    assert set(reading.values) == {f.name for f in FIELDS}


def test_probable_fields_are_physically_coherent(night_capture):
    """min <= now <= max, and peak power over peak current is a sane voltage.

    This is the triangulation that promoted these three from guesses to
    'probable'. If a future register-map change breaks it, that should fail.
    """
    reading = decode(bytes.fromhex(night_capture[0]["frame_hex"])).values
    assert (
        reading["battery_voltage_min_today"]
        <= reading["battery_voltage"]
        <= reading["battery_voltage_max_today"]
    )
    implied_voltage = reading["charging_power_max_today"] / reading["charging_current_max_today"]
    assert 12.0 < implied_voltage < 16.0, implied_voltage
