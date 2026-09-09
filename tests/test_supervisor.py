"""Multi-device supervision, with fake devices instead of hardware.

The thing being tested is that devices are genuinely independent: one failing,
retrying, or being slow must not stall another. That property is why the
supervisor exists, and it is invisible in a single-device test.
"""

from __future__ import annotations

import asyncio

import pytest

from amphour.device import DeviceError, PollingDevice, StreamingDevice
from amphour.fields import Field
from amphour.reading import Reading
from amphour.supervisor import Supervisor

FIELDS = (Field("battery_voltage", "volts", "confirmed", "test field"),)


class FakeDevice(PollingDevice):
    """Answers polls from a script; can be told to fail on given attempts."""

    fields = FIELDS

    def __init__(self, name, values, *, fail_polls=(), fail_opens=0, interval=0.01):
        super().__init__(name, interval=interval, backoff_initial=0.01, backoff_max=0.02)
        self.values = list(values)
        self.fail_polls = set(fail_polls)
        self.fail_opens = fail_opens
        self.opens = self.closes = self.polls = 0

    async def open(self):
        self.opens += 1
        if self.opens <= self.fail_opens:
            raise DeviceError(f"scripted open failure {self.opens}")

    async def close(self):
        self.closes += 1

    async def poll(self):
        index = self.polls
        self.polls += 1
        if index in self.fail_polls:
            raise DeviceError(f"scripted poll failure at {index}")
        value = self.values[index % len(self.values)]
        return Reading(source=self.name, values={"battery_voltage": value})


class RecordingSink:
    name = "recording"

    def __init__(self, *, explode=False):
        self.readings: list[Reading] = []
        self.closed = False
        self.explode = explode

    async def publish(self, reading):
        if self.explode:
            raise RuntimeError("this sink is broken on purpose")
        self.readings.append(reading)

    async def aclose(self):
        self.closed = True


async def run_until(sup, predicate, timeout=5.0):
    task = asyncio.create_task(sup.run())
    try:
        async with asyncio.timeout(timeout):
            while not predicate() and not task.done():
                await asyncio.sleep(0.005)
    finally:
        sup.stop()
        await task


async def test_every_device_publishes_tagged_with_its_own_name():
    a = FakeDevice("shunt", [12.8])
    b = FakeDevice("controller", [13.4])
    sink = RecordingSink()
    sup = Supervisor([a, b], [sink])
    await run_until(sup, lambda: len({r.source for r in sink.readings}) == 2)
    sources = {r.source for r in sink.readings}
    assert sources == {"shunt", "controller"}
    by_source = {r.source: r.values["battery_voltage"] for r in sink.readings}
    assert by_source == {"shunt": 12.8, "controller": 13.4}


async def test_one_device_failing_does_not_stall_another():
    """The reason the supervisor runs a task per device rather than a loop.

    A BLE module that has wandered off must not hold up a serial shunt that is
    working perfectly.
    """
    broken = FakeDevice("broken", [1.0], fail_opens=10_000)
    healthy = FakeDevice("healthy", [12.8])
    sink = RecordingSink()
    sup = Supervisor([broken, healthy], [sink])
    await run_until(sup, lambda: len(sink.readings) >= 5)
    assert all(r.source == "healthy" for r in sink.readings)
    assert broken.opens > 1  # it kept retrying the whole time
    assert len(sink.readings) >= 5


async def test_a_device_recovers_after_transient_failures():
    device = FakeDevice("flaky", [12.8], fail_polls={1, 2})
    sink = RecordingSink()
    sup = Supervisor([device], [sink])
    await run_until(sup, lambda: len(sink.readings) >= 4)
    assert device.opens > 1  # reconnected after each failure
    assert len(sink.readings) >= 4


class NeverReadsDevice(PollingDevice):
    """Connects perfectly, then fails every read.

    This is the shape that matters on real hardware and that no other fake
    covers: BLE connects, the peripheral accepts, and then never answers a
    characteristic read. open() succeeding is not evidence of progress.
    """

    fields = FIELDS

    def __init__(self, name="answers-but-never-reads", **kwargs):
        super().__init__(name, interval=0.01, **kwargs)
        self.opens = self.closes = self.polls = 0

    async def open(self):
        self.opens += 1

    async def close(self):
        self.closes += 1

    async def poll(self):
        self.polls += 1
        # This await is load-bearing for the TEST, not for the device. Without
        # the fix, PollingDevice.run's retry loop contains no suspension point
        # at all on this path, so it never yields to the event loop and the
        # whole suite wedges instead of failing. Yielding here lets the test
        # observe the spin and assert on it.
        await asyncio.sleep(0)
        raise DeviceError("connected, but the read never came back")


async def test_a_device_that_connects_but_never_reads_does_not_spin():
    """Regression: PollingDevice backed off on connect failure but not on read
    failure, so a device that opened fine and failed every poll reconnected as
    fast as the event loop allowed."""
    device = NeverReadsDevice(backoff_initial=0.05, backoff_max=1.0)
    sup = Supervisor([device], [RecordingSink()])
    task = asyncio.create_task(sup.run())
    try:
        await asyncio.sleep(0.3)
    finally:
        sup.stop()
        await task
    assert device.opens <= 6, f"reconnected {device.opens}x in 0.3s - not backing off"


async def test_backoff_escalates_while_reads_keep_failing():
    """A successful open must not reset the backoff. Only a reading is progress."""
    device = NeverReadsDevice(backoff_initial=0.02, backoff_max=1.0)
    sup = Supervisor([device], [RecordingSink()])
    task = asyncio.create_task(sup.run())
    try:
        await asyncio.sleep(0.3)
    finally:
        sup.stop()
        await task
    assert device._backoff > 0.02, "backoff never grew; it is being reset every cycle"


class NeverStreamsDevice(StreamingDevice):
    """Opens perfectly, then the stream fails immediately, every time.

    The streaming twin of NeverReadsDevice. With the VE.Direct noise check in
    place this is a real shape, not a hypothetical: a link delivering bytes
    that never frame now raises DeviceTimeout out of stream() on every attempt
    while open() keeps succeeding.
    """

    fields = FIELDS

    def __init__(self, name="opens-but-never-streams", **kwargs):
        super().__init__(name, **kwargs)
        self.opens = self.closes = 0

    async def open(self):
        self.opens += 1

    async def close(self):
        self.closes += 1

    async def stream(self):
        for _ in ():  # never runs; it is what makes this an async generator
            yield
        await asyncio.sleep(0)
        raise DeviceError("connected, but the stream never produced anything")


class OneReadingThenFailsDevice(StreamingDevice):
    """Yields one reading, then fails - the shape that SHOULD reset backoff."""

    fields = FIELDS

    def __init__(self, name="flaky-stream", **kwargs):
        super().__init__(name, **kwargs)
        self.opens = self.closes = 0

    async def open(self):
        self.opens += 1

    async def close(self):
        self.closes += 1

    async def stream(self):
        await asyncio.sleep(0)
        yield Reading(source=self.name, values={"battery_voltage": 12.8})
        raise DeviceError("stream dropped after one reading")


async def test_streaming_backoff_escalates_while_the_stream_keeps_failing():
    """A successful open() must not reset the backoff, same as PollingDevice.

    Without this the device retries at backoff_initial forever: it cannot
    spin, because run() sleeps at the end of its loop, but it never backs
    away from a device that is not coming back either.
    """
    device = NeverStreamsDevice(backoff_initial=0.02, backoff_max=1.0)
    sup = Supervisor([device], [RecordingSink()])
    task = asyncio.create_task(sup.run())
    try:
        await asyncio.sleep(0.3)
    finally:
        sup.stop()
        await task
    assert device._backoff > 0.02, "backoff never grew; it is being reset every cycle"


async def test_a_streaming_device_that_produces_a_reading_resets_its_backoff():
    """The other half: real progress must clear the penalty."""
    device = OneReadingThenFailsDevice(backoff_initial=0.02, backoff_max=1.0)
    sink = RecordingSink()
    sup = Supervisor([device], [sink])
    task = asyncio.create_task(sup.run())
    try:
        await asyncio.sleep(0.3)
    finally:
        sup.stop()
        await task
    assert len(sink.readings) >= 2, "device should keep producing across reconnects"
    assert device._backoff == 0.02, "a reading arrived; the backoff should be back to initial"


async def test_open_failures_back_off_then_succeed():
    device = FakeDevice("slow-start", [12.8], fail_opens=3)
    sink = RecordingSink()
    sup = Supervisor([device], [sink])
    await run_until(sup, lambda: len(sink.readings) >= 1)
    assert device.opens >= 4


async def test_one_broken_sink_does_not_stop_the_others():
    good, bad = RecordingSink(), RecordingSink(explode=True)
    sup = Supervisor([FakeDevice("d", [12.8])], [bad, good])
    await run_until(sup, lambda: len(good.readings) >= 3)
    assert len(good.readings) >= 3


async def test_stopping_closes_every_sink():
    sink = RecordingSink()
    sup = Supervisor([FakeDevice("d", [12.8])], [sink])
    await run_until(sup, lambda: len(sink.readings) >= 1)
    assert sink.closed


async def test_failure_hook_reports_the_device_name():
    seen: list[str] = []
    device = FakeDevice("named", [12.8], fail_opens=2)
    device.set_failure_hook(seen.append)
    sup = Supervisor([device], [RecordingSink()])
    await run_until(sup, lambda: len(seen) >= 2)
    assert set(seen) == {"named"}


@pytest.mark.parametrize("count", [1, 3, 5])
async def test_scales_to_several_devices(count):
    devices = [FakeDevice(f"dev{i}", [float(i)]) for i in range(count)]
    sink = RecordingSink()
    sup = Supervisor(devices, [sink])
    await run_until(sup, lambda: len({r.source for r in sink.readings}) == count)
    assert len({r.source for r in sink.readings}) == count
