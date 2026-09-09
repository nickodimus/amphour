"""VE.Direct framing and decoding, against blocks captured from a live device.

Fixtures are real blocks off a SmartShunt 500A/50mV on 2026-09-09. Every field
meaning was separately cross-checked against the `description` and `units` an
independent implementation recorded for the same labels on the same physical
device, which is why they are marked `confirmed`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amphour.drivers.victron_vedirect.fields import BY_LABEL, FIELDS
from amphour.drivers.victron_vedirect.protocol import (
    BlockReader,
    checksum_ok,
    parse_block,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def blocks() -> list[dict]:
    path = FIXTURES / "vedirect-smartshunt-2026-09-09.json"
    data = json.loads(path.read_text())
    assert data, "VE.Direct fixture is missing"
    return data


def test_every_captured_block_passes_its_checksum(blocks):
    for entry in blocks:
        assert checksum_ok(bytes.fromhex(entry["block_hex"])), entry["kind"]


def test_a_flipped_byte_fails_the_checksum(blocks):
    raw = bytearray(bytes.fromhex(blocks[0]["block_hex"]))
    raw[8] ^= 0xFF
    assert not checksum_ok(bytes(raw))


def test_main_block_parses_to_the_expected_labels(blocks):
    main = next(b for b in blocks if b["kind"] == "main")
    parsed = parse_block(bytes.fromhex(main["block_hex"]))
    assert {"PID", "V", "I", "P", "CE", "SOC", "TTG", "Alarm", "BMV"} <= set(parsed)
    assert "Checksum" not in parsed, "the checksum record is framing, not data"
    assert parsed["BMV"] == "SmartShunt 500A/50mV"


def test_history_block_parses_to_the_expected_labels(blocks):
    hist = next(b for b in blocks if b["kind"] == "history")
    parsed = parse_block(bytes.fromhex(hist["block_hex"]))
    assert {"H1", "H2", "H4", "H9", "H17", "H18"} <= set(parsed)


def test_reader_frames_blocks_out_of_a_byte_stream(blocks):
    """Feed the stream in awkward chunks; framing must not depend on where the
    reads happen to land."""
    stream = b"".join(bytes.fromhex(b["block_hex"]) for b in blocks)
    reader = BlockReader()
    got = []
    for i in range(0, len(stream), 7):  # deliberately not a block boundary
        got.extend(reader.feed(stream[i : i + 7]))
    assert len(got) == len(blocks)
    assert reader.blocks_ok == len(blocks)
    assert reader.blocks_bad == 0


def test_reader_discards_corrupt_data_and_resynchronises(blocks):
    """A real capture contained corrupt regions beside perfectly valid blocks.

    Garbage must be dropped, and the reader must still frame the good blocks
    that follow it.
    """
    good = bytes.fromhex(blocks[0]["block_hex"])
    corrupt = (FIXTURES / "vedirect-corrupt-sample.bin").read_bytes()
    reader = BlockReader()
    out = list(reader.feed(corrupt + good))
    assert len(out) >= 1
    assert reader.blocks_ok >= 1


def test_reader_does_not_grow_without_bound_on_pure_garbage():
    reader = BlockReader()
    for _ in range(50):
        list(reader.feed(b"\x00" * 1024))
    assert len(reader._buf) <= 4096


def test_every_declared_label_has_a_field():
    assert len(FIELDS) == len(BY_LABEL)
    assert {f.name for f in FIELDS} == {label.name for label in BY_LABEL.values()}


def test_field_names_share_the_vocabulary_with_the_renogy_driver():
    """battery_voltage must mean the same thing in both drivers, so one query
    compares them. This is the property that makes two sources disagreeing
    visible instead of split across metric names that never meet."""
    from amphour.drivers.renogy.registers import FIELDS as RENOGY

    shared = {f.name for f in FIELDS} & {f.name for f in RENOGY}
    assert "battery_voltage" in shared
    assert "battery_state_of_charge" in shared
    # ... but a shunt's signed net current is NOT a controller's charge current
    assert "battery_current" not in {f.name for f in RENOGY}


def test_scaling_produces_clean_numbers(blocks):
    """mV to V is a 0.001 multiply, which reintroduces binary float noise."""
    main = next(b for b in blocks if b["kind"] == "main")
    parsed = parse_block(bytes.fromhex(main["block_hex"]))
    volts = round(int(parsed["V"]) * BY_LABEL["V"].scale, 3)
    assert volts == float(f"{volts:.3f}")
    assert 0 < volts < 100
