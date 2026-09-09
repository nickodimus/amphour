"""Renogy BT-1 wire protocol: Modbus RTU framed over a BLE GATT characteristic.

Everything here is pure and synchronous. It takes bytes and returns a Reading,
which makes the whole protocol testable against captured frames without a radio.

Frame layout, confirmed against a live BT-TH-F265000C on 2026-09-09
(see tests/fixtures/night-2026-09-09.json for the capture these came from):

    request   ff 03 0100 0022 d1f1              8 bytes
              |  |  |    |    +-- CRC16/MODBUS, low byte first
              |  |  |    +------- 34 words
              |  |  +------------ start register 0x0100
              |  +--------------- function 3, read holding registers
              +------------------ device id 255 (broadcast)

    response  ff 03 44 <68 data bytes> <crc>   73 bytes
                    +-- byte count, 34 words * 2

The response arrives as a single ATT notification. It is fragmented into three
ACL packets on the air (27/27/26 bytes) but BlueZ reassembles before the value
reaches us, so callers see one 73-byte payload.
"""

from __future__ import annotations

from typing import Final

DEVICE_ID: Final = 255
FUNCTION_READ_HOLDING: Final = 3
START_REGISTER: Final = 0x0100
WORD_COUNT: Final = 34

HEADER_LEN: Final = 3  # device id, function, byte count
CRC_LEN: Final = 2
EXPECTED_FRAME_LEN: Final = HEADER_LEN + WORD_COUNT * 2 + CRC_LEN  # 73


class ProtocolError(Exception):
    """Base for every malformed-frame condition."""


class ShortFrameError(ProtocolError):
    """Frame is not the length the register count implies.

    The predecessor to this app read past the end of short buffers and got
    zeros back, which is indistinguishable from a genuine reading of zero.
    A truncated frame is an error, not a measurement.
    """


class CrcError(ProtocolError):
    """The device's own checksum does not match the payload."""


class UnexpectedResponseError(ProtocolError):
    """Device id or function code is not what we asked for."""


def crc16_modbus(data: bytes) -> int:
    """CRC16/MODBUS: init 0xFFFF, poly 0xA001 (reversed 0x8005), no final xor.

    Verified against 11 real device frames plus the request frame; see
    tests/test_protocol.py::test_crc_matches_every_captured_frame.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_read_request(
    device_id: int = DEVICE_ID,
    function: int = FUNCTION_READ_HOLDING,
    register: int = START_REGISTER,
    words: int = WORD_COUNT,
) -> bytes:
    """Build a Modbus RTU read request. CRC is appended low byte first."""
    body = bytes([device_id, function, register >> 8, register & 0xFF, words >> 8, words & 0xFF])
    return body + crc16_modbus(body).to_bytes(2, "little")


def verify_frame(frame: bytes, *, words: int = WORD_COUNT) -> None:
    """Raise if the frame is truncated, mis-addressed, or fails its checksum.

    Called before any field is decoded, so a corrupt frame can never be
    mistaken for data.
    """
    expected_len = HEADER_LEN + words * 2 + CRC_LEN
    if len(frame) != expected_len:
        raise ShortFrameError(f"expected {expected_len} bytes for {words} words, got {len(frame)}")

    if frame[1] != FUNCTION_READ_HOLDING:
        # bit 7 set on the function code is Modbus' exception response
        if frame[1] & 0x80:
            raise UnexpectedResponseError(
                f"device returned Modbus exception, function 0x{frame[1]:02x}, code {frame[2]}"
            )
        raise UnexpectedResponseError(f"unexpected function 0x{frame[1]:02x}")

    declared = frame[2]
    if declared != words * 2:
        raise UnexpectedResponseError(
            f"byte count {declared} does not match {words} words ({words * 2})"
        )

    sent = int.from_bytes(frame[-CRC_LEN:], "little")
    calculated = crc16_modbus(frame[:-CRC_LEN])
    if sent != calculated:
        raise CrcError(f"crc mismatch: frame says 0x{sent:04x}, computed 0x{calculated:04x}")


# --- register access -------------------------------------------------------
#
# Registers start at 0x0100 and the data area starts at byte 3, so register
# 0x0100+k lives at byte offset 3 + 2k.


def _offset(register: int) -> int:
    return HEADER_LEN + (register - START_REGISTER) * 2


def u16(frame: bytes, register: int) -> int:
    o = _offset(register)
    return int.from_bytes(frame[o : o + 2], "big")


def u32(frame: bytes, register: int) -> int:
    """Two consecutive registers, high word first."""
    o = _offset(register)
    return int.from_bytes(frame[o : o + 4], "big")


def temperature(raw_byte: int) -> int:
    """Renogy encodes temperature sign in bit 7 rather than as two's complement.

    The predecessor used the raw byte, which reads -5 C as 133. We have no
    sub-zero capture to confirm this against, so it is implemented from the
    documented convention and flagged here rather than silently trusted.
    """
    magnitude = raw_byte & 0x7F
    return -magnitude if raw_byte & 0x80 else magnitude
