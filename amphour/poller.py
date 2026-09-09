"""The poll loop.

Shape of the thing, and why it differs from the predecessor:

The old app called time.sleep(interval) *inside* the BLE notification
callback, which blocked the GATT event thread for the whole interval. It
worked, but it meant the library could not process anything - including a
disconnect - while waiting. Here the wait is an await in the loop, and the
callback only ever hands the bytes over.

Failure handling separates two kinds of problem:

  transport failures   the link is gone: disconnect, back off, reconnect
  protocol failures    the link is fine but the frame was bad: log it, count
                       it, try again next tick. Only after several in a row do
                       we assume the link is sick and reconnect.

A bad frame is never published. That is the whole point of verifying the CRC.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from . import protocol
from .config import Config
from .reading import Reading, decode
from .sinks.base import Sink
from .transport import Transport, TransportError

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_PROTOCOL_ERRORS = 3


class Poller:
    def __init__(
        self,
        transport: Transport,
        sinks: Sequence[Sink],
        config: Config,
        *,
        on_failure: object = None,
    ) -> None:
        self.transport = transport
        self.sinks = list(sinks)
        self.config = config
        self._on_failure = on_failure
        self._stop = asyncio.Event()
        self._request = protocol.build_read_request()
        self._backoff = config.poll.backoff_initial
        self._protocol_errors = 0

    def stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, but wake early on shutdown. Returns False if we should stop."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return True
        return False

    def _note_failure(self) -> None:
        recorder = getattr(self._on_failure, "record_failure", None)
        if callable(recorder):
            recorder()

    async def read_once(self) -> Reading:
        """One request/response/decode cycle. Raises on any failure."""
        frame = await self.transport.request(self._request, protocol.EXPECTED_FRAME_LEN)
        return decode(frame)

    async def _publish(self, reading: Reading) -> None:
        for sink in self.sinks:
            try:
                await sink.publish(reading)
            except Exception:
                # One sink failing must never stop another, nor the loop.
                log.exception("sink %s failed to publish", sink.name)

    async def run(self) -> None:
        log.info("polling %s every %.0fs", self.config.device.address, self.config.poll.interval)
        try:
            while not self._stop.is_set():
                if not self.transport.is_connected and not await self._connect_with_backoff():
                    break
                try:
                    reading = await self.read_once()
                except TransportError as exc:
                    log.warning("transport error: %s", exc)
                    self._note_failure()
                    await self.transport.disconnect()
                    continue
                except protocol.ProtocolError as exc:
                    self._protocol_errors += 1
                    self._note_failure()
                    log.warning("bad frame (%d in a row): %s", self._protocol_errors, exc)
                    if self._protocol_errors >= MAX_CONSECUTIVE_PROTOCOL_ERRORS:
                        log.error(
                            "%d bad frames in a row, reconnecting",
                            self._protocol_errors,
                        )
                        self._protocol_errors = 0
                        await self.transport.disconnect()
                    if not await self._sleep(self.config.poll.interval):
                        break
                    continue

                self._protocol_errors = 0
                self._backoff = self.config.poll.backoff_initial
                log.info("%s", reading)
                await self._publish(reading)

                if not await self._sleep(self.config.poll.interval):
                    break
        finally:
            await self.aclose()

    async def _connect_with_backoff(self) -> bool:
        """Keep trying to connect until it works or we're told to stop."""
        while not self._stop.is_set():
            try:
                await self.transport.connect()
                self._backoff = self.config.poll.backoff_initial
                return True
            except TransportError as exc:
                self._note_failure()
                log.warning("connect failed (%s); retrying in %.0fs", exc, self._backoff)
                if not await self._sleep(self._backoff):
                    return False
                self._backoff = min(self._backoff * 2, self.config.poll.backoff_max)
        return False

    async def aclose(self) -> None:
        await self.transport.disconnect()
        for sink in self.sinks:
            try:
                await sink.aclose()
            except Exception:
                log.exception("error closing sink %s", sink.name)
