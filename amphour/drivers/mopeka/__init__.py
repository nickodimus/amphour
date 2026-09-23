"""Mopeka Pro Check driver: a BLE beacon nobody connects to.

Unlike the Renogy BT-1, this puck is never connected to. It broadcasts its
reading in the advertisement itself, so the driver only listens. That makes it
a StreamingDevice: there is nothing to request, and staleness is directly
observable as silence rather than inferred from values that stop changing.

Two pucks on this property advertise under the same manufacturer id and only
one of them is this device, so `address` is a required filter rather than a
convenience. One of the others is a puck retired on 2026-09-15 with a dead
ultrasonic transducer; it is LOUDER at the collector than the live tank and it
advertises a perfectly well formed reading of zero. Matching on manufacturer
id alone would quietly monitor the wrong tank.

On sharing one adapter: a second BLE driver scanning the same hci device in
the same process is UNVERIFIED as of 2026-09-22. If advertisements start going
missing once two scanners are live, that is the first thing to suspect, and
the fix is one shared scanner feeding both decoders - a deliberate change, not
a scramble. The adapter in use is logged on every open() so the pairing is
visible in the journal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from bleak import BleakScanner
from bleak.args.bluez import BlueZScannerArgs
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from ...device import DeviceError, DeviceTimeout, StreamingDevice
from ...fields import Field
from ...reading import Reading
from .. import register
from .decode import Advertisement, ProtocolError, decode
from .fields import FIELDS, MANUFACTURER_ID, QUALITY_BY_NAME, QUALITY_NAMES, TANKS

log = logging.getLogger(__name__)

_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")

__all__ = ["MopekaProCheckDevice", "decode"]


class MopekaProCheckDevice(StreamingDevice):
    """Listens for one puck's advertisements and reports its tank."""

    fields: tuple[Field, ...] = FIELDS

    def __init__(
        self,
        name: str,
        address: str,
        *,
        tank_type: str = "20LB_V",
        empty_mm: int | None = None,
        full_mm: int | None = None,
        min_quality: str = "low",
        adapter: str | None = None,
        emit_interval: float = 60.0,
        stale_after: float = 300.0,
        backoff_initial: float = 5.0,
        backoff_max: float = 300.0,
    ) -> None:
        super().__init__(name, backoff_initial=backoff_initial, backoff_max=backoff_max)
        # Validated here, not in the decoder. The decoder parses this address
        # to check the MAC tail every advertisement, and that parse runs on the
        # scanner's callback - a place where a raised exception is somewhere
        # between invisible and fatal depending on the backend. A typo in a
        # config file belongs in the startup error, not in the radio path.
        if not _MAC.fullmatch(address):
            raise ValueError(f"address {address!r} is not a BLE MAC like AA:BB:CC:DD:EE:FF")
        self.address = address.upper()
        self.adapter = adapter
        self.emit_interval = emit_interval
        self.stale_after = stale_after

        if min_quality not in QUALITY_BY_NAME:
            raise ValueError(
                f"min_quality {min_quality!r} is not one of {', '.join(QUALITY_BY_NAME)}"
            )
        self.min_quality = QUALITY_BY_NAME[min_quality]

        # Custom geometry wins over the named tank, so an odd tank is
        # expressible without inventing a name for it. Both halves are
        # required together: half a geometry silently paired with half a
        # standard tank's would produce a plausible and wrong percentage.
        if (empty_mm is None) != (full_mm is None):
            raise ValueError("empty_mm and full_mm must be given together, or neither")
        if empty_mm is None or full_mm is None:
            if tank_type not in TANKS:
                raise ValueError(
                    f"unknown tank_type {tank_type!r}; known: {', '.join(sorted(TANKS))}. "
                    f"Or give empty_mm and full_mm for a tank not listed"
                )
            empty_mm, full_mm = TANKS[tank_type]
        if full_mm <= empty_mm:
            raise ValueError(f"full_mm ({full_mm}) must be greater than empty_mm ({empty_mm})")
        self.tank_type = tank_type
        self.empty_mm = empty_mm
        self.full_mm = full_mm

        self._scanner: BleakScanner | None = None
        self._latest: Advertisement | None = None
        self._arrived = asyncio.Event()
        self._ignored_reads = 0

    # --- radio ------------------------------------------------------------

    async def open(self) -> None:
        bluez: BlueZScannerArgs = {"adapter": self.adapter} if self.adapter else {}
        scanner = BleakScanner(detection_callback=self._on_advertisement, bluez=bluez)
        try:
            await scanner.start()
        except (BleakError, OSError) as exc:
            # OSError as well as BleakError: this property resets USB Bluetooth
            # adapters often enough that the overwatch wall counts them, and an
            # adapter that has just vanished must be a RETRYABLE fault. The
            # supervisor stops a device for good when it raises something the
            # retry loop does not recognise, so an unwrapped error here would
            # turn a passing USB glitch into a tank that never reports again.
            raise DeviceError(f"cannot start BLE scan: {exc}") from exc
        self._scanner = scanner
        self._latest = None
        self._arrived.clear()
        log.info(
            "[%s] listening for %s on %s (tank %s, %d-%dmm, min quality %s)",
            self.name,
            self.address,
            self.adapter or "the default adapter",
            self.tank_type,
            self.empty_mm,
            self.full_mm,
            QUALITY_NAMES[self.min_quality],
        )

    async def close(self) -> None:
        scanner, self._scanner = self._scanner, None
        if scanner is None:
            return
        with contextlib.suppress(BleakError, OSError):
            await scanner.stop()

    def _on_advertisement(self, device: BLEDevice, data: AdvertisementData) -> None:
        if device.address.upper() != self.address:
            return
        payload = data.manufacturer_data.get(MANUFACTURER_ID)
        if payload is None:
            return
        try:
            adv = decode(bytes(payload), address=self.address)
        except ProtocolError as exc:
            # Not a dead radio, just one advertisement we cannot read. Log and
            # wait for the next; the puck sends another within seconds.
            log.debug("[%s] undecodable advertisement: %s", self.name, exc)
            return

        if adv.quality < self.min_quality:
            self._ignored_reads += 1
            log.debug(
                "[%s] read quality %s below minimum %s (%d ignored in a row)",
                self.name,
                QUALITY_NAMES[adv.quality],
                QUALITY_NAMES[self.min_quality],
                self._ignored_reads,
            )
        else:
            self._ignored_reads = 0

        # Latest wins. Readings go out on an interval and propane does not
        # move between advertisements, so queuing them would only build a
        # backlog of values that were already superseded.
        self._latest = adv
        self._arrived.set()

    # --- stream -----------------------------------------------------------

    def _build_reading(self, adv: Advertisement) -> Reading:
        values: dict[str, float] = {
            "sensor_temperature": float(adv.temperature_c),
            "sensor_battery_voltage": adv.battery_volts,
            "sensor_battery_percent": float(adv.battery_percent),
            "tank_read_quality": float(adv.quality),
            "tank_ignored_reads": float(self._ignored_reads),
        }
        # A puck that cannot see the liquid surface reports a well formed zero,
        # not an error. Publishing that zero would draw an empty tank; ESPHome
        # publishes NaN instead, which InfluxDB rejects and which then poisons
        # the write buffer. So the level is simply ABSENT when it is not
        # trustworthy - no zero, no NaN, no point at all - while temperature,
        # battery and quality still publish, which keeps a struggling puck
        # visibly alive rather than making it look dead.
        if adv.quality >= self.min_quality:
            values["tank_distance"] = float(adv.distance_mm)
            values["tank_level"] = float(adv.level_percent(self.empty_mm, self.full_mm))
        return Reading(source=self.name, values=values, taken_at=datetime.now(UTC))

    async def stream(self) -> AsyncIterator[Reading]:
        loop = asyncio.get_running_loop()
        last_emit = 0.0
        while True:
            try:
                async with asyncio.timeout(self.stale_after):
                    await self._arrived.wait()
            except TimeoutError as exc:
                # The puck advertises every few seconds unprompted, so silence
                # this long is a real fault: the puck is gone, its cell is
                # flat, or the adapter has stopped delivering advertisements -
                # the last of which is what a scanner conflict would look like.
                raise DeviceTimeout(
                    f"no advertisement from {self.address} for {self.stale_after:.0f}s"
                ) from exc
            self._arrived.clear()

            adv = self._latest
            if adv is None:
                continue
            now = loop.time()
            if now - last_emit < self.emit_interval:
                continue
            last_emit = now
            yield self._build_reading(adv)


@register("mopeka_ble", FIELDS)
def _build(
    name: str,
    address: str,
    tank_type: str = "20LB_V",
    adapter: str | None = None,
    interval: float = 60.0,
    **kwargs: Any,
) -> MopekaProCheckDevice:
    # `interval` is the shared config name for how often a device reports.
    return MopekaProCheckDevice(
        name, address, tank_type=tank_type, adapter=adapter, emit_interval=interval, **kwargs
    )
