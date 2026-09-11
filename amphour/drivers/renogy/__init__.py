"""Renogy BT-1 / BT-TH driver: Modbus RTU framed over a BLE GATT characteristic.

Protocol facts here were established from a live capture rather than
documentation - see the repo's tests/fixtures and the notes in registers.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from bleak import BleakClient, BleakScanner
from bleak.args.bluez import BlueZClientArgs, BlueZScannerArgs
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDeviceNotFoundError, BleakError

from ...device import DeviceError, DeviceTimeout, PollingDevice
from ...fields import Field
from ...reading import Reading
from .. import register
from . import protocol
from .decode import charging_state_name, decode
from .registers import FIELDS

log = logging.getLogger(__name__)

NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ffd1-0000-1000-8000-00805f9b34fb"

__all__ = ["RenogyBT1Device", "charging_state_name", "decode", "protocol"]


class RenogyBT1Device(PollingDevice):
    """One BLE connection to a BT-1 module, polled on an interval.

    The module accepts exactly one BLE connection at a time. A second client
    does not queue, it fails - so nothing else may hold the device while this
    runs, and while it IS held the module stops advertising, which is why a
    scan for it comes back empty.
    """

    fields: tuple[Field, ...] = FIELDS

    def __init__(
        self,
        name: str,
        address: str,
        *,
        alias: str | None = None,
        adapter: str | None = None,
        interval: float = 30.0,
        connect_timeout: float = 30.0,
        response_timeout: float = 10.0,
        scan_timeout: float = 15.0,
        backoff_initial: float = 5.0,
        backoff_max: float = 300.0,
    ) -> None:
        super().__init__(
            name,
            interval=interval,
            backoff_initial=backoff_initial,
            backoff_max=backoff_max,
        )
        self.address = address
        self.alias = alias
        self.adapter = adapter
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout
        self.scan_timeout = scan_timeout

        self._client: BleakClient | None = None
        self._buffer = bytearray()
        self._want = 0
        self._complete: asyncio.Future[bytes] | None = None
        self._request = protocol.build_read_request()

    # --- connection -------------------------------------------------------

    def _scanner_args(self) -> BlueZScannerArgs:
        return {"adapter": self.adapter} if self.adapter else {}

    async def _scan(self) -> BLEDevice | None:
        if self.alias:
            log.info("[%s] scanning %.0fs for alias %r", self.name, self.scan_timeout, self.alias)
            return await BleakScanner.find_device_by_name(
                self.alias, timeout=self.scan_timeout, bluez=self._scanner_args()
            )
        log.info("[%s] scanning %.0fs for %s", self.name, self.scan_timeout, self.address)
        return await BleakScanner.find_device_by_address(
            self.address, timeout=self.scan_timeout, bluez=self._scanner_args()
        )

    async def _open_client(self, target: BLEDevice | str) -> BleakClient:
        client_args: BlueZClientArgs = {"adapter": self.adapter} if self.adapter else {}
        client = BleakClient(target, timeout=self.connect_timeout, bluez=client_args)
        await client.connect()
        await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        return client

    async def open(self) -> None:
        target: BLEDevice | str = self.address
        if self.alias:
            found = await self._scan()
            if found is None:
                raise DeviceError(f"no device advertising alias {self.alias!r}")
            target = found

        client: BleakClient | None = None
        try:
            try:
                client = await self._open_client(target)
            except BleakDeviceNotFoundError:
                if self.alias:
                    raise
                # BlueZ will not connect to an address it has never seen
                # advertise. Scan to populate its cache, then use the
                # discovered BLEDevice directly.
                log.info("[%s] %s unknown to BlueZ; scanning", self.name, self.address)
                found = await self._scan()
                if found is None:
                    raise DeviceError(
                        f"{self.address} not found by scan. It may be out of range, "
                        f"powered down, or already connected to another client - a "
                        f"BLE peripheral stops advertising while a central holds it"
                    ) from None
                client = await self._open_client(found)
        except BleakError as exc:
            if client is not None:
                with contextlib.suppress(BleakError):
                    await client.disconnect()
            raise DeviceError(f"connect failed: {exc}") from exc

        self._client = client
        log.info("[%s] connected to %s", self.name, self.address)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        with contextlib.suppress(BleakError):
            await client.disconnect()

    # --- polling ----------------------------------------------------------

    def _on_notify(self, _characteristic: object, data: bytearray) -> None:
        """Accumulate notification payloads until the expected length arrives.

        On the captured hardware the whole 73-byte reply arrives as a single
        notification, because the negotiated MTU is large enough. That is a
        property of one pairing, not of the protocol, so this accumulates
        rather than assuming it.
        """
        fut = self._complete
        if fut is None or fut.done():
            log.debug("[%s] notification with no request outstanding", self.name)
            return
        self._buffer.extend(data)
        if len(self._buffer) >= self._want:
            fut.set_result(bytes(self._buffer[: self._want]))

    async def poll(self) -> Reading:
        client = self._client
        if client is None or not client.is_connected:
            raise DeviceError("not connected")

        loop = asyncio.get_running_loop()
        self._buffer.clear()
        self._want = protocol.EXPECTED_FRAME_LEN
        self._complete = loop.create_future()
        try:
            await client.write_gatt_char(WRITE_CHAR_UUID, self._request, response=True)
            async with asyncio.timeout(self.response_timeout):
                frame = await self._complete
        except TimeoutError as exc:
            raise DeviceTimeout(
                f"no complete reply in {self.response_timeout}s "
                f"(got {len(self._buffer)}/{self._want} bytes)"
            ) from exc
        except BleakError as exc:
            raise DeviceError(f"request failed: {exc}") from exc
        finally:
            self._complete = None

        try:
            return decode(frame, source=self.name)
        except protocol.ProtocolError as exc:
            # A bad frame is not a dead link; surface it as retryable so the
            # loop tries again rather than tearing the connection down.
            raise DeviceError(f"bad frame: {exc}") from exc


@register("renogy_bt1", FIELDS)
def _build(
    name: str,
    address: str,
    alias: str | None = None,
    adapter: str | None = None,
    interval: float = 30.0,
    **kwargs: float,
) -> RenogyBT1Device:
    return RenogyBT1Device(name, address, alias=alias, adapter=adapter, interval=interval, **kwargs)
