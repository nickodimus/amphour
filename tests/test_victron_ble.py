"""Victron Instant Readout: framing, decryption and the battery monitor record.

The golden data here is the DECRYPTED record captured from a real SmartShunt
500A/50mV on 2026-09-22, alongside what the same shunt reported over its
VE.Direct cable at the same moment. The plaintext is battery readings and
carries no secret.

The device's real encryption key is deliberately NOT in this repository, which
is public. Where a test needs an encrypted frame it builds one with a throwaway
key, so the protocol layer is exercised end to end without publishing anyone's
key. Decoding is tested against the real bytes; only the encryption wrapper is
synthetic.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from amphour.drivers import build, fields_for
from amphour.drivers.victron_ble import protocol
from amphour.drivers.victron_ble.decode import decode_battery_monitor

# Captured 2026-09-22 21:5x from a SmartShunt 500A/50mV. The VE.Direct cable on
# the same device, read within the same minute, reported:
#     battery_voltage 52.623 V   battery_state_of_charge 89.2 %
#     consumed_amp_hours -22.609  battery_current -2.952 A
#     time_to_go_minutes 2921
# Current and time-to-go differ slightly because the load was moving and the
# two paths were sampled seconds apart; the rest agree.
SHUNT_PLAINTEXT = bytes.fromhex("780b8f1400000000fbd3ffe200c0f7")

TEST_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


def build_advertisement(plaintext: bytes, *, key: bytes, record_type: int = 0x02,
                        counter: int = 0x1234, prefix: int = 0x10) -> bytes:
    """Wrap a plaintext record the way a Victron device would."""
    nonce = counter.to_bytes(2, "little") + b"\x00" * 14
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return (
        bytes([prefix])
        + (0x8902).to_bytes(2, "little")
        + bytes([0xA3, record_type])
        + counter.to_bytes(2, "little")
        + bytes([key[0]])
        + ciphertext
    )


def test_decoded_values_match_the_cable_reading_of_the_same_shunt():
    """The cross-check that earns these fields `confirmed`: a second,
    independent measurement of the same battery agreeing field for field."""
    reading = decode_battery_monitor(SHUNT_PLAINTEXT, source="shunt")
    v = reading.values
    assert v["battery_voltage"] == pytest.approx(52.63, abs=0.01)
    assert v["battery_state_of_charge"] == pytest.approx(89.2, abs=0.05)
    assert v["consumed_amp_hours"] == pytest.approx(-22.6, abs=0.05)
    assert v["battery_current"] == pytest.approx(-2.818, abs=0.001)
    assert v["time_to_go_minutes"] == pytest.approx(2936, abs=1)


def test_consumed_amp_hours_is_negative_like_the_vedirect_driver():
    """Both drivers report the same field for the same shunt, so the sign
    convention has to match or a single query comparing them is nonsense."""
    reading = decode_battery_monitor(SHUNT_PLAINTEXT, source="shunt")
    assert reading.values["consumed_amp_hours"] < 0


def test_current_is_the_upper_22_bits_not_the_lower():
    """Reading current as the LOW 22 bits gives -12.353 A where the truth is
    -2.818 A: plausible in shape, wrong by four times, and it survives a
    casual glance. This test exists because that mistake was actually made."""
    reading = decode_battery_monitor(SHUNT_PLAINTEXT, source="shunt")
    assert reading.values["battery_current"] == pytest.approx(-2.818, abs=0.001)
    assert reading.values["battery_current"] != pytest.approx(-12.353, abs=0.01)


def test_round_trip_through_the_encrypted_frame():
    frame = build_advertisement(SHUNT_PLAINTEXT, key=TEST_KEY)
    record_type, counter, plaintext = protocol.parse(frame, TEST_KEY)
    assert record_type == protocol.RECORD_BATTERY_MONITOR
    assert counter == 0x1234
    assert plaintext == SHUNT_PLAINTEXT


def test_a_non_product_advertisement_is_skipped_not_decoded():
    """A Victron device emits more than one advertisement type under the same
    company id and MAC. Only 0x10 is a product advertisement; decoding the
    other one lands every offset on garbage."""
    frame = build_advertisement(SHUNT_PLAINTEXT, key=TEST_KEY, prefix=0x02)
    with pytest.raises(protocol.NotAProductAdvertisement):
        protocol.parse(frame, TEST_KEY)


def test_the_wrong_key_is_named_as_such_rather_than_yielding_noise():
    other = bytes([TEST_KEY[0] ^ 0xFF]) + TEST_KEY[1:]
    frame = build_advertisement(SHUNT_PLAINTEXT, key=TEST_KEY)
    with pytest.raises(protocol.WrongKeyError):
        protocol.parse(frame, other)


def test_a_short_key_is_rejected_before_it_can_produce_garbage():
    frame = build_advertisement(SHUNT_PLAINTEXT, key=TEST_KEY)
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(frame, TEST_KEY[:8])


def test_an_unknown_time_to_go_is_absent_rather_than_a_fiction():
    """0xFFFF means the shunt is not reporting it. Exporting that as 65535
    minutes would be a lie a dashboard would happily draw."""
    plaintext = b"\xff\xff" + SHUNT_PLAINTEXT[2:]
    reading = decode_battery_monitor(plaintext, source="shunt")
    assert "time_to_go_minutes" not in reading.values
    assert "battery_voltage" in reading.values


def test_a_truncated_record_raises_rather_than_decoding_short():
    with pytest.raises(protocol.ProtocolError):
        decode_battery_monitor(SHUNT_PLAINTEXT[:10], source="shunt")


def test_driver_registered_with_its_fields():
    names = {f.name for f in fields_for("victron_ble")}
    assert {"battery_voltage", "battery_state_of_charge", "consumed_amp_hours"} <= names


def test_build_requires_a_key():
    from amphour.device import DeviceError

    with pytest.raises(DeviceError):
        build("victron_ble", name="shunt", address="AA:BB:CC:DD:EE:FF")


def test_build_takes_the_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("AMPHOUR_TEST_VICTRON_KEY", TEST_KEY.hex())
    device = build(
        "victron_ble", name="shunt", address="aa:bb:cc:dd:ee:ff",
        encryption_key_env="AMPHOUR_TEST_VICTRON_KEY",
    )
    assert device.address == "AA:BB:CC:DD:EE:FF"


def test_a_key_that_is_not_hex_is_rejected_at_build_time():
    from amphour.device import DeviceError

    with pytest.raises(DeviceError):
        build("victron_ble", name="shunt", address="AA:BB:CC:DD:EE:FF",
              encryption_key="not a key")
