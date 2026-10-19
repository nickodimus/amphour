"""One BLE scanner per adapter, shared by every BLE driver in the process.

Each BLE driver used to open its own ``BleakScanner``. On a host with a single
adapter that is several BlueZ discovery sessions against one radio, and BlueZ
rejects the second with ``org.bluez.Error.InProgress: Operation already in
progress`` -- so the driver that started second (in practice the Renogy, whose
scan-then-connect cycle restarts every poll) never finds its device, even
sitting beside it. The victron_ble, mopeka and renogy driver headers all named
this hazard and pointed here as the fix.

So there is ONE scanner per adapter, started on first use and left running for
the life of the process. Advertisement-only drivers (victron_ble, mopeka)
subscribe and decode what arrives; the Renogy, which must connect, asks for the
freshest ``BLEDevice`` the scanner has seen and then opens a GATT link -- a
connection and a scan coexist happily on one adapter, it is only two
*discoveries* that conflict.

The scanner is unfiltered and active, matching what victron_ble already did on
its own: every device in range is reported, and each driver filters by address
in its own callback. A watchdog restarts the scanner if advertisements stop
arriving entirely -- a BlueZ discovery can be dropped out from under us by an
adapter reset or a bluetoothd restart with no error raised to the callback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

log = logging.getLogger(__name__)

DetectionCallback = Callable[[BLEDevice, AdvertisementData], None]

# If NO advertisement at all arrives for this long, assume the discovery has
# been dropped (adapter reset, bluetoothd restart) and restart the scanner. A
# live radio in a populated area sees something every few seconds; this much
# total silence across every device is the adapter, not any one peripheral.
_IDLE_RESTART_S = 60.0
_WATCHDOG_TICK_S = 10.0


class ScannerHub:
    """One shared ``BleakScanner`` for a single adapter.

    Not a device: it holds no config of its own and never emits readings. It is
    created per adapter by the CLI and injected into each BLE driver, which
    calls :meth:`ensure_started` from its own ``open()`` -- so the scanner comes
    up lazily on the first driver to need it and is shared by the rest.
    """

    def __init__(
        self, adapter: str | None = None, *, idle_restart_s: float = _IDLE_RESTART_S
    ) -> None:
        self.adapter = adapter
        self._idle_restart_s = idle_restart_s
        self._scanner: BleakScanner | None = None
        self._subs: dict[int, DetectionCallback] = {}
        self._next_handle = 0
        # address (UPPER) / advertised name -> (device, monotonic last seen).
        # Both updated on every advertisement; the name index serves the rare
        # alias lookup the Renogy driver supports.
        self._by_addr: dict[str, tuple[BLEDevice, float]] = {}
        self._by_name: dict[str, tuple[BLEDevice, float]] = {}
        self._last_adv = 0.0
        self._lock = asyncio.Lock()
        self._watchdog: asyncio.Task[None] | None = None
        self._closed = False

    # --- lifecycle --------------------------------------------------------

    async def ensure_started(self) -> None:
        """Start the scanner and its watchdog once. Idempotent and safe to call
        concurrently from several drivers' ``open()``."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("scanner hub is closed")
            if self._scanner is None:
                await self._start_locked()
            if self._watchdog is None:
                self._watchdog = asyncio.create_task(
                    self._run_watchdog(), name=f"ble-hub-watchdog:{self.adapter or 'default'}"
                )

    async def _start_locked(self) -> None:
        kwargs: dict[str, Any] = {"detection_callback": self._on_detection}
        if self.adapter:
            kwargs["adapter"] = self.adapter
        scanner = BleakScanner(**kwargs)
        await scanner.start()
        self._scanner = scanner
        self._last_adv = time.monotonic()
        log.info("[ble-hub] scanning on %s", self.adapter or "the default adapter")

    async def _restart(self) -> None:
        async with self._lock:
            if self._closed:
                return
            old, self._scanner = self._scanner, None
            if old is not None:
                with contextlib.suppress(Exception):
                    await old.stop()
            try:
                await self._start_locked()
                log.warning(
                    "[ble-hub] restarted scanner on %s", self.adapter or "the default adapter"
                )
            except Exception as exc:
                # Leave _scanner None; the watchdog will try again next tick,
                # and drivers' ensure_started() will retry on their own backoff.
                log.error("[ble-hub] scanner restart failed: %s", exc)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            wd, self._watchdog = self._watchdog, None
            scanner, self._scanner = self._scanner, None
        if wd is not None:
            wd.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wd
        if scanner is not None:
            with contextlib.suppress(Exception):
                await scanner.stop()

    # --- detection --------------------------------------------------------

    def _on_detection(self, device: BLEDevice, adv: AdvertisementData) -> None:
        now = time.monotonic()
        self._last_adv = now
        self._by_addr[device.address.upper()] = (device, now)
        if adv.local_name:
            self._by_name[adv.local_name] = (device, now)
        # Fan out. A raising subscriber must not stop the others, nor wedge the
        # scanner's dispatch for everyone.
        for cb in list(self._subs.values()):
            try:
                cb(device, adv)
            except Exception:
                log.exception("[ble-hub] subscriber callback raised")

    # --- subscription (advertisement consumers: victron_ble, mopeka) ------

    def subscribe(self, callback: DetectionCallback) -> int:
        handle = self._next_handle
        self._next_handle += 1
        self._subs[handle] = callback
        return handle

    def unsubscribe(self, handle: int) -> None:
        self._subs.pop(handle, None)

    # --- on-demand lookup (connectors that must scan-then-connect: renogy) -

    async def get_device(
        self,
        address: str | None = None,
        *,
        name: str | None = None,
        max_age_s: float = 20.0,
        timeout: float = 15.0,
    ) -> BLEDevice | None:
        """The freshest ``BLEDevice`` seen for ``address`` (or advertised
        ``name``), provided it was seen within ``max_age_s``; otherwise wait up
        to ``timeout`` for a fresh advertisement, then give up with ``None``.

        ``max_age_s`` preserves the "connect only to an address BlueZ has
        freshly seen advertise" rule the Renogy driver depends on."""
        key = address.upper() if address else None
        deadline = time.monotonic() + timeout
        while True:
            hit = self._by_name.get(name) if name else (self._by_addr.get(key) if key else None)
            if hit is not None and (time.monotonic() - hit[1]) <= max_age_s:
                return hit[0]
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.25)

    # --- watchdog ---------------------------------------------------------

    async def _run_watchdog(self) -> None:
        while True:
            await asyncio.sleep(_WATCHDOG_TICK_S)
            if self._closed:
                return
            idle = time.monotonic() - self._last_adv
            if self._scanner is not None and idle > self._idle_restart_s:
                log.warning(
                    "[ble-hub] no advertisements for %.0fs on %s - restarting scanner",
                    self._idle_restart_s,
                    self.adapter or "the default adapter",
                )
                await self._restart()
                # Give the fresh scanner a grace period before judging it idle.
                self._last_adv = time.monotonic()
