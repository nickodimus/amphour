"""Runs every configured device concurrently and fans readings out to sinks.

Replaces the single-device Poller. Each device gets its own task under an
asyncio.TaskGroup, so one device failing and retrying does not stall another -
a BLE module that has wandered off must not hold up a serial shunt that is
working perfectly.

A device's run() is not expected to return. If one does, that is a bug in the
driver and the supervisor says so rather than silently running with fewer
devices than configured.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence

from .device import Device
from .reading import Reading
from .sinks.base import Sink

log = logging.getLogger(__name__)


class Supervisor:
    def __init__(self, devices: Sequence[Device], sinks: Sequence[Sink], *,
                 retry_initial: float = 15.0, retry_max: float = 300.0,
                 stable_run: float = 120.0) -> None:
        self.devices = list(devices)
        self.sinks = list(sinks)
        self._stop = asyncio.Event()
        # Backoff for restarting a device whose run() raised -- see _guard.
        self._retry_initial = retry_initial
        self._retry_max = retry_max
        self._stable_run = stable_run

    def stop(self) -> None:
        self._stop.set()

    async def _emit(self, reading: Reading) -> None:
        log.info("%s", reading)
        for sink in self.sinks:
            try:
                await sink.publish(reading)
            except Exception:
                # One sink failing must never stop another, nor a device.
                log.exception("sink %s failed to publish", sink.name)

    async def _guard(self, device: Device) -> None:
        """Run one device forever, restarting it after a backoff if it raises.

        A driver's run() carries its own retry loop for connect/read failures.
        When something escapes that loop and raises -- or run() returns -- the
        device used to STOP for the life of the process, so a transient glitch
        (a Renogy BLE module that wandered off, a controller briefly unreachable)
        stayed dead until a manual `systemctl restart amphour`. Now the
        supervisor restarts the device after a pause that climbs to a cap, so it
        recovers on its own. A raise on one device never touches another either
        way -- independence is the whole reason this supervisor exists.
        """
        delay = self._retry_initial
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await device.run(self._emit)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] driver raised", device.name)
            else:
                log.error("[%s] run() returned; a driver should run until cancelled",
                          device.name)
            if self._stop.is_set():
                break
            # A device that ran healthily for a while before failing backs off
            # from scratch; one that fails fast keeps climbing to the cap, so a
            # genuinely dead device does not spin.
            if time.monotonic() - started >= self._stable_run:
                delay = self._retry_initial
            log.warning("[%s] restarting in %.0fs (other devices unaffected)",
                        device.name, delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break  # stop was requested during the backoff
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, self._retry_max)

    async def run(self) -> None:
        log.info(
            "starting %d device(s): %s",
            len(self.devices),
            ", ".join(d.name for d in self.devices),
        )
        tasks: list[asyncio.Task[None]] = []
        try:
            async with asyncio.TaskGroup() as tg:
                for device in self.devices:
                    tasks.append(tg.create_task(self._guard(device), name=f"device:{device.name}"))
                await self._stop.wait()
                for task in tasks:
                    task.cancel()
        except* asyncio.CancelledError:
            pass
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        for sink in self.sinks:
            try:
                await sink.aclose()
            except Exception:
                log.exception("error closing sink %s", sink.name)
