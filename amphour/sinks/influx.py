"""InfluxDB 1.x sink: line protocol over HTTP, no client library.

Deliberately dependency-light - see amphour/line_protocol.py for why neither
InfluxData client is a good choice for a 1.x server in 2026.

Writes are buffered and flushed on a point count or a deadline, whichever comes
first. `requests` is synchronous, so the actual POST runs in a worker thread to
keep it off the event loop.

This sink is inert unless configured. A failure to reach InfluxDB is logged and
the buffer is dropped rather than growing without bound - metrics are better
lost than allowed to exhaust memory on a 1 GB board.

That is still what happens by default. With `spool_dir` set, a batch the server
could not be REACHED for goes to disk instead (amphour/sinks/spool.py) and is
replayed, oldest first, one segment after each successful live write, so live
data is never queued behind a backlog. Replaying is safe to repeat: every line
carries the time its reading was taken, at the same precision as the live
write, and InfluxDB overwrites a point with the same series and timestamp.

Three things are deliberately NOT spooled:

  - a batch the server REFUSED (400, 413). It has already kept every point it
    could parse, and a refused point would be refused again on every replay,
    forever, in front of everything behind it.
  - a batch taken while the clock has not been set since boot. Replayed, it
    would land at the wrong time on top of real points (see spool.py).
  - anything when the spool itself cannot be written. publish() must not raise.

Any other answer - no connection, a timeout, a 5xx, an auth or missing-database
error that someone can fix on the server - keeps the data.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..device import Device
from ..fields import exported
from ..line_protocol import to_line
from ..reading import Reading
from .spool import Spool, clock_is_trustworthy, describe

log = logging.getLogger(__name__)

# The server answered and refused the DATA. Everything else is the server being
# away or misconfigured, which a replay can outlast.
REFUSED = (400, 413)


class InfluxRefused(requests.RequestException):
    """InfluxDB answered and rejected the payload itself."""


def _when(epoch: int | None) -> str:
    if epoch is None:
        return "?"
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class InfluxSink:
    name = "influxdb"

    def __init__(
        self,
        devices: Sequence[Device],
        *,
        url: str,
        database: str,
        measurement: str = "amphour",
        username: str | None = None,
        password: str | None = None,
        tags: dict[str, str] | None = None,
        include_unverified: bool = False,
        batch_size: int = 60,
        flush_interval: float = 60.0,
        timeout: float = 10.0,
        retention_policy: str | None = None,
        spool_dir: str | None = None,
        spool_max_mb: int = 64,
        clock_ok: Callable[[], bool] = clock_is_trustworthy,
    ) -> None:
        self.url = url.rstrip("/")
        self.database = database
        self.measurement = measurement
        self.tags = tags or {}
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.timeout = timeout
        self.retention_policy = retention_policy
        # Keyed by reading SOURCE, not device name. A driver emitting several
        # readings per device (the EG4's two packs) reports sources the device
        # name never matches, and a miss here meant `allowed` came back None and
        # the unverified-field filter silently did not apply to that device.
        self._allowed = {
            source: exported(d.fields, include_unverified=include_unverified)
            for d in devices
            for source in getattr(d, "sources", (d.name,))
        }
        self._buffer: list[str] = []
        self._last_flush = 0.0
        self._lock = asyncio.Lock()

        # Off unless a directory is given. Built here, so an unwritable spool
        # stops amphour at startup instead of being found during an outage.
        self._spool: Spool | None = None
        self._clock_ok = clock_ok
        self._replayed = 0
        self._replayed_first: int | None = None
        self._replayed_last: int | None = None
        if spool_dir:
            self._spool = Spool(spool_dir, max_bytes=spool_max_mb * 1024 * 1024)
            segments, size = self._spool.pending()
            if segments:
                log.info(
                    "influxdb spool holds %d segment(s), %.1f MB, from an earlier run",
                    segments,
                    size / 1e6,
                )

        self._session = requests.Session()
        if username is not None and password is not None:
            # Basic auth, not the u=/p= query parameters: InfluxData's own docs
            # warn that query-string credentials land in server logs.
            self._session.auth = (username, password)
        retrying = HTTPAdapter(
            max_retries=Retry(
                total=2,
                backoff_factor=0.5,
                status_forcelist=(500, 502, 503, 504),
                allowed_methods=frozenset(["POST"]),
            )
        )
        # BOTH schemes. Mounting only http:// meant an https:// url quietly
        # fell back to requests' default adapter and got no retries at all -
        # a config change with no error and no log line, which is the shape
        # this project keeps finding: a behaviour that silently is not there.
        self._session.mount("http://", retrying)
        self._session.mount("https://", retrying)

    def _params(self) -> dict[str, str]:
        params = {"db": self.database, "precision": "s"}
        if self.retention_policy:
            params["rp"] = self.retention_policy
        return params

    async def publish(self, reading: Reading) -> None:
        allowed = self._allowed.get(reading.source)
        fields: dict[str, Any] = {
            name: value
            for name, value in reading.values.items()
            if allowed is None or name in allowed
        }
        # Text readings a Prometheus gauge cannot hold - a model string, an
        # alarm state - do fit here, so InfluxDB gets the fuller picture.
        for name, value in reading.text.items():
            if allowed is None or name in allowed:
                fields[name] = value
        if not fields:
            return
        # The device name is a TAG, not a field: it is what the series is split
        # by, and tags are indexed where fields are not.
        tags = {**self.tags, "device": reading.source}
        line = to_line(self.measurement, fields, tags, reading.taken_at, precision="s")
        async with self._lock:
            self._buffer.append(line)
            now = asyncio.get_running_loop().time()
            if self._last_flush == 0.0:
                self._last_flush = now
            due = (
                len(self._buffer) >= self.batch_size
                or now - self._last_flush >= self.flush_interval
            )
            if due:
                await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._buffer:
            return
        payload = "\n".join(self._buffer).encode()
        count = len(self._buffer)
        self._buffer.clear()
        self._last_flush = asyncio.get_running_loop().time()
        try:
            await asyncio.to_thread(self._post, payload)
        except requests.RequestException as exc:
            await self._undelivered(payload, count, exc)
            return
        log.debug("wrote %d points to influxdb", count)
        # The server just answered, so this is the moment to send some of what
        # it missed. One segment per live flush: the backlog never holds up
        # the readings being taken now.
        await self._replay_one()

    async def _undelivered(
        self, payload: bytes, count: int, exc: requests.RequestException
    ) -> None:
        """Spool a batch that could not be written, or say why it was dropped.

        Every line here keeps the words "influxdb write": the overwatch guard
        publisher counts them, and an outage must stay visible there whether
        or not the data survived it.
        """
        if self._spool is None:
            log.warning("influxdb write of %d points failed, dropped: %s", count, exc)
            return
        if isinstance(exc, InfluxRefused):
            log.warning(
                "influxdb write of %d points refused by the server, dropped: %s", count, exc
            )
            return
        if not self._clock_ok():
            log.warning(
                "influxdb write of %d points failed, dropped "
                "(clock not set since boot, so not spooled): %s",
                count,
                exc,
            )
            return
        try:
            evicted = await asyncio.to_thread(self._spool.append, payload)
            segments, size = await asyncio.to_thread(self._spool.pending)
        except OSError as spool_exc:
            log.warning(
                "influxdb write of %d points failed, dropped (spool write failed: %s): %s",
                count,
                spool_exc,
                exc,
            )
            return
        log.warning(
            "influxdb write of %d points failed, spooled (%d segment(s), %.1f MB waiting): %s",
            count,
            segments,
            size / 1e6,
            exc,
        )
        if evicted:
            log.warning(
                "influxdb spool is over its %d MB cap: deleted the %d oldest segment(s)",
                self._spool.max_bytes // (1024 * 1024),
                evicted,
            )

    async def _replay_one(self) -> None:
        """Send the oldest spooled segment, if there is one."""
        if self._spool is None:
            return
        try:
            segment = await asyncio.to_thread(self._spool.oldest)
            if segment is None:
                return
            payload = await asyncio.to_thread(segment.read_bytes)
        except OSError as exc:
            log.warning("influxdb spool could not be read: %s", exc)
            return
        points, first, last = describe(payload)
        if points:
            try:
                await asyncio.to_thread(self._post, payload)
            except InfluxRefused as exc:
                # The server wrote what it could parse and named the rest. Keeping
                # the segment would only meet the same refusal on every flush.
                log.warning(
                    "influxdb spool segment %s (%d points, %s to %s) refused in part "
                    "or whole, discarded: %s",
                    segment.name,
                    points,
                    _when(first),
                    _when(last),
                    exc,
                )
            except requests.RequestException as exc:
                log.warning("influxdb spool replay stopped, will retry: %s", exc)
                return
            else:
                self._replayed += points
                firsts = [t for t in (self._replayed_first, first) if t is not None]
                lasts = [t for t in (self._replayed_last, last) if t is not None]
                self._replayed_first = min(firsts) if firsts else None
                self._replayed_last = max(lasts) if lasts else None
                log.info(
                    "influxdb spool replayed %d points (%s to %s)",
                    points,
                    _when(first),
                    _when(last),
                )
        try:
            await asyncio.to_thread(self._spool.discard, segment)
            segments, _ = await asyncio.to_thread(self._spool.pending)
        except OSError as exc:
            log.warning("influxdb spool could not be cleared: %s", exc)
            return
        if segments == 0 and self._replayed:
            # A WARNING although it is good news: it asks for a person. A
            # server-side rollup that only looks back a few minutes has not
            # seen these points and will not, until someone re-runs it.
            log.warning(
                "influxdb spool drained: replayed %d points taken %s to %s. "
                "Downsamples on the server do not see late points; re-run them "
                "over that span.",
                self._replayed,
                _when(self._replayed_first),
                _when(self._replayed_last),
            )
            self._replayed = 0
            self._replayed_first = self._replayed_last = None

    def _post(self, payload: bytes) -> None:
        response = self._session.post(
            f"{self.url}/write",
            params=self._params(),
            data=payload,
            timeout=self.timeout,
        )
        if response.status_code == 204:
            return
        message = f"influxdb returned {response.status_code}: {response.text[:200]}"
        if response.status_code in REFUSED:
            raise InfluxRefused(message)
        raise requests.RequestException(message)

    async def aclose(self) -> None:
        async with self._lock:
            await self._flush_locked()
        self._session.close()
