"""Victron "Instant Readout" over BLE - a passive listener, not a connection.

Victron devices broadcast their live state in BLE advertisements. This driver
listens; it never connects, never pairs and never holds the device. That has
three consequences worth knowing before choosing it over VE.Direct:

- **It costs the device nothing** and it cannot lock anyone else out. A
  VE.Direct port is one cable to one host; advertisements are heard by every
  listener in range at once, VictronConnect included.
- **It needs no cable**, which is the whole point when one VE.Direct lead has
  to serve several devices.
- **It carries less.** The battery monitor record holds the live state and
  nothing else: no lifetime history, no charge cycles, no seconds-since-full.
  If those matter, keep the cable. This driver is for the live numbers and for
  people whose device has no VE.Direct port free at all.

The per-device encryption key comes from VictronConnect: connect to the
device, gear icon, Product info, "Instant readout via Bluetooth", show the
encryption key. It is per device - a key for one SmartShunt will not decrypt
another, and the protocol layer says so plainly rather than returning noise.

Several devices can be listened to at once by configuring one `[[device]]`
block each; each opens its own scanner. On a host with a single adapter that
is several BlueZ discovery sessions against one radio, which works but has not
been characterised under load - if advertisements start going missing, that is
the first thing to suspect.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from bleak import BleakScanner

from ...device import Device, DeviceError, StreamingDevice
from ...fields import Field
from ...reading import Reading
from .. import register
from . import protocol
from .decode import decode_battery_monitor
from .records import FIELDS

log = logging.getLogger(__name__)


class VictronBLEDevice(StreamingDevice):
    """Listens for one Victron device's Instant Readout advertisements."""

    fields: tuple[Field, ...] = FIELDS

    def __init__(
        self,
        name: str,
        address: str,
        encryption_key: str,
        *,
        adapter: str | None = None,
        stale_after: float = 120.0,
        backoff_initial: float = 5.0,
        backoff_max: float = 300.0,
    ) -> None:
        super().__init__(name, backoff_initial=backoff_initial, backoff_max=backoff_max)
        self.address = address.upper()
        self.adapter = adapter
        self.stale_after = stale_after
        try:
            self._key = bytes.fromhex(encryption_key.strip())
        except ValueError as exc:
            raise DeviceError(f"encryption key is not hex: {exc}") from exc
        if len(self._key) != protocol.KEY_LEN:
            raise DeviceError(
                f"encryption key must be {protocol.KEY_LEN} bytes "
                f"({protocol.KEY_LEN * 2} hex characters), got {len(self._key)}"
            )
        self._scanner: BleakScanner | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)
        self._unhandled: set[int] = set()

    def _on_advertisement(self, device: Any, advertisement: Any) -> None:
        if device.address.upper() != self.address:
            return
        payload = (advertisement.manufacturer_data or {}).get(protocol.VICTRON_COMPANY_ID)
        if not payload:
            return
        with contextlib.suppress(asyncio.QueueFull):
            # Dropping the oldest would mean holding state to do it; an
            # advertisement is a snapshot, so a full queue means the consumer
            # is behind and the newest frame will be along in a second anyway.
            self._queue.put_nowait(bytes(payload))

    async def open(self) -> None:
        kwargs: dict[str, Any] = {"detection_callback": self._on_advertisement}
        if self.adapter:
            kwargs["adapter"] = self.adapter
        scanner = BleakScanner(**kwargs)
        try:
            await scanner.start()
        except Exception as exc:  # bleak raises BleakError and OSError alike
            raise DeviceError(f"could not start BLE scan: {exc}") from exc
        self._scanner = scanner
        log.info(
            "[%s] listening for Victron advertisements from %s%s",
            self.name,
            self.address,
            f" on {self.adapter}" if self.adapter else "",
        )

    async def close(self) -> None:
        scanner, self._scanner = self._scanner, None
        if scanner is not None:
            with contextlib.suppress(Exception):
                await scanner.stop()
        while not self._queue.empty():
            self._queue.get_nowait()

    async def stream(self) -> AsyncIterator[Reading]:
        while True:
            try:
                async with asyncio.timeout(self.stale_after):
                    payload = await self._queue.get()
            except TimeoutError as exc:
                raise DeviceError(
                    f"no advertisement from {self.address} in {self.stale_after}s "
                    f"- out of range, powered down, or Instant Readout turned off"
                ) from exc

            try:
                record_type, _counter, plaintext = protocol.parse(payload, self._key)
            except protocol.NotAProductAdvertisement:
                # Expected: this device also emits other advertisement types.
                continue
            except protocol.WrongKeyError as exc:
                # Not recoverable by retrying - fail loudly so it gets fixed.
                raise DeviceError(str(exc)) from exc
            except protocol.ProtocolError as exc:
                log.warning("[%s] undecodable advertisement: %s", self.name, exc)
                continue

            if record_type != protocol.RECORD_BATTERY_MONITOR:
                if record_type not in self._unhandled:
                    self._unhandled.add(record_type)
                    known = protocol.RECORD_NAMES.get(record_type, "unknown")
                    log.warning(
                        "[%s] record type 0x%02x (%s) is not decoded yet; no field of it "
                        "has been verified against hardware, so it is skipped rather "
                        "than guessed at",
                        self.name,
                        record_type,
                        known,
                    )
                continue

            try:
                yield decode_battery_monitor(plaintext, source=self.name)
            except protocol.ProtocolError as exc:
                log.warning("[%s] %s", self.name, exc)
                continue


@register("victron_ble", FIELDS)
def _build(
    name: str,
    address: str,
    encryption_key: str | None = None,
    encryption_key_env: str | None = None,
    **kwargs: Any,
) -> Device:
    """Build from config.

    The key may be given inline as `encryption_key`, or named with
    `encryption_key_env` and kept in the environment. The environment wins,
    matching how the InfluxDB credentials are handled: a config file gets
    copied, backed up and pasted into terminals, and this repository is public.
    """
    if encryption_key_env:
        from_env = os.environ.get(encryption_key_env)
        if from_env:
            encryption_key = from_env
        elif not encryption_key:
            raise DeviceError(
                f"{encryption_key_env} is not set and no inline encryption_key was given"
            )
    if not encryption_key:
        raise DeviceError(
            "victron_ble needs an `encryption_key` (or `encryption_key_env`); "
            "read it from VictronConnect under Product info -> Instant readout via Bluetooth"
        )
    return VictronBLEDevice(name, address, encryption_key, **kwargs)
