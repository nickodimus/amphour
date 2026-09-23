"""Victron "Instant Readout" BLE advertisement framing and decryption.

Victron devices broadcast their live state in the manufacturer-specific field
of a BLE advertisement, under company id 0x02E1. The payload is AES-CTR
encrypted with a per-device key that the owner reads out of VictronConnect
(Product info -> Instant readout via Bluetooth). No connection, no pairing and
no GATT: a passive listener sees every device in radio range at once, and
reading one costs the battery nothing.

THE TRAP, and the reason this module checks the prefix byte: a Victron device
emits MORE THAN ONE kind of advertisement under the same company id and the
same MAC. Only the record whose first byte is 0x10 is a product advertisement.
The other one seen in the field (on a SmartShunt, first bytes 02 f0 95 30 ...)
is something else entirely, and every offset below lands on garbage if you
decode it. Verified against a live SmartShunt on 2026-09-22: sampling without
the prefix check caught the wrong record roughly half the time.

Layout of a product advertisement, after the company id is stripped:

    [0]     0x10    product advertisement prefix
    [1:3]   u16 LE  model id
    [3]             unidentified; constant per device in every capture so far
    [4]             record type - what kind of device state follows
    [5:7]   u16 LE  counter; monotonic, and used as the AES-CTR nonce
    [7]             first byte of the encryption key, as a check
    [8:]            ciphertext

The counter doubles as the nonce, which is what makes each frame decryptable
on its own with no session state - the property that lets a listener join
mid-stream and read the very next advertisement.
"""

from __future__ import annotations

from typing import Final

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

VICTRON_COMPANY_ID: Final = 0x02E1
PRODUCT_ADVERTISEMENT: Final = 0x10
HEADER_LEN: Final = 8
KEY_LEN: Final = 16

# Record types. Only those verified against real hardware are decoded; the
# rest are named so an unknown device reports something honest instead of a
# bare integer.
RECORD_BATTERY_MONITOR: Final = 0x02

RECORD_NAMES: Final[dict[int, str]] = {
    0x01: "solar charger",
    0x02: "battery monitor",
    0x03: "inverter",
    0x04: "dc/dc converter",
    0x05: "smart lithium",
    0x06: "inverter rs",
    0x08: "dc energy meter",
    0x0A: "orion xs",
    0x0C: "ve.bus",
    0x0D: "dc charger",
}


class ProtocolError(Exception):
    """An advertisement that cannot be trusted as this device's state."""


class NotAProductAdvertisement(ProtocolError):
    """The record is from a Victron device but is not a product advertisement.

    Expected and common: a device emits other advertisement types too. A
    listener skips these silently rather than treating them as a fault.
    """


class WrongKeyError(ProtocolError):
    """The key's check byte does not match, so decryption would yield noise."""


def parse(payload: bytes, key: bytes) -> tuple[int, int, bytes]:
    """Return (record_type, counter, plaintext) for one advertisement.

    `payload` is the manufacturer-specific data under company id 0x02E1, with
    the company id already stripped (which is what BlueZ hands over).
    """
    if len(key) != KEY_LEN:
        raise ProtocolError(f"encryption key must be {KEY_LEN} bytes, got {len(key)}")
    if len(payload) < HEADER_LEN + 1:
        raise ProtocolError(f"advertisement too short: {len(payload)} bytes")
    if payload[0] != PRODUCT_ADVERTISEMENT:
        raise NotAProductAdvertisement(f"prefix 0x{payload[0]:02x}, not a product advertisement")
    if payload[7] != key[0]:
        raise WrongKeyError(
            f"key check byte 0x{payload[7]:02x} does not match this key's "
            f"0x{key[0]:02x}; the key belongs to a different device"
        )

    record_type = payload[4]
    counter = int.from_bytes(payload[5:7], "little")
    # The counter IS the nonce, little-endian in the low two bytes.
    nonce = payload[5:7] + b"\x00" * 14
    decryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor()
    plaintext = decryptor.update(payload[HEADER_LEN:]) + decryptor.finalize()
    return record_type, counter, plaintext


def model_id(payload: bytes) -> int:
    """The device's model id, readable without the key."""
    return int.from_bytes(payload[1:3], "little")
