"""EG4 LifePower4 (V1/V2) 48 V server-rack battery over RS485 Modbus.

A PollingDevice: once per interval it sends one Modbus read to a pack's address
and decodes the reply. Same shape as the Renogy driver (Modbus request/response)
but the transport is an RS485 serial line, not a BLE characteristic, and it uses
stdlib termios + asyncio rather than pyserial — the same choice victron_vedirect
made, and for the same reason (one fewer dependency for a job the stdlib does).

SCAFFOLD STATUS (2026-09-15): NOT YET RUN AGAINST HARDWARE. The framing is from
the EG4 protocol doc and tuxntoast/eg4-ll; every field is `unverified` until a
capture from Sky's packs confirms it (see registers.py). Wired and waiting.

TWO PACKS, ONE BUS — the open question to resolve at install. Sky has two packs.
They daisy-chain on ONE RS485 bus (pins 7/8 pack-to-pack), and the monitor reads
them on ONE bus (pins 1/2) at two DIP-set Modbus addresses. amphour's model is
one [[device]] = one source label, so the natural config is two [[device]]
blocks (eg4-1 @ address 1, eg4-2 @ address 2) — BUT both would open the same
serial port and step on each other. Pick one before shipping:
  (a) two USB-RS485 adapters, one bus each — cleanest; each device its own port.
  (b) one adapter, and teach this driver to poll a list of addresses on the one
      port, emitting a Reading per pack. That needs a small change to the poll
      contract (device.run emitting N readings). Left as a TODO.
The single-pack path below is correct and testable as-is against one adapter.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import termios
from typing import Any

from ...device import DeviceError, DeviceTimeout, PollingDevice
from ...fields import Field
from ...reading import Reading
from .. import register
from . import protocol
from .decode import decode
from .registers import FIELDS

log = logging.getLogger(__name__)

BAUD = {
    9600: termios.B9600,
    19200: termios.B19200,
    115200: termios.B115200,
}


class EG4LifePower4Device(PollingDevice):
    fields: tuple[Field, ...] = FIELDS

    def __init__(
        self,
        name: str,
        port: str,
        address: int,
        *,
        baud: int = 9600,
        interval: float = 30.0,
        response_timeout: float = 2.0,
        **kwargs: float,
    ) -> None:
        super().__init__(name, interval=interval, **kwargs)
        self.port = port
        self.address = address
        self.baud = baud
        self.response_timeout = response_timeout
        self._fd: int | None = None
        self._buf = bytearray()
        self._wake: asyncio.Event = asyncio.Event()

    async def open(self) -> None:
        if self.baud not in BAUD:
            raise DeviceError(f"unsupported baud {self.baud}; known: {sorted(BAUD)}")
        if not 1 <= self.address <= 247:
            raise DeviceError(f"address {self.address} out of Modbus range 1..247")
        try:
            fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise DeviceError(f"cannot open {self.port}: {exc}") from exc
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0  # iflag: no IXON/IXOFF/ICRNL — raw bytes
            attrs[1] = 0  # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0  # lflag: non-canonical, no echo
            attrs[4] = BAUD[self.baud]  # ispeed
            attrs[5] = BAUD[self.baud]  # ospeed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except termios.error as exc:
            os.close(fd)
            raise DeviceError(f"cannot configure {self.port}: {exc}") from exc
        self._fd = fd
        self._buf.clear()
        asyncio.get_running_loop().add_reader(fd, self._on_readable)
        log.info("[%s] EG4 pack %d on %s at %d baud", self.name, self.address, self.port, self.baud)

    def _on_readable(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            data = os.read(fd, 512)
        except BlockingIOError:
            return
        except OSError as exc:
            log.warning("[%s] read error: %s", self.name, exc)
            return
        if data:
            self._buf.extend(data)
            self._wake.set()

    async def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)

    async def poll(self) -> Reading:
        if self._fd is None:
            raise DeviceError("port not open")
        request = protocol.build_read_request(self.address)
        # RS485 is half-duplex: flush any stale bytes, then transmit.
        with contextlib.suppress(termios.error):
            termios.tcflush(self._fd, termios.TCIFLUSH)
        self._buf.clear()
        self._wake.clear()
        try:
            os.write(self._fd, request)
        except OSError as exc:
            raise DeviceError(f"write failed: {exc}") from exc

        want = protocol.EXPECTED_FRAME_LEN
        try:
            async with asyncio.timeout(self.response_timeout):
                while len(self._buf) < want:
                    await self._wake.wait()
                    self._wake.clear()
        except TimeoutError as exc:
            raise DeviceTimeout(
                f"no complete reply from pack {self.address} in {self.response_timeout}s "
                f"({len(self._buf)}/{want} bytes)"
            ) from exc

        frame = bytes(self._buf[:want])
        return decode(frame, address=self.address, source=self.name)


@register("eg4_lifepower4", FIELDS)
def _build(
    name: str,
    port: str,
    address: int,
    baud: int = 9600,
    interval: float = 30.0,
    **kwargs: Any,
) -> EG4LifePower4Device:
    return EG4LifePower4Device(
        name, port, int(address), baud=int(baud), interval=interval, **kwargs
    )
