"""EG4 LifePower4 V2 wire protocol: standard Modbus RTU over RS485.

Pure and synchronous — takes bytes, returns bytes/ints — so the whole frame
layer is testable against a captured reply without a battery on the bench.

VERIFIED 2026-09-22 against Sky's packs. The V2 speaks STANDARD Modbus RTU
(function 0x03, CRC16/MODBUS). This was confirmed by a man-in-the-middle capture
of EG4 BMS Tools V1.0 talking to the pack — its poll was literally
`40 03 00 69 00 17 da c9` (read model, addr 0x40) — and the live register block
was then matched field-for-field against the BMS Tools readout. (The older
LifePower4 `7E..0D` ASCII protocol and the EG4-LL Modbus map are BOTH wrong for
this battery.)

Bus:
    RS485 on the battery's RJ45, pins 1(B)/2(A), 9600 baud, 8N1.
    Master / lone pack answers at address 0x40 (64). A chained slave answers at
    0x3F (63) with the master relaying over the battery-comm link — so a slave
    reply lands a little slower; give the read a generous timeout.

Frame layout (live-data read, 25 holding registers from 0x0000):
    request   AD 03 0000 0019 <crc>            8 bytes
              |  |  |    |    +-- CRC16/MODBUS, low byte first
              |  |  |    +------- 0x19 = 25 words
              |  |  +------------ start register 0x0000
              |  +--------------- function 3, read holding registers
              +------------------ pack address (0x3F slave / 0x40 master)

    response  AD 03 32 <50 data bytes> <crc>    55 bytes
                    +-- byte count = 25 words * 2 = 0x32

Data area starts at byte 3 (after address, function, byte count). The register
map in registers.py is expressed as byte offsets into the whole frame, so the
decoders below take a byte offset directly rather than a register number.
"""

from __future__ import annotations

from typing import Final

FUNCTION_READ_HOLDING: Final = 3
START_REGISTER: Final = 0x0000
WORD_COUNT: Final = 0x19  # 25 words: regs 0x0000..0x0018 cover every live field

HEADER_LEN: Final = 3  # address, function, byte count
CRC_LEN: Final = 2
DATA_START: Final = HEADER_LEN  # first data byte
EXPECTED_FRAME_LEN: Final = HEADER_LEN + WORD_COUNT * 2 + CRC_LEN  # 55


class ProtocolError(Exception):
    """Base for every malformed-frame condition."""


class ShortFrameError(ProtocolError):
    """Frame is not the length the byte count implies. A truncated frame is an
    error, not a measurement — never decode past the end of a short buffer."""


class CrcError(ProtocolError):
    """The pack's own checksum does not match the payload."""


class UnexpectedResponseError(ProtocolError):
    """Address or function code is not what we asked, or a Modbus exception."""


def crc16_modbus(data: bytes) -> int:
    """CRC16/MODBUS: init 0xFFFF, poly 0xA001, no final xor. Low byte first on
    the wire. Identical to the Renogy driver's CRC — same Modbus standard."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_read_request(
    address: int, register: int = START_REGISTER, words: int = WORD_COUNT
) -> bytes:
    """Build the Modbus RTU read for one pack. CRC appended low byte first."""
    body = bytes(
        [address, FUNCTION_READ_HOLDING, register >> 8, register & 0xFF, words >> 8, words & 0xFF]
    )
    return body + crc16_modbus(body).to_bytes(2, "little")


def verify_frame(frame: bytes, *, address: int | None = None, words: int = WORD_COUNT) -> None:
    """Raise if the frame is truncated, mis-addressed, or fails its checksum.
    Called before any field is decoded, so a corrupt frame cannot be data."""
    expected = HEADER_LEN + words * 2 + CRC_LEN
    if len(frame) != expected:
        raise ShortFrameError(f"expected {expected} bytes for {words} words, got {len(frame)}")
    if frame[1] != FUNCTION_READ_HOLDING:
        if frame[1] & 0x80:
            raise UnexpectedResponseError(
                f"pack returned Modbus exception, function 0x{frame[1]:02x}, code {frame[2]}"
            )
        raise UnexpectedResponseError(f"unexpected function 0x{frame[1]:02x}")
    if frame[2] != words * 2:
        raise UnexpectedResponseError(f"byte count {frame[2]} does not match {words} words")
    if address is not None and frame[0] != address:
        raise UnexpectedResponseError(f"reply from address {frame[0]}, expected {address}")
    sent = int.from_bytes(frame[-CRC_LEN:], "little")
    calc = crc16_modbus(frame[:-CRC_LEN])
    if sent != calc:
        raise CrcError(f"crc mismatch: frame says 0x{sent:04x}, computed 0x{calc:04x}")


# --- byte-offset decoders (offsets are into the whole frame; data starts at 3) ---


def u16(frame: bytes, offset: int) -> int:
    return int.from_bytes(frame[offset : offset + 2], "big")


def s16(frame: bytes, offset: int) -> int:
    return int.from_bytes(frame[offset : offset + 2], "big", signed=True)
