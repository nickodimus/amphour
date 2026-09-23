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
from collections.abc import Sequence

from .device import Device
from .reading import Reading
from .sinks.base import Sink

log = logging.getLogger(__name__)


class Supervisor:
    def __init__(self, devices: Sequence[Device], sinks: Sequence[Sink]) -> None:
        self.devices = list(devices)
        self.sinks = list(sinks)
        self._stop = asyncio.Event()

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
        try:
            await device.run(self._emit)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A driver raising something its own retry loop does not recognise
            # used to propagate through the TaskGroup and end the process, so a
            # single malformed frame on one device silenced every other device
            # too. Independence is the whole reason this supervisor exists: the
            # failing device stops and says so loudly, the rest keep reporting.
            log.exception(
                "[%s] driver raised; this device has STOPPED, others continue", device.name
            )
        else:
            log.error("[%s] run() returned; a driver should run until cancelled", device.name)

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
