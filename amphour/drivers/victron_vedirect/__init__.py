"""Victron VE.Direct driver: a device that streams, rather than one we poll.

Serial is configured with stdlib termios on a descriptor this process holds
open for the lifetime of the connection. That matters: configuring the port
with an external `stty` and then opening it separately leaves a window in
which the port is closed, and a capture taken that way on 2026-09-09 came back
with duplicated fragments and lost bytes while the same port read cleanly from
a process that held it throughout.

No pyserial dependency - termios and asyncio's add_reader are enough, and one
fewer dependency on a device that has to run unattended.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import termios
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from ...device import DeviceError, DeviceTimeout, StreamingDevice
from ...fields import Field
from ...reading import Reading
from .. import register
from .fields import BY_LABEL, FIELDS
from .protocol import BlockReader

log = logging.getLogger(__name__)

__all__ = ["VEDirectDevice"]

BAUD = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    115200: termios.B115200,
}


class VEDirectDevice(StreamingDevice):
    """Reads one VE.Direct port. Suits the SmartShunt and SmartSolar MPPT alike.

    The two emit different label sets over the identical protocol, so one
    driver covers both; `fields` advertises every label this driver knows and a
    given device simply never sends the ones that do not apply to it.
    """

    fields: tuple[Field, ...] = FIELDS

    def __init__(
        self,
        name: str,
        port: str,
        *,
        baud: int = 19200,
        emit_interval: float = 30.0,
        stale_after: float = 15.0,
        backoff_initial: float = 5.0,
        backoff_max: float = 300.0,
    ) -> None:
        super().__init__(name, backoff_initial=backoff_initial, backoff_max=backoff_max)
        self.port = port
        self.baud = baud
        self.emit_interval = emit_interval
        self.stale_after = stale_after

        self._fd: int | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self._reader = BlockReader()
        self._latest: dict[str, str] = {}
        self._last_emit = 0.0

    # --- port -------------------------------------------------------------

    async def open(self) -> None:
        if self.baud not in BAUD:
            raise DeviceError(f"unsupported baud {self.baud}; known: {sorted(BAUD)}")
        try:
            fd = os.open(self.port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise DeviceError(f"cannot open {self.port}: {exc}") from exc

        try:
            attrs = termios.tcgetattr(fd)
            # Fully raw: no input processing at all. In particular no IXON /
            # IXOFF, which would eat 0x11/0x13 out of the data, and no ICRNL,
            # which would rewrite the \r\n record separators the framing needs.
            attrs[0] = 0  # iflag
            attrs[1] = 0  # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0  # lflag: non-canonical, no echo
            attrs[4] = BAUD[self.baud]  # ispeed
            attrs[5] = BAUD[self.baud]  # ospeed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error as exc:
            os.close(fd)
            raise DeviceError(f"cannot configure {self.port}: {exc}") from exc

        self._fd = fd
        self._reader = BlockReader()
        self._latest = {}
        asyncio.get_running_loop().add_reader(fd, self._on_readable)
        log.info("[%s] reading %s at %d baud", self.name, self.port, self.baud)

    def _on_readable(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            log.warning("[%s] read error: %s", self.name, exc)
            self._detach()
            return
        if data:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(data)

    def _detach(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)

    async def close(self) -> None:
        self._detach()

    # --- stream -----------------------------------------------------------

    def _build_reading(self) -> Reading:
        values: dict[str, float] = {}
        text: dict[str, str] = {}
        for label, raw in self._latest.items():
            spec = BY_LABEL.get(label)
            if spec is None:
                continue  # a label this driver does not model yet
            if spec.kind == "text":
                text[spec.name] = raw
                continue
            try:
                number = int(raw)
            except ValueError:
                try:
                    number = float(raw)  # type: ignore[assignment]
                except ValueError:
                    log.debug("[%s] %s=%r is not numeric", self.name, label, raw)
                    continue
            scaled = number * spec.scale if spec.scale != 1 else float(number)
            # 0.001 and 0.01 scaling on ints reintroduces binary float noise
            values[spec.name] = round(scaled, 3 if spec.scale == 0.001 else 2)
        return Reading(source=self.name, values=values, text=text, taken_at=datetime.now(UTC))

    async def stream(self) -> AsyncIterator[Reading]:
        loop = asyncio.get_running_loop()
        last_block = loop.time()
        while True:
            try:
                async with asyncio.timeout(self.stale_after):
                    chunk = await self._queue.get()
            except TimeoutError as exc:
                # A VE.Direct device transmits unprompted about once a second.
                # Silence is therefore a fault we can see directly, without
                # having to infer staleness from values that stop changing.
                raise DeviceTimeout(f"no data on {self.port} for {self.stale_after}s") from exc

            framed = False
            for block in self._reader.feed(chunk):
                self._latest.update(block)
                framed = True

            now = loop.time()
            if framed:
                last_block = now
            elif now - last_block >= self.stale_after:
                # Bytes ARE arriving, so the silence check above can never fire,
                # yet none of them frame. Without this a noisy link streams
                # forever, emits nothing, and reports no fault - which from
                # outside is indistinguishable from a healthy becalmed system.
                # The rejection counts go in the message because they separate
                # "the wire is quiet" from "the wire is lying", which need
                # different repairs: a cable or a baud rate, not a reboot.
                # pending distinguishes the two noise modes, which have
                # different causes: blocks that FRAME but fail checksum push
                # blocks_bad up (a marginal cable, interference), while bytes
                # that never even contain a Checksum record leave both counters
                # at zero and pile up in the buffer instead (wrong baud rate,
                # wrong port, a device speaking something else entirely).
                raise DeviceTimeout(
                    f"data arriving on {self.port} but no valid block for "
                    f"{now - last_block:.1f}s ({self._reader.blocks_bad} rejected, "
                    f"{self._reader.blocks_ok} accepted since connect, "
                    f"{self._reader.pending} bytes unframed)"
                )

            # Emit only once the instantaneous block has been seen, so a
            # reading is never published from history counters alone.
            if "V" in self._latest and now - self._last_emit >= self.emit_interval:
                self._last_emit = now
                yield self._build_reading()


@register("victron_vedirect", FIELDS)
def _build(
    name: str,
    port: str,
    baud: int = 19200,
    interval: float = 30.0,
    **kwargs: float,
) -> VEDirectDevice:
    # `interval` is the shared config name for how often a device reports.
    return VEDirectDevice(name, port, baud=baud, emit_interval=interval, **kwargs)
