"""Poll-loop behaviour, exercised with a replay transport instead of a radio.

This is the half of the system the predecessor had no way to test at all: its
sleep lived inside the BLE callback, so the loop and the transport could not be
separated.
"""

from __future__ import annotations

import asyncio

import pytest

from amphour import protocol
from amphour.config import from_dict
from amphour.poller import Poller
from amphour.reading import Reading
from amphour.transport import TransportError

FAST = {
    "device": {"address": "AA:BB:CC:DD:EE:FF"},
    "poll": {"interval": 0.01, "backoff_initial": 0.01, "backoff_max": 0.02},
    "prometheus": {"enabled": False},
    "influxdb": {"enabled": True, "url": "http://x", "database": "y"},
}


class ReplayTransport:
    """Serves canned frames, and can be scripted to fail."""

    def __init__(
        self, frames: list[bytes], *, fail_at: set[int] | None = None, connect_failures: int = 0
    ) -> None:
        self.frames = frames
        self.fail_at = fail_at or set()
        self.connect_failures = connect_failures
        self.connects = 0
        self.disconnects = 0
        self.requests = 0
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connects += 1
        if self.connects <= self.connect_failures:
            raise TransportError(f"scripted connect failure {self.connects}")
        self._connected = True

    async def disconnect(self) -> None:
        if self._connected:
            self.disconnects += 1
        self._connected = False

    async def request(self, payload: bytes, expected_len: int) -> bytes:
        assert payload == protocol.build_read_request()
        index = self.requests
        self.requests += 1
        if index in self.fail_at:
            raise TransportError(f"scripted request failure at {index}")
        return self.frames[index % len(self.frames)]


class RecordingSink:
    name = "recording"

    def __init__(self, *, explode: bool = False) -> None:
        self.readings: list[Reading] = []
        self.closed = False
        self.explode = explode

    async def publish(self, reading: Reading) -> None:
        if self.explode:
            raise RuntimeError("this sink is broken on purpose")
        self.readings.append(reading)

    async def aclose(self) -> None:
        self.closed = True


async def run_until(poller: Poller, sink: RecordingSink, count: int, timeout: float = 5.0):
    task = asyncio.create_task(poller.run())
    async with asyncio.timeout(timeout):
        while len(sink.readings) < count and not task.done():
            await asyncio.sleep(0.005)
    poller.stop()
    await task
    return task


@pytest.fixture
def frames(night_capture) -> list[bytes]:
    return [bytes.fromhex(e["frame_hex"]) for e in night_capture]


async def test_happy_path_publishes_decoded_readings(frames):
    transport, sink = ReplayTransport(frames), RecordingSink()
    poller = Poller(transport, [sink], from_dict(FAST))
    await run_until(poller, sink, 3)
    assert len(sink.readings) >= 3
    assert sink.readings[0].values["battery_voltage"] == 11.5
    assert transport.connects == 1


async def test_a_corrupt_frame_is_never_published(frames):
    corrupt = bytearray(frames[0])
    corrupt[10] ^= 0xFF
    transport = ReplayTransport([bytes(corrupt), frames[1]])
    sink = RecordingSink()
    poller = Poller(transport, [sink], from_dict(FAST))
    await run_until(poller, sink, 2)
    # every published reading came from a frame that passed its CRC
    assert all(r.values["battery_voltage"] > 0 for r in sink.readings)
    assert len(sink.readings) >= 2


async def test_repeated_bad_frames_force_a_reconnect(frames):
    corrupt = bytearray(frames[0])
    corrupt[10] ^= 0xFF
    transport = ReplayTransport([bytes(corrupt)])
    sink = RecordingSink()
    poller = Poller(transport, [sink], from_dict(FAST))
    task = asyncio.create_task(poller.run())
    async with asyncio.timeout(5.0):
        while transport.connects < 2:
            await asyncio.sleep(0.005)
    poller.stop()
    await task
    assert transport.disconnects >= 1


async def test_transport_failure_reconnects_and_recovers(frames):
    transport = ReplayTransport(frames, fail_at={1, 2})
    sink = RecordingSink()
    poller = Poller(transport, [sink], from_dict(FAST))
    await run_until(poller, sink, 3)
    assert transport.connects > 1
    assert len(sink.readings) >= 3


async def test_connect_failures_back_off_then_succeed(frames):
    transport = ReplayTransport(frames, connect_failures=3)
    sink = RecordingSink()
    poller = Poller(transport, [sink], from_dict(FAST))
    await run_until(poller, sink, 1)
    assert transport.connects == 4
    assert sink.readings


async def test_one_broken_sink_does_not_stop_the_loop_or_the_others(frames):
    good, bad = RecordingSink(), RecordingSink(explode=True)
    poller = Poller(ReplayTransport(frames), [bad, good], from_dict(FAST))
    await run_until(poller, good, 3)
    assert len(good.readings) >= 3


async def test_stop_closes_the_transport_and_every_sink(frames):
    transport, sink = ReplayTransport(frames), RecordingSink()
    poller = Poller(transport, [sink], from_dict(FAST))
    await run_until(poller, sink, 1)
    assert sink.closed
    assert not transport.is_connected


async def test_failures_are_reported_to_the_health_recorder(frames):
    class Recorder:
        def __init__(self) -> None:
            self.count = 0

        def record_failure(self) -> None:
            self.count += 1

    recorder = Recorder()
    transport = ReplayTransport(frames, fail_at={0, 1})
    sink = RecordingSink()
    poller = Poller(transport, [sink], from_dict(FAST), on_failure=recorder)
    await run_until(poller, sink, 1)
    assert recorder.count >= 2
