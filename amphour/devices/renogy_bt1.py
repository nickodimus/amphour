"""BLE transport for the Renogy BT-1 / BT-TH module, over bleak.

Protocol observations that shaped this, all from a live capture on 2026-09-09:

  * The reply is one ATT notification of 73 bytes. It IS fragmented on air into
    three ACL packets, but BlueZ reassembles before the value reaches us.
    We still accumulate rather than assume one notification, because that is a
    property of the negotiated MTU on one particular pairing, not of the
    protocol - a smaller MTU elsewhere would deliver genuine fragments.

  * Reply latency is ~150 ms after the write response.

  * The module accepts exactly one BLE connection at a time. A second client
    does not queue, it fails - so nothing else may hold the device while this
    runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from bleak import BleakClient, BleakScanner
from bleak.args.bluez import BlueZClientArgs, BlueZScannerArgs
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDeviceNotFoundError, BleakError

from ..transport import TransportError, TransportTimeout

log = logging.getLogger(__name__)

NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ffd1-0000-1000-8000-00805f9b34fb"


class RenogyBT1Transport:
    """One BLE connection to a BT-1 module."""

    def __init__(
        self,
        address: str,
        *,
        adapter: str | None = None,
        alias: str | None = None,
        connect_timeout: float = 30.0,
        response_timeout: float = 10.0,
        scan_timeout: float = 15.0,
    ) -> None:
        self.address = address
        self.alias = alias
        self.adapter = adapter
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout
        self.scan_timeout = scan_timeout

        self._client: BleakClient | None = None
        self._buffer = bytearray()
        self._want: int = 0
        self._complete: asyncio.Future[bytes] | None = None

    # --- lifecycle --------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def _scanner_args(self) -> BlueZScannerArgs:
        return {"adapter": self.adapter} if self.adapter else {}

    async def _scan_for_device(self) -> BLEDevice | None:
        """Find the device by advertised name if configured, else by address.

        BlueZ will only connect to an address it already knows about, so a
        cold start - or a device that has been away long enough to be evicted
        from bluetoothd's cache - fails with BleakDeviceNotFoundError until a
        scan has seen it advertise. Passing the discovered BLEDevice to
        BleakClient sidesteps the cache entirely.

        Note that a BLE peripheral stops advertising while another central is
        connected to it. If the device is held by something else, this scan
        finds nothing however long it runs.
        """
        if self.alias:
            log.info("scanning up to %.0fs for alias %r", self.scan_timeout, self.alias)
            return await BleakScanner.find_device_by_name(
                self.alias, timeout=self.scan_timeout, bluez=self._scanner_args()
            )
        log.info("scanning up to %.0fs for %s", self.scan_timeout, self.address)
        return await BleakScanner.find_device_by_address(
            self.address, timeout=self.scan_timeout, bluez=self._scanner_args()
        )

    async def _open(self, target: BLEDevice | str) -> BleakClient:
        client_args: BlueZClientArgs = {"adapter": self.adapter} if self.adapter else {}
        client = BleakClient(target, timeout=self.connect_timeout, bluez=client_args)
        await client.connect()
        await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        return client

    async def connect(self) -> None:
        # Fast path: connect straight to the address, which works whenever
        # BlueZ already knows the device. Fall back to a scan only when it
        # does not, so the steady-state reconnect stays quick.
        target: BLEDevice | str = self.address
        if self.alias:
            found = await self._scan_for_device()
            if found is None:
                raise TransportError(f"no device advertising alias {self.alias!r}")
            target = found

        client: BleakClient | None = None
        try:
            try:
                client = await self._open(target)
            except BleakDeviceNotFoundError:
                if self.alias:
                    raise
                log.info("%s unknown to BlueZ; scanning for it", self.address)
                found = await self._scan_for_device()
                if found is None:
                    raise TransportError(
                        f"{self.address} not found by scan. It may be out of range, "
                        f"powered down, or already connected to another client - a "
                        f"BLE peripheral stops advertising while a central holds it"
                    ) from None
                client = await self._open(found)
        except BleakError as exc:
            if client is not None:
                # best effort: the connection is already failing, a failure to
                # tear it down adds nothing the caller can act on
                with contextlib.suppress(BleakError):
                    await client.disconnect()
            raise TransportError(f"connect failed: {exc}") from exc

        self._client = client
        log.info("connected to %s", self.address)

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except BleakError as exc:
            log.warning("error while disconnecting (ignored): %s", exc)

    # --- request/response -------------------------------------------------

    def _on_notify(self, _characteristic: object, data: bytearray) -> None:
        """Accumulate notification payloads until the expected length arrives."""
        fut = self._complete
        if fut is None or fut.done():
            log.debug("notification with no request outstanding (%d bytes)", len(data))
            return
        self._buffer.extend(data)
        if len(self._buffer) >= self._want:
            fut.set_result(bytes(self._buffer[: self._want]))

    async def request(self, payload: bytes, expected_len: int) -> bytes:
        if not self.is_connected or self._client is None:
            raise TransportError("not connected")

        loop = asyncio.get_running_loop()
        self._buffer.clear()
        self._want = expected_len
        self._complete = loop.create_future()
        try:
            await self._client.write_gatt_char(WRITE_CHAR_UUID, payload, response=True)
            async with asyncio.timeout(self.response_timeout):
                return await self._complete
        except TimeoutError as exc:
            raise TransportTimeout(
                f"no complete reply in {self.response_timeout}s "
                f"(got {len(self._buffer)}/{expected_len} bytes)"
            ) from exc
        except BleakError as exc:
            raise TransportError(f"request failed: {exc}") from exc
        finally:
            self._complete = None
