#!/usr/bin/env python3
"""Build a test fixture from a live BLE capture.

Fixtures are made of two INDEPENDENT things, and keeping them independent is
the whole point:

  frame_hex     the raw bytes the device actually put on the air, from a
                btmon capture
  old_app_log   what the PREVIOUS implementation (solar-bt-monitor) reported
                for that same instant, read out of its own log file

The test suite decodes `frame_hex` with amphour's decoder and asserts it
matches `old_app_log`. Because those two come from different programs, that is
a real cross-check rather than a decoder agreeing with itself.

This script deliberately does NOT decode the frames. If it did, the fixture
would carry this script's opinion of what the bytes mean, and the test would be
circular.

Usage:

    # on the device, while the old app is running (non-disruptive):
    sudo timeout -s INT 360 btmon -w /tmp/cap.btsnoop

    # then, with the capture start time it printed:
    btmon -r /tmp/cap.btsnoop > cap.txt
    python tools/extract_fixtures.py \
        --btmon-text cap.txt \
        --app-log app.log \
        --start "2026-09-09 11:13:42" \
        --out tests/fixtures/day-2026-09-09.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

# Field names as the OLD application logged them. Left exactly as it wrote
# them; the mapping onto amphour's names lives in tests/conftest.py.
LOG_LINE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \w+\s+: (\w+): (-?[\d.]+)$")
ATT_LINE = re.compile(r"ATT: (Handle Value Notification|Write Request)")
TIME_LINE = re.compile(r"#\d+\s+\[hci\d+\]\s+([\d.]+)")
DATA_LINE = re.compile(r"^\s+Data: ([0-9a-f]+)\s*$")


def parse_btmon(text: str) -> tuple[list[tuple[float, bytes]], list[tuple[float, bytes]]]:
    """Return (notifications, writes) as (relative_seconds, payload) pairs."""
    lines = text.splitlines()
    notifications: list[tuple[float, bytes]] = []
    writes: list[tuple[float, bytes]] = []
    for i, line in enumerate(lines):
        match = ATT_LINE.search(line)
        if not match:
            continue
        timestamp = None
        for back in range(i, max(-1, i - 4), -1):
            found = TIME_LINE.search(lines[back])
            if found:
                timestamp = float(found.group(1))
                break
        payload = None
        for forward in range(i, min(len(lines), i + 5)):
            found = DATA_LINE.match(lines[forward])
            if found:
                payload = bytes.fromhex(found.group(1))
                break
        if timestamp is None or payload is None:
            continue
        target = notifications if match.group(1).startswith("Handle") else writes
        target.append((timestamp, payload))
    return notifications, writes


def parse_app_log(text: str) -> list[tuple[datetime.datetime, dict[str, float]]]:
    """Group the old app's per-field log lines into one block per reading."""
    blocks: list[tuple[datetime.datetime, dict[str, float]]] = []
    current: dict[str, float] = {}
    current_time: datetime.datetime | None = None
    for line in text.splitlines():
        match = LOG_LINE.match(line)
        if not match:
            continue
        stamp = datetime.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        name, value = match.group(2), match.group(3)
        if current_time is None or abs((stamp - current_time).total_seconds()) > 5:
            if current and current_time is not None:
                blocks.append((current_time, current))
            current, current_time = {}, stamp
        current[name] = float(value) if "." in value else int(value)
    if current and current_time is not None:
        blocks.append((current_time, current))
    return blocks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--btmon-text", required=True, type=Path)
    parser.add_argument("--app-log", required=True, type=Path)
    parser.add_argument("--start", required=True, help='capture start, "YYYY-MM-DD HH:MM:SS"')
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--max-pair-skew",
        type=float,
        default=20.0,
        help="seconds; a frame with no log block this close is dropped",
    )
    parser.add_argument("--note", default="", help="free-text note stored in each entry")
    args = parser.parse_args()

    start = datetime.datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S")
    notifications, writes = parse_btmon(args.btmon_text.read_text())
    blocks = parse_app_log(args.app_log.read_text())

    print(f"notifications : {len(notifications)}")
    print(f"writes        : {len(writes)}")
    print(f"log blocks    : {len(blocks)}")
    if not notifications:
        print("no notifications found - wrong capture file?", file=sys.stderr)
        return 1
    if not blocks:
        print("no log blocks found - wrong log window?", file=sys.stderr)
        return 1

    entries, unpaired = [], 0
    for offset, frame in notifications:
        wall = start + datetime.timedelta(seconds=offset)
        nearest, skew = min(
            ((b, abs((b[0] - wall).total_seconds())) for b in blocks),
            key=lambda pair: pair[1],
        )
        if skew > args.max_pair_skew:
            unpaired += 1
            continue
        entries.append(
            {
                "wall_clock": wall.isoformat(timespec="seconds"),
                "frame_hex": frame.hex(),
                "frame_len": len(frame),
                # produced by the PREVIOUS implementation, not by this script
                "old_app_log": nearest[1],
                "old_app_log_time": nearest[0].isoformat(timespec="seconds"),
                "pair_skew_s": round(skew, 3),
                "note": args.note,
            }
        )

    if unpaired:
        print(f"dropped {unpaired} frame(s) with no log block within {args.max_pair_skew}s")
    if not entries:
        print("nothing paired - check --start against the log timestamps", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(entries, indent=2) + "\n")
    unique = len({e["frame_hex"] for e in entries})
    print(f"wrote {len(entries)} entries ({unique} unique frames) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
