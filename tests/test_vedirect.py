"""VE.Direct framing and decoding, against blocks captured from a live device.

Fixtures are real blocks off a SmartShunt 500A/50mV on 2026-09-09. Every field
meaning was separately cross-checked against the `description` and `units` an
independent implementation recorded for the same labels on the same physical
device, which is why they are marked `confirmed`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from amphour.device import DeviceTimeout
from amphour.drivers.victron_vedirect import VEDirectDevice
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


def test_framer_survives_the_whole_raw_capture_corruption_and_all():
    """The harshest test available: 59 KB of unedited serial capture.

    That capture was taken with an external `stty` followed by a separate
    `cat`, which leaves the port closed in between; the result contained
    genuinely corrupt regions - duplicated fragments, lost bytes, a label
    reading `H16\\nH7` - alongside 88 blocks that verify perfectly.

    Keeping it is deliberate. Clean fixtures prove the parser handles good
    input; this one proves the framer does not derail on bad input, which is
    what a wedged USB adapter actually produces.
    """
    import random

    raw = (FIXTURES / "vedirect-raw-stream-2026-09-09.bin").read_bytes()
    random.seed(1)
    reader = BlockReader()
    out = []
    pos = 0
    while pos < len(raw):  # irregular chunks, as a serial read delivers them
        step = random.randint(1, 300)
        out.extend(reader.feed(raw[pos : pos + step]))
        pos += step

    assert reader.blocks_ok == 88, "every checksum-valid block must be recovered"
    assert reader.blocks_bad > 0, "the corrupt regions must actually be rejected"
    assert reader.pending < 2048, "buffer must stay bounded"

    main = [b for b in out if "SOC" in b]
    history = [b for b in out if "H18" in b]
    assert len(main) == 44
    assert len(history) == 44

    # nothing incoherent got through the checksum
    for block in main:
        assert 0 <= int(block["SOC"]) <= 1000
        assert 5_000 < int(block["V"]) < 70_000


def test_no_corrupt_block_is_ever_yielded():
    """A block that fails its checksum must be dropped, not repaired.

    A VE.Direct block with a bad checksum is indistinguishable from correctly
    framed nonsense, so guessing at it would manufacture readings.
    """
    raw = (FIXTURES / "vedirect-raw-stream-2026-09-09.bin").read_bytes()
    reader = BlockReader()
    for block in reader.feed(raw):
        for label, value in block.items():
            if label.startswith("H") or label in {"V", "I", "P", "CE", "SOC", "TTG"}:
                int(value)  # raises if the checksum let through a mangled number


# --- device-level staleness -------------------------------------------------
#
# The framer above is well covered. What was not covered is what the DEVICE
# does when the framer keeps rejecting everything: stream()'s timeout wrapped
# only the queue read, so a link delivering bytes that never frame never timed
# out, never emitted, and looked perfectly alive from outside.


async def _feed(device, payload, period=0.01):
    """Push bytes at the device's queue until cancelled."""
    while True:
        with contextlib.suppress(asyncio.QueueFull):
            device._queue.put_nowait(payload)
        await asyncio.sleep(period)


async def _drain(device, budget=2.0):
    """Consume stream() to completion, bounded, so a hang fails as a timeout."""
    async with asyncio.timeout(budget):
        async for _ in device.stream():
            pass


async def test_bytes_that_never_frame_are_a_fault_not_a_silence(blocks):
    """Garbage on the wire must be detectable. Silence already was."""
    device = VEDirectDevice("shunt", "/dev/null", stale_after=0.2, emit_interval=0.0)
    feeder = asyncio.create_task(_feed(device, b"\x00\xff not a ve.direct block \x7f"))
    try:
        with pytest.raises(DeviceTimeout) as caught:
            await _drain(device)
    finally:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
    assert "block" in str(caught.value).lower()


async def test_total_silence_is_still_reported(blocks):
    device = VEDirectDevice("shunt", "/dev/null", stale_after=0.2, emit_interval=0.0)
    with pytest.raises(DeviceTimeout):
        await _drain(device)


async def test_a_healthy_stream_is_not_called_stale(blocks):
    """The new check must not fire while real blocks are arriving."""
    main = next(b for b in blocks if b["kind"] == "main")
    good = bytes.fromhex(main["block_hex"])
    device = VEDirectDevice("shunt", "/dev/null", stale_after=0.3, emit_interval=0.0)
    feeder = asyncio.create_task(_feed(device, good, period=0.02))
    seen = []
    try:
        async with asyncio.timeout(1.0):
            async for reading in device.stream():
                seen.append(reading)
                if len(seen) >= 3:
                    break
    finally:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
    assert len(seen) >= 3
    assert seen[0].values["battery_voltage"] > 0
