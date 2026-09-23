"""EG4 LifePower4 V2 — 48 V server-rack battery over RS485 Modbus RTU.

VERIFIED 2026-09-22 against Sky's two packs. The V2 speaks standard Modbus RTU
(FC 0x03); the wire protocol was captured from EG4 BMS Tools and the register map
cross-checked against its readout (see protocol.py / registers.py). Same shape as
the Renogy driver (Modbus request/response) but the transport is an RS485 serial
line, not a BLE characteristic, and it uses stdlib termios + asyncio rather than
pyserial — the same choice victron_vedirect made, one fewer dependency.

ONE PORT, N PACKS. Both packs share the ONE RS485 monitor line (pins 1/2): the
master answers at address 0x40 (64) directly, and a chained slave answers at
0x3F (63) with the master relaying over the battery-comm link — so the slave
replies a little slower, hence the generous response timeout. Two [[device]]
blocks on one port would fight over the fd, so this driver — not the config —
fans out across addresses: it opens the port once and polls each configured pack
per interval, emitting one Reading per pack tagged `<name>-<label>` (e.g.
`eg4-master`, `eg4-slave`), which is what puts each pack on its own `device=`
metric label.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import termios
from dataclasses import dataclass
from typing import Any

from ...device import DeviceError, DeviceTimeout, Emit, PollingDevice
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


@dataclass(frozen=True, slots=True)
class Pack:
    address: int
    label: str


class EG4LifePower4Device(PollingDevice):
    fields: tuple[Field, ...] = FIELDS

    def __init__(
        self,
        name: str,
        port: str,
        packs: tuple[Pack, ...],
        *,
        baud: int = 9600,
        interval: float = 30.0,
        response_timeout: float = 1.5,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, interval=interval, **kwargs)
        self.port = port
        self.packs = packs
        self.baud = baud
        self.response_timeout = response_timeout
        self._fd: int | None = None
        self._buf = bytearray()
        self._wake: asyncio.Event = asyncio.Event()

    async def open(self) -> None:
        if self.baud not in BAUD:
            raise DeviceError(f"unsupported baud {self.baud}; known: {sorted(BAUD)}")
        for pack in self.packs:
            if not 1 <= pack.address <= 247:
                raise DeviceError(f"address {pack.address} out of Modbus range 1..247")
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
        log.info("[%s] EG4 on %s at %d baud, packs: %s", self.name, self.port, self.baud,
                 ", ".join(f"{p.label}@{p.address}" for p in self.packs))

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

    async def _poll_pack(self, pack: Pack) -> Reading:
        if self._fd is None:
            raise DeviceError("port not open")
        request = protocol.build_read_request(pack.address)
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
                f"no complete reply from {pack.label} (addr {pack.address}) in "
                f"{self.response_timeout}s ({len(self._buf)}/{want} bytes)"
            ) from exc

        frame = bytes(self._buf[:want])
        try:
            return decode(frame, address=pack.address, source=f"{self.name}-{pack.label}")
        except protocol.ProtocolError as exc:
            # A malformed reply is a bad read, not a broken program. Two packs
            # share one half-duplex line and a late reply can land in the next
            # pack's window, so this happens in the field. Raised as DeviceError
            # it becomes the per-pack "carry on" path in run(); raised as a bare
            # ProtocolError it escaped the TaskGroup and killed the process,
            # taking the shunt and the charge controller down with it (11 times
            # on 2026-09-22 before anyone noticed, because systemd restarted it
            # and every health check still said "active").
            raise DeviceError(f"{pack.label} (addr {pack.address}): {exc}") from exc

    async def poll(self) -> Reading:
        # Satisfies PollingDevice's contract (a single reading); run() below is
        # what actually drives the multi-pack loop.
        return await self._poll_pack(self.packs[0])

    async def run(self, emit: Emit) -> None:
        while True:
            try:
                await self.open()
            except DeviceError as exc:
                self._note_failure()
                log.warning("[%s] connect failed: %s", self.name, exc)
                await self._sleep_backoff()
                continue

            try:
                while True:
                    answered = False
                    for pack in self.packs:
                        try:
                            reading = await self._poll_pack(pack)
                        except DeviceError as exc:
                            # One pack missing a cycle (e.g. a slow slave relay)
                            # must not kill the other; log and carry on. Only if
                            # NO pack answers do we treat it as a link failure.
                            log.warning("[%s/%s] %s", self.name, pack.label, exc)
                            continue
                        self._reset_backoff()
                        answered = True
                        await emit(reading)
                    if not answered:
                        raise DeviceError("no configured pack answered")
                    await asyncio.sleep(self.interval)
            except DeviceError as exc:
                self._note_failure()
                log.warning("[%s] %s", self.name, exc)
            finally:
                await self.close()
            await self._sleep_backoff()


def _parse_packs(packs: Any, address: Any, name: str) -> tuple[Pack, ...]:
    """Build the pack list from config: a `packs` list of {address, label}, or a
    single `address` (labelled with the device name)."""
    if packs is not None:
        if not isinstance(packs, list) or not packs:
            raise DeviceError("`packs` must be a non-empty list of {address, label} tables")
        out: list[Pack] = []
        seen: set[str] = set()
        for i, p in enumerate(packs):
            if not isinstance(p, dict) or "address" not in p:
                raise DeviceError(f"packs[{i}] needs an `address`")
            label = str(p.get("label", f"pack{p['address']}"))
            if label in seen:
                raise DeviceError(f"duplicate pack label {label!r}; labels tag the metrics")
            seen.add(label)
            out.append(Pack(address=int(p["address"]), label=label))
        return tuple(out)
    if address is not None:
        return (Pack(address=int(address), label=name),)
    raise DeviceError("configure either `packs = [...]` or a single `address`")


@register("eg4_lifepower4", FIELDS)
def _build(
    name: str,
    port: str,
    packs: Any = None,
    address: Any = None,
    baud: int = 9600,
    interval: float = 30.0,
    response_timeout: float = 1.5,
    **kwargs: Any,
) -> EG4LifePower4Device:
    return EG4LifePower4Device(
        name,
        port,
        _parse_packs(packs, address, name),
        baud=int(baud),
        interval=interval,
        response_timeout=response_timeout,
        **kwargs,
    )
