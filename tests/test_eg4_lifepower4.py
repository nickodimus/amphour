"""EG4 LifePower4 V2 driver — protocol framing and decode.

GOLDEN_MASTER / GOLDEN_SLAVE are real 55-byte Modbus replies captured from Sky's
two-pack bank on 2026-09-22 (addresses 0x40 and 0x3F), the same instant EG4 BMS
Tools showed pack ~53.3 V, SOC 75%, 16 cells ~3.33 V, temps 21-23 C, 100 Ah.
"""

from __future__ import annotations

import pytest

from amphour.device import DeviceError
from amphour.drivers import build, fields_for
from amphour.drivers.eg4_lifepower4 import _parse_packs
from amphour.drivers.eg4_lifepower4.decode import decode
from amphour.drivers.eg4_lifepower4.protocol import (
    CrcError,
    ShortFrameError,
    UnexpectedResponseError,
    build_read_request,
    verify_frame,
)

GOLDEN_MASTER = bytes.fromhex(
    "40033214d4ff4f0d030d060d050d060d040d060d040d060d050d050d040d050d050d060d050d05"
    "001700170015004b00640064004c39f5"
)
GOLDEN_SLAVE = bytes.fromhex(
    "3f033214d4ff570d030d060d050d060d040d060d040d060d050d050d040d050d050d060d050d05"
    "001700170015004b00640064004c5f08"
)


def test_read_request_is_well_formed_modbus():
    from amphour.drivers.eg4_lifepower4.protocol import crc16_modbus

    # address 0x40, FC 3, start 0x0000, 25 words, then a self-consistent CRC
    req = build_read_request(0x40)
    assert req[:6] == bytes([0x40, 0x03, 0x00, 0x00, 0x00, 0x19])
    assert len(req) == 8
    assert int.from_bytes(req[-2:], "little") == crc16_modbus(req[:-2])


def test_verify_accepts_captured_frames():
    verify_frame(GOLDEN_MASTER, address=0x40)
    verify_frame(GOLDEN_SLAVE, address=0x3F)


def test_verify_rejects_wrong_address():
    with pytest.raises(UnexpectedResponseError):
        verify_frame(GOLDEN_MASTER, address=0x3F)


def test_verify_rejects_corrupt_crc():
    bad = bytearray(GOLDEN_MASTER)
    bad[10] ^= 0xFF
    with pytest.raises(CrcError):
        verify_frame(bytes(bad), address=0x40)


def test_verify_rejects_short_frame():
    with pytest.raises(ShortFrameError):
        verify_frame(GOLDEN_MASTER[:-4], address=0x40)


def test_decode_master_matches_bms_tools():
    r = decode(GOLDEN_MASTER, address=0x40, source="eg4-master")
    v = r.values
    assert r.source == "eg4-master"
    assert v["battery_voltage"] == 53.32
    assert v["battery_current"] == -1.77           # + charge / - discharge
    assert v["battery_state_of_charge"] == 75.0
    assert v["battery_state_of_health"] == 100.0
    assert v["battery_capacity"] == 100.0
    assert v["battery_remaining_capacity"] == 76.0
    assert v["cell_voltage_min"] == 3.331
    assert v["cell_voltage_max"] == 3.334
    assert v["cell_voltage_delta"] == 0.003
    assert v["battery_temperature_pcb"] == 23.0
    assert v["battery_temperature_ambient"] == 23.0
    assert v["battery_temperature_cell"] == 21.0
    assert v["battery_temperature"] == 23.0        # warmest of the three
    assert v["battery_temperature_min"] == 21.0
    # per-cell fields, emitted individually as well as summarised
    assert v["cell_voltage_01"] == 3.331
    assert v["cell_voltage_02"] == 3.334
    assert v["cell_voltage_16"] == 3.333
    assert sum(1 for k in v if k.startswith("cell_voltage_") and k[-2:].isdigit()) == 16


def test_decode_reports_all_declared_fields():
    # every declared field is actually produced by decode, and vice versa
    r = decode(GOLDEN_SLAVE, address=0x3F, source="eg4-slave")
    declared = {f.name for f in fields_for("eg4_lifepower4")}
    assert set(r.values) == declared


def test_driver_registered():
    from amphour.drivers import available

    assert "eg4_lifepower4" in available()


def test_parse_packs_from_list():
    spec = [{"address": 64, "label": "master"}, {"address": 63, "label": "slave"}]
    packs = _parse_packs(spec, None, "eg4")
    assert [(p.address, p.label) for p in packs] == [(64, "master"), (63, "slave")]


def test_parse_packs_single_address():
    packs = _parse_packs(None, 64, "eg4")
    assert [(p.address, p.label) for p in packs] == [(64, "eg4")]


def test_parse_packs_rejects_duplicate_label():
    spec = [{"address": 64, "label": "x"}, {"address": 63, "label": "x"}]
    with pytest.raises(DeviceError):
        _parse_packs(spec, None, "eg4")


def test_build_makes_a_two_pack_device():
    d = build(
        "eg4_lifepower4",
        name="eg4",
        port="/dev/null",
        packs=[{"address": 64, "label": "master"}, {"address": 63, "label": "slave"}],
    )
    assert [(p.address, p.label) for p in d.packs] == [(64, "master"), (63, "slave")]
