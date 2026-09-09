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
from bleak.exc import BleakError

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

    async def _resolve_address(self) -> str:
        """Scan for the device if we were given an alias instead of an address.

        BlueZ will happily connect by address without scanning when the device
        is already known, so this only runs when an alias was configured.
        """
        if not self.alias:
            return self.address
        log.info("scanning up to %.0fs for alias %r", self.scan_timeout, self.alias)
        scanner_args: BlueZScannerArgs = {"adapter": self.adapter} if self.adapter else {}
        device = await BleakScanner.find_device_by_name(
            self.alias, timeout=self.scan_timeout, bluez=scanner_args
        )
        if device is None:
            raise TransportError(f"no BLE device advertising alias {self.alias!r}")
        log.info("alias %r resolved to %s", self.alias, device.address)
        return device.address

    async def connect(self) -> None:
        address = await self._resolve_address()
        # bleak 3.x: the `adapter=` kwarg is deprecated in favour of `bluez=`.
        client_args: BlueZClientArgs = {"adapter": self.adapter} if self.adapter else {}
        client = BleakClient(address, timeout=self.connect_timeout, bluez=client_args)
        try:
            await client.connect()
            await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        except BleakError as exc:
            # best effort: the connection is already failing, a failure to
            # tear it down adds nothing the caller can act on
            with contextlib.suppress(BleakError):
                await client.disconnect()
            raise TransportError(f"connect failed: {exc}") from exc
        self._client = client
        log.info("connected to %s", address)

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
