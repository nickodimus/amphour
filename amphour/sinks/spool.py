"""A disk spool for writes that could not be delivered.

The InfluxDB sink used to drop a batch it could not deliver, on purpose: a
buffer that grows while the server is away will exhaust a 1 GB board. That
reasoning is about MEMORY. A day-long outage of the network path (2026-11-04:
19.7 hours, every reading taken and none kept) is the case it gets wrong, and
the SD card has room memory does not. So undelivered batches go to disk, in
bounded segment files, and are replayed when the server answers again.

Layout: `<directory>/<sequence>.lp`, line protocol, one point per line, exactly
the bytes that would have been POSTed. Segments are numbered, not dated, so
their order never depends on the clock - the host this was written for has no
RTC. A segment is closed at `segment_bytes` and a new one started, which keeps
each replay a modest single POST and lets the cap evict whole files.

Bounded: when the directory exceeds `max_bytes` the OLDEST segments are deleted
until it fits. Losing the oldest hour of a week-long outage is better than
filling the card under the thing that takes the readings.

Everything here is synchronous file I/O and holds no lock of its own. The sink
calls it from a worker thread while holding the sink's lock, so calls never
overlap.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SUFFIX = ".lp"


def clock_is_trustworthy() -> bool:
    """Has this host's clock been set from something real since it booted?

    Every spooled point carries the timestamp the reading was taken at, and a
    replay lands it there. A board with no RTC that reboots while offline comes
    up with a stale clock; readings stamped by it would be replayed into the
    PAST, on top of real points with the same timestamps, which InfluxDB
    overwrites without a word. Dropping those is the honest outcome.

    Trustworthy means either: systemd-timesyncd has synchronised since boot
    (it creates this flag file under /run, which is wiped at every boot), or
    the host has a hardware clock at all.
    """
    if Path("/run/systemd/timesync/synchronized").exists():
        return True
    rtc = Path("/sys/class/rtc")
    return rtc.is_dir() and any(rtc.glob("rtc*"))


class Spool:
    def __init__(
        self,
        directory: str | Path,
        *,
        max_bytes: int,
        segment_bytes: int = 256 * 1024,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("spool max_bytes must be positive")
        self.directory = Path(directory).expanduser()
        self.max_bytes = max_bytes
        self.segment_bytes = segment_bytes
        # Fail at startup, not at the first outage: a spool that cannot be
        # written is discovered exactly when it is needed otherwise.
        self.directory.mkdir(parents=True, exist_ok=True)
        if not os.access(self.directory, os.W_OK | os.X_OK):
            raise PermissionError(f"spool directory {self.directory} is not writable")
        existing = self._segments()
        # Never append to a segment left by a previous run: start past it.
        self._next = (int(existing[-1].stem) + 1) if existing else 0
        self._current: Path | None = None

    def _segments(self) -> list[Path]:
        """Every segment, oldest first."""
        found = [p for p in self.directory.iterdir() if p.suffix == SUFFIX and p.stem.isdigit()]
        return sorted(found, key=lambda p: int(p.stem))

    def pending(self) -> tuple[int, int]:
        """(segments, bytes) waiting to be replayed."""
        segments = self._segments()
        return len(segments), sum(p.stat().st_size for p in segments)

    def append(self, payload: bytes) -> int:
        """Store one undelivered batch. Returns how many segments the cap evicted."""
        if self._current is None or self._current.stat().st_size >= self.segment_bytes:
            self._current = self.directory / f"{self._next:010d}{SUFFIX}"
            self._next += 1
        # One write of whole lines, newline-terminated, then fsync: an outage
        # is exactly when the power is also likely to go, and a torn tail would
        # be one unparseable line in the replay.
        data = payload if payload.endswith(b"\n") else payload + b"\n"
        fd = os.open(self._current, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return self._enforce_cap()

    def _enforce_cap(self) -> int:
        segments = self._segments()
        sizes = {p: p.stat().st_size for p in segments}
        total = sum(sizes.values())
        evicted = 0
        # Always keep the newest segment, even if it alone is over the cap:
        # the batch just written is the one thing known to be worth having.
        while total > self.max_bytes and len(segments) > 1:
            oldest = segments.pop(0)
            total -= sizes[oldest]
            self._unlink(oldest)
            evicted += 1
        return evicted

    def oldest(self) -> Path | None:
        """The next segment to replay, or None when the spool is empty.

        Handing out the segment still being appended to closes it, so a batch
        spooled during the replay starts a fresh file instead of landing in one
        that is about to be deleted.
        """
        segments = self._segments()
        if not segments:
            return None
        if segments[0] == self._current:
            self._current = None
        return segments[0]

    def discard(self, segment: Path) -> None:
        self._unlink(segment)

    def _unlink(self, segment: Path) -> None:
        if segment == self._current:
            self._current = None
        segment.unlink(missing_ok=True)


def describe(payload: bytes) -> tuple[int, int | None, int | None]:
    """(points, first epoch, last epoch) of a line-protocol payload.

    The timestamp is the last space-separated token of a line. Used only to
    say, in the log, which stretch of time a replay covered.
    """
    lines = [line for line in payload.split(b"\n") if line.strip()]

    def stamp(line: bytes) -> int | None:
        token = line.rsplit(b" ", 1)[-1]
        return int(token) if token.isdigit() else None

    if not lines:
        return 0, None, None
    return len(lines), stamp(lines[0]), stamp(lines[-1])
