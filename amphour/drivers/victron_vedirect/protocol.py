"""Victron VE.Direct text protocol: framing and checksum.

VE.Direct is not request/response. The device transmits continuously at
19200 8N1, emitting blocks roughly once a second. A block is a run of

    \\r\\n<label>\\t<value>

records terminated by a `Checksum` record whose value is a single raw byte.
A block is intact when every byte of it, including that byte, sums to zero
modulo 256.

Observed on a live SmartShunt 500A/50mV on 2026-09-09: two block types
alternate at 1 Hz each - a "main" block of instantaneous values and a
"history" block of H-prefixed lifetime counters. The checksum covers one block
only, so the two are independent.

This parser resynchronises rather than trusting the stream. A capture taken
that day contained genuinely corrupt regions - duplicated fragments and lost
bytes - alongside 88 blocks that verified perfectly. Anything that fails its
checksum is discarded, because a VE.Direct block with a bad checksum is
indistinguishable from correctly-framed nonsense.
"""

from __future__ import annotations

from collections.abc import Iterator

CHECKSUM_LABEL = b"Checksum"
RECORD_SEP = b"\r\n"
FIELD_SEP = b"\t"

# A main block is ~150 bytes and a history block ~250. Anything much larger
# means we are accumulating garbage and never finding a valid checksum, so the
# buffer is capped rather than growing without bound on a wedged adapter.
MAX_BLOCK_BYTES = 2048


class VEDirectError(Exception):
    """Malformed VE.Direct data."""


def checksum_ok(block: bytes) -> bool:
    """A VE.Direct block is intact when all its bytes sum to 0 mod 256."""
    return sum(block) % 256 == 0


def parse_block(block: bytes) -> dict[str, str]:
    """Split a verified block into label/value pairs, dropping the checksum.

    Values stay as text. Interpretation belongs to the field table, so that a
    label this driver does not know about is preserved rather than discarded.
    """
    out: dict[str, str] = {}
    for record in block.split(RECORD_SEP):
        if FIELD_SEP not in record:
            continue
        label, _, value = record.partition(FIELD_SEP)
        name = label.decode("ascii", "replace").strip()
        if name == CHECKSUM_LABEL.decode():
            continue
        out[name] = value.decode("ascii", "replace").strip()
    return out


class BlockReader:
    """Incremental framer. Feed it bytes, get complete verified blocks out.

    Tracks how many blocks were rejected so a caller can tell "the link is
    quiet" apart from "the link is noisy", which are different faults.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.blocks_ok = 0
        self.blocks_bad = 0

    def feed(self, data: bytes) -> Iterator[dict[str, str]]:
        self._buf.extend(data)
        marker = RECORD_SEP + CHECKSUM_LABEL + FIELD_SEP
        while True:
            at = self._buf.find(marker)
            if at < 0:
                break
            end = at + len(marker) + 1  # include the checksum byte itself
            if end > len(self._buf):
                break  # checksum byte has not arrived yet
            block = bytes(self._buf[:end])
            del self._buf[:end]
            if checksum_ok(block):
                self.blocks_ok += 1
                yield parse_block(block)
            else:
                self.blocks_bad += 1

        if len(self._buf) > MAX_BLOCK_BYTES:
            # No valid frame in far more than a block's worth of data. Keep the
            # tail, which may hold the start of a good block, and drop the rest.
            self.blocks_bad += 1
            del self._buf[: -MAX_BLOCK_BYTES // 2]
