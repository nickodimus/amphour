from __future__ import annotations

import pytest

from amphour.drivers.renogy import protocol


def test_request_matches_the_captured_request_byte_for_byte():
    # Taken off the wire on 2026-09-09; the device answers this exact frame.
    assert protocol.build_read_request().hex() == "ff0301000022d1f1"


def test_crc_matches_every_captured_frame(all_frames):
    for entry in all_frames:
        frame = bytes.fromhex(entry["frame_hex"])
        sent = int.from_bytes(frame[-2:], "little")
        assert protocol.crc16_modbus(frame[:-2]) == sent, entry["wall_clock"]


def test_crc_of_known_vector():
    # The Modbus specification's own check value for "123456789".
    assert protocol.crc16_modbus(b"123456789") == 0x4B37


def test_verify_accepts_captured_frames(all_frames):
    for entry in all_frames:
        protocol.verify_frame(bytes.fromhex(entry["frame_hex"]))


def test_short_frame_is_rejected_not_zero_filled(night_capture):
    """The predecessor read past the end and got zeros, which look like data."""
    frame = bytes.fromhex(night_capture[0]["frame_hex"])
    with pytest.raises(protocol.ShortFrameError):
        protocol.verify_frame(frame[:40])


def test_corrupt_payload_is_rejected(night_capture):
    frame = bytearray(bytes.fromhex(night_capture[0]["frame_hex"]))
    frame[10] ^= 0xFF  # flip a data byte, leave the CRC alone
    with pytest.raises(protocol.CrcError):
        protocol.verify_frame(bytes(frame))


def test_corrupt_crc_is_rejected(night_capture):
    frame = bytearray(bytes.fromhex(night_capture[0]["frame_hex"]))
    frame[-1] ^= 0xFF
    with pytest.raises(protocol.CrcError):
        protocol.verify_frame(bytes(frame))


def test_modbus_exception_response_is_named(night_capture):
    frame = bytearray(bytes.fromhex(night_capture[0]["frame_hex"]))
    frame[1] = 0x83  # function 3 with the exception bit set
    with pytest.raises(protocol.UnexpectedResponseError, match="exception"):
        protocol.verify_frame(bytes(frame))


def test_wrong_byte_count_is_rejected(night_capture):
    frame = bytearray(bytes.fromhex(night_capture[0]["frame_hex"]))
    frame[2] = 0x20  # claim 32 bytes when the frame carries 68
    with pytest.raises(protocol.UnexpectedResponseError, match="byte count"):
        protocol.verify_frame(bytes(frame))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0x12, 18), (0x17, 23), (0x00, 0), (0x7F, 127), (0x85, -5), (0x80, 0), (0xFF, -127)],
)
def test_temperature_sign_bit(raw, expected):
    """Renogy puts the sign in bit 7 rather than using two's complement.

    The predecessor used the raw byte, so -5 C would have read as 133. We have
    no sub-zero capture yet, so this encodes the documented convention.
    """
    assert protocol.temperature(raw) == expected


def test_register_offsets_land_where_the_capture_says(night_capture):
    frame = bytes.fromhex(night_capture[0]["frame_hex"])
    # Anchors cross-checked by hand against the old app's log for this instant.
    assert protocol.u16(frame, 0x0100) == 16  # state of charge
    assert protocol.u16(frame, 0x0101) == 115  # 11.5 V
    assert protocol.u16(frame, 0x010F) == 802  # peak charge power today
    assert protocol.u32(frame, 0x011C) == 3890339  # lifetime generation
