"""What a device driver is.

The first version of this abstraction was request/response: send bytes, get
bytes back. That fits the Renogy BT-1, which answers a Modbus read, and does
not fit Victron VE.Direct at all - a VE.Direct device streams text blocks at
you once a second, unprompted, with nothing to request.

So the abstraction is deliberately looser: a device runs until cancelled and
emits readings however it gets them. Drivers that poll use PollingDevice, which
supplies the connect/retry/backoff loop; drivers that read a stream use
StreamingDevice. Neither is required - a driver only has to implement run().
"""

from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol, runtime_checkable

from .fields import Field
from .reading import Reading

log = logging.getLogger(__name__)

Emit = Callable[[Reading], Awaitable[None]]


class DeviceError(Exception):
    """A driver could not talk to its device. Retryable."""


class DeviceTimeout(DeviceError):
    """No data arrived within the deadline."""


@runtime_checkable
class Device(Protocol):
    """A source of readings."""

    name: str
    fields: tuple[Field, ...]

    async def run(self, emit: Emit) -> None:
        """Produce readings until cancelled. Must not return normally."""
        ...


class _RetryingDevice(abc.ABC):
    """Shared connect/retry/backoff, so no driver reinvents it."""

    def __init__(
        self,
        name: str,
        *,
        backoff_initial: float = 5.0,
        backoff_max: float = 300.0,
    ) -> None:
        self.name = name
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self._backoff = backoff_initial
        self._on_failure: Callable[[str], None] | None = None

    def set_failure_hook(self, hook: Callable[[str], None]) -> None:
        self._on_failure = hook

    def _note_failure(self) -> None:
        if self._on_failure is not None:
            self._on_failure(self.name)

    @abc.abstractmethod
    async def open(self) -> None:
        """Establish the connection. Raise DeviceError if it cannot be made."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Tear down. Must not raise."""

    async def _sleep_backoff(self) -> None:
        log.warning("[%s] retrying in %.0fs", self.name, self._backoff)
        await asyncio.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, self.backoff_max)

    def _reset_backoff(self) -> None:
        self._backoff = self.backoff_initial


class PollingDevice(_RetryingDevice):
    """For devices that answer a request: connect, ask, decode, wait, repeat."""

    def __init__(self, name: str, *, interval: float = 30.0, **kwargs: float) -> None:
        super().__init__(name, **kwargs)
        self.interval = interval

    @abc.abstractmethod
    async def poll(self) -> Reading:
        """One request/response/decode cycle. Raise DeviceError on failure."""

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
                    reading = await self.poll()
                    # Backoff resets on PROGRESS, never on a successful open.
                    # A device that connects perfectly and never answers a read
                    # would otherwise clear the counter every cycle and retry at
                    # backoff_initial forever, so the backoff would never grow.
                    self._reset_backoff()
                    await emit(reading)
                    await asyncio.sleep(self.interval)
            except DeviceError as exc:
                self._note_failure()
                log.warning("[%s] %s", self.name, exc)
            except asyncio.CancelledError:
                await self.close()
                raise
            finally:
                await self.close()
            # EVERY failure path backs off before reconnecting, not just the
            # connect path. Without this, a device that opened fine and failed
            # each poll reconnected as fast as the loop allowed - measured at
            # 9479 attempts in 0.3s. Worse, nothing on that path suspends, so
            # the loop never yielded to the event loop at all and every other
            # device stopped with it. This sleep is the backoff AND the only
            # guaranteed suspension point on the failure path.
            await self._sleep_backoff()


class StreamingDevice(_RetryingDevice):
    """For devices that push data continuously, like Victron VE.Direct.

    There is nothing to request; the driver reads whatever arrives. Staleness
    is therefore detectable directly - no bytes within a timeout - rather than
    having to be inferred from values that stop changing.
    """

    @abc.abstractmethod
    def stream(self) -> AsyncIterator[Reading]:
        """Yield readings as they arrive. Raise DeviceError on failure."""

    async def run(self, emit: Emit) -> None:
        while True:
            try:
                await self.open()
            except DeviceError as exc:
                self._note_failure()
                log.warning("[%s] open failed: %s", self.name, exc)
                await self._sleep_backoff()
                continue

            self._reset_backoff()
            try:
                async for reading in self.stream():
                    await emit(reading)
                log.warning("[%s] stream ended", self.name)
            except DeviceError as exc:
                self._note_failure()
                log.warning("[%s] %s", self.name, exc)
            except asyncio.CancelledError:
                await self.close()
                raise
            finally:
                await self.close()
            await self._sleep_backoff()
