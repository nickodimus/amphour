"""Mopeka Pro Check driver - decode and the quality gate.

The fixture holds nine real advertisements captured off demmastar's hci0 on
2026-09-22: six from the Studio puck on a 20lb vertical tank, three from the
Heat puck retired 2026-09-15 with a dead ultrasonic transducer.

What makes this a cross-check rather than a restatement of our own arithmetic
is APP_REPORTED below. Those three numbers were read off the Mopeka phone app
at the time of capture, by a different program that had never seen this code.
The decode has to land on them.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

from amphour.device import DeviceError
from amphour.drivers import available, build, fields_for
from amphour.drivers.mopeka import MopekaProCheckDevice
from amphour.drivers.mopeka.decode import ProtocolError, decode
from amphour.drivers.mopeka.fields import MANUFACTURER_ID

FIXTURE = Path(__file__).parent / "fixtures" / "mopeka-pro-check-2026-09-22.json"

STUDIO = "C7:BB:BC:8D:70:3E"
RETIRED = "F1:AE:79:54:4B:26"

# Read off the Mopeka app, 2026-09-22, same tank, same time as the capture.
APP_REPORTED = {"level_percent": 75, "temperature_f": 59, "battery": "full"}

TANK_20LB_V = (38, 254)


def _fixture(kind: str) -> list[bytes]:
    rows = json.loads(FIXTURE.read_text())
    return [bytes.fromhex(r["data_hex"]) for r in rows if r["kind"] == kind]


@pytest.fixture(scope="module")
def studio() -> list[bytes]:
    data = _fixture("studio")
    assert data, "studio fixture is missing - the suite needs it"
    return data


@pytest.fixture(scope="module")
def retired() -> list[bytes]:
    data = _fixture("retired")
    assert data, "retired-puck fixture is missing - it is the negative case"
    return data


# --- the cross-check --------------------------------------------------------


def test_decode_matches_what_the_app_reported(studio):
    adv = decode(studio[0], address=STUDIO)
    assert adv.level_percent(*TANK_20LB_V) == APP_REPORTED["level_percent"]
    assert adv.temperature_c * 9 / 5 + 32 == APP_REPORTED["temperature_f"]
    assert adv.battery_percent == 100  # the app said "full"


def test_every_studio_advertisement_agrees(studio):
    # All six were captured within a minute; propane does not move that fast,
    # so a disagreement would mean the decode is reading something unstable.
    levels = {decode(d, address=STUDIO).level_percent(*TANK_20LB_V) for d in studio}
    assert levels == {75}


def test_studio_reads_high_quality(studio):
    assert all(decode(d, address=STUDIO).quality == 3 for d in studio)


# --- the retired puck -------------------------------------------------------


def test_retired_puck_decodes_cleanly_but_reads_zero(retired):
    # The point of this puck: nothing about the advertisement is malformed.
    # It is a well formed reading of nothing, which is why a MAC filter and a
    # quality gate are both needed and neither alone is enough.
    for data in retired:
        adv = decode(data, address=RETIRED)
        assert adv.quality == 0
        assert adv.raw_level == 0
        assert adv.distance_mm == 0
        assert adv.battery_percent == 100  # a healthy cell behind a dead transducer


# --- payload integrity ------------------------------------------------------


def test_mac_tail_must_match_the_sender(studio):
    with pytest.raises(ProtocolError, match="MAC tail"):
        decode(studio[0], address=RETIRED)


def test_mac_tail_is_checked_only_when_an_address_is_given(studio):
    assert decode(studio[0]).mac_tail == bytes.fromhex("8d703e")


@pytest.mark.parametrize("length", [0, 9, 11, 26])
def test_wrong_length_is_rejected(length):
    with pytest.raises(ProtocolError, match="expected 10 bytes"):
        decode(bytes(length))


def test_unsupported_sensor_type_is_rejected(studio):
    # 0x05 is BOTTOM_UP_WATER: a real Mopeka type, but not one this driver's
    # LPG coefficients apply to, so it must be refused rather than mis-read.
    water = bytes([0x05]) + studio[0][1:]
    with pytest.raises(ProtocolError, match="unsupported sensor type"):
        decode(water, address=STUDIO)


# --- level geometry ---------------------------------------------------------


def test_level_is_clamped_at_both_ends(studio):
    adv = decode(studio[0], address=STUDIO)
    assert adv.level_percent(0, 10) == 100  # distance past full
    assert adv.level_percent(5000, 6000) == 0  # distance below empty


def test_same_reading_different_tank_gives_different_percent(studio):
    # The percentage is an interpretation of the distance, not a measurement.
    adv = decode(studio[0], address=STUDIO)
    assert adv.level_percent(38, 254) == 75
    assert adv.level_percent(38, 508) != 75
    assert adv.distance_mm == 202  # unchanged: this is the measured quantity


# --- the driver -------------------------------------------------------------


def _advertisement(address: str, payload: bytes):
    return (
        SimpleNamespace(address=address),
        SimpleNamespace(manufacturer_data={MANUFACTURER_ID: payload}),
    )


def _device(**kwargs) -> MopekaProCheckDevice:
    return MopekaProCheckDevice("studio", STUDIO, **kwargs)


def test_good_reading_publishes_level_and_distance(studio):
    device = _device()
    device._on_advertisement(*_advertisement(STUDIO, studio[0]))
    values = device._build_reading(device._latest).values
    assert values["tank_level"] == 75.0
    assert values["tank_distance"] == 202.0
    assert values["sensor_temperature"] == 15.0
    assert values["tank_ignored_reads"] == 0.0


def test_low_quality_omits_the_level_rather_than_publishing_zero(retired):
    # NEVER a NaN and never a false zero: an untrustworthy level is simply
    # absent, while the puck's own vitals keep publishing so it stays visibly
    # alive. A zero here would draw an empty tank.
    device = MopekaProCheckDevice("retired", RETIRED, min_quality="low")
    device._on_advertisement(*_advertisement(RETIRED, retired[0]))
    values = device._build_reading(device._latest).values
    assert "tank_level" not in values
    assert "tank_distance" not in values
    assert values["tank_read_quality"] == 0.0
    assert values["sensor_battery_percent"] == 100.0


def test_no_reading_ever_carries_a_nan(studio, retired):
    for address, payloads in ((STUDIO, studio), (RETIRED, retired)):
        device = MopekaProCheckDevice("puck", address, min_quality="low")
        for payload in payloads:
            device._on_advertisement(*_advertisement(address, payload))
            values = device._build_reading(device._latest).values
            assert not any(math.isnan(v) for v in values.values())


def test_ignored_reads_counts_up_and_resets(studio, retired):
    device = MopekaProCheckDevice("puck", RETIRED, min_quality="low")
    for payload in retired:
        device._on_advertisement(*_advertisement(RETIRED, payload))
    assert device._ignored_reads == len(retired)

    # A good read from the same address clears the run.
    good = bytes(studio[0][:5]) + bytes.fromhex("544b26") + bytes(studio[0][8:])
    device._on_advertisement(*_advertisement(RETIRED, good))
    assert device._ignored_reads == 0


def test_advertisements_from_other_pucks_are_ignored(studio, retired):
    device = _device()
    device._on_advertisement(*_advertisement(RETIRED, retired[0]))
    assert device._latest is None
    device._on_advertisement(*_advertisement(STUDIO, studio[0]))
    assert device._latest is not None


def test_undecodable_payload_does_not_become_a_reading(studio):
    device = _device()
    device._on_advertisement(*_advertisement(STUDIO, b"\x00\x01\x02"))
    assert device._latest is None


# --- configuration ----------------------------------------------------------


def test_unknown_tank_type_is_refused():
    with pytest.raises(ValueError, match="unknown tank_type"):
        _device(tank_type="55GAL_HORIZONTAL")


def test_half_a_custom_geometry_is_refused():
    with pytest.raises(ValueError, match="must be given together"):
        _device(empty_mm=40)


def test_custom_geometry_overrides_the_named_tank():
    device = _device(tank_type="20LB_V", empty_mm=10, full_mm=500)
    assert (device.empty_mm, device.full_mm) == (10, 500)


def test_inverted_geometry_is_refused():
    with pytest.raises(ValueError, match="greater than"):
        _device(empty_mm=300, full_mm=100)


def test_unknown_min_quality_is_refused():
    with pytest.raises(ValueError, match="min_quality"):
        _device(min_quality="excellent")


def test_driver_is_registered_and_builds():
    device = build("mopeka_ble", name="studio", address=STUDIO, tank_type="20LB_V")
    assert isinstance(device, MopekaProCheckDevice)
    assert device.address == STUDIO


def test_registered_fields_use_existing_unit_vocabulary():
    """This driver may introduce exactly one new unit string: millimeters.

    Complements tests/test_field_vocabulary.py rather than duplicating it.
    That one catches the SAME field name declared with two different units -
    the `amps` vs `amperes` collision of 4d636f1. It cannot see a second
    SPELLING arriving under a new name, because there is no conflict to find:
    a driver adding `tank_distance` in "mm" while the vocabulary already says
    "millimeters" passes it cleanly and leaves two words for one unit.

    So this test watches the other direction - new vocabulary - and it is
    deliberately narrow: only mopeka_ble, only one permitted addition. Adding
    a unit string here should require editing this line and thinking about it.
    """
    others = {
        f.unit for driver in available() if driver != "mopeka_ble" for f in fields_for(driver)
    }
    ours = {f.unit for f in fields_for("mopeka_ble")}
    assert ours - others == {"millimeters"}, (
        "mopeka_ble introduced an unexpected new unit string; if that is "
        "deliberate, say so here, and check no existing driver already has a "
        "word for the same quantity"
    )


@pytest.mark.parametrize(
    "bad", ["", "not-a-mac", "C7:BB:BC:8D:70", "C7BBBC8D703E", "ZZ:BB:BC:8D:70:3E"]
)
def test_malformed_address_is_refused_at_construction(bad):
    # The decoder parses this address on every advertisement, on the scanner's
    # callback. A typo must fail at startup, not raise inside the radio path.
    with pytest.raises(ValueError, match="not a BLE MAC"):
        MopekaProCheckDevice("studio", bad)


@pytest.mark.parametrize("failure", [BleakError("no adapter"), OSError("adapter went away")])
async def test_failing_scan_start_is_retryable(monkeypatch, failure):
    # Must be DeviceError: the supervisor stops a device permanently when it
    # raises anything the retry loop does not recognise, and an adapter reset
    # has to be survivable.
    class Boom:
        def __init__(self, *a, **k): ...
        async def start(self):
            raise failure

    monkeypatch.setattr("amphour.drivers.mopeka.BleakScanner", Boom)
    with pytest.raises(DeviceError, match="cannot start BLE scan"):
        await _device().open()
