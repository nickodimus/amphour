"""The VE.Direct driver against a REAL file descriptor, not a fake queue.

Everything else in the suite stops at the framer: bytes in, blocks out. This
file exercises the layer underneath it — `os.open`, the termios configuration,
`loop.add_reader`, `os.read`, the bounded queue — by giving the driver a
pseudo-terminal and replaying the unedited 59 KB capture down it.

A pty is not a wire. It has no baud rate, no electrical noise, and no USB
adapter to wedge. What it does have is a real fd with real termios semantics,
which is the part of this driver that had never executed at all: as of
2026-09-11 no driver in this program had completed a read from anything but a
fixture. This narrows that gap; it does not close it.

The invariant to assert is `blocks_ok`, which is a property of the CAPTURE (88
verifiable blocks) and is reproduced exactly at every chunk size. Reading count
is not an invariant — see test_emit_interval_governs_how_often_a_reading_lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import termios
from pathlib import Path

import pytest

from amphour.device import DeviceError
from amphour.drivers.victron_vedirect import VEDirectDevice

FIXTURES = Path(__file__).parent / "fixtures"
RAW = FIXTURES / "vedirect-raw-stream-2026-09-09.bin"

# The capture contains exactly this many checksum-valid blocks; the rest of the
# file is the genuinely corrupt region documented in test_vedirect.py.
BLOCKS_IN_CAPTURE = 88


@pytest.fixture
def serial_pair():
    """A pty whose slave path a driver can open by name.

    The slave fd is closed immediately: the driver opens the path itself, which
    is what a real run does. The pts survives because the master stays open.
    """
    master, slave = pty.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    try:
        yield master, path
    finally:
        with contextlib.suppress(OSError):
            os.close(master)


async def _replay(device, master, data, chunk=256, settle=0.25):
    """Push `data` at the device and collect everything it emits."""
    readings = []
    finished = asyncio.Event()

    async def writer():
        for i in range(0, len(data), chunk):
            # to_thread: a pty buffer fills, and a blocking write on the event
            # loop would deadlock against the reader draining it.
            await asyncio.to_thread(os.write, master, data[i : i + chunk])
            await asyncio.sleep(0)
        await asyncio.sleep(settle)
        finished.set()

    async def consumer():
        try:
            async for reading in device.stream():
                readings.append(reading)
        except (DeviceError, asyncio.CancelledError):
            pass

    tasks = [asyncio.create_task(writer()), asyncio.create_task(consumer())]
    try:
        async with asyncio.timeout(30):
            await finished.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return readings


async def test_the_driver_reads_a_real_fd_and_recovers_every_block(serial_pair):
    master, path = serial_pair
    device = VEDirectDevice("shunt", path, emit_interval=0.0, stale_after=5.0)
    await device.open()
    try:
        readings = await _replay(device, master, RAW.read_bytes())
    finally:
        await device.close()

    assert device._reader.blocks_ok == BLOCKS_IN_CAPTURE
    assert device._reader.pending < 2048, "buffer must stay bounded"
    assert readings, "a real fd produced no readings at all"


@pytest.mark.parametrize("chunk", [64, 256, 1024, 4096])
async def test_block_recovery_does_not_depend_on_read_size(serial_pair, chunk):
    """A serial port delivers whatever happens to have arrived. Framing must
    not care where the boundaries fall."""
    master, path = serial_pair
    device = VEDirectDevice("shunt", path, emit_interval=0.0, stale_after=5.0)
    await device.open()
    try:
        await _replay(device, master, RAW.read_bytes(), chunk=chunk)
    finally:
        await device.close()
    assert device._reader.blocks_ok == BLOCKS_IN_CAPTURE


async def test_values_survive_the_whole_path_from_fd_to_reading(serial_pair):
    """Decoded values must match what the capture actually holds — this is the
    only test that carries a value from a file descriptor all the way out."""
    master, path = serial_pair
    device = VEDirectDevice("shunt", path, emit_interval=0.0, stale_after=5.0)
    await device.open()
    try:
        readings = await _replay(device, master, RAW.read_bytes())
    finally:
        await device.close()

    voltages = {r.values["battery_voltage"] for r in readings if "battery_voltage" in r.values}
    assert voltages, "no battery_voltage came through"
    # The bank was discharging around 11.1 V throughout this capture.
    assert all(10.5 < v < 12.0 for v in voltages), sorted(voltages)
    assert any(r.text.get("alarm") == "OFF" for r in readings)
    assert any("battery_state_of_charge" in r.values for r in readings)


async def test_emit_interval_governs_how_often_a_reading_lands(serial_pair):
    """Readings are throttled by wall clock, not by block count. With the
    throttle off, every feed() that carries a block can emit — which is why
    reading count is not an invariant of the capture and blocks_ok is."""
    master, path = serial_pair
    device = VEDirectDevice("shunt", path, emit_interval=3600.0, stale_after=5.0)
    await device.open()
    try:
        readings = await _replay(device, master, RAW.read_bytes())
    finally:
        await device.close()
    assert device._reader.blocks_ok == BLOCKS_IN_CAPTURE
    assert len(readings) == 1, "an hour-long interval should let exactly one through"


async def test_termios_is_actually_applied_to_the_fd(serial_pair):
    """The driver sets fully raw mode so IXON cannot eat 0x11/0x13 out of the
    data and ICRNL cannot rewrite the \\r\\n the framing depends on. If
    tcsetattr silently did nothing, every other test here would still pass on a
    pty — so this checks the fd, not the behaviour."""
    _, path = serial_pair
    device = VEDirectDevice("shunt", path)
    await device.open()
    try:
        iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(device._fd)
        assert iflag == 0, "input processing is on; IXON/ICRNL would corrupt the stream"
        assert oflag == 0
        assert lflag == 0, "canonical mode or echo is on"
        assert cflag & termios.CS8
        assert cc[termios.VMIN] == 0 and cc[termios.VTIME] == 0
    finally:
        await device.close()


async def test_an_unsupported_baud_is_refused_before_the_port_is_touched(serial_pair):
    _, path = serial_pair
    device = VEDirectDevice("shunt", path, baud=1234)
    with pytest.raises(DeviceError) as caught:
        await device.open()
    assert "1234" in str(caught.value)
    assert device._fd is None, "an fd was opened despite the baud being rejected"


async def test_a_missing_port_is_a_retryable_DeviceError():
    device = VEDirectDevice("shunt", "/dev/definitely-not-a-real-port")
    with pytest.raises(DeviceError):
        await device.open()
    assert device._fd is None


async def test_close_releases_the_descriptor(serial_pair):
    """A device that reconnects forever must not leak an fd per attempt."""
    master, path = serial_pair
    device = VEDirectDevice("shunt", path)
    for _ in range(20):
        await device.open()
        assert device._fd is not None
        await device.close()
        assert device._fd is None
    # If each cycle leaked, opening a 21st time would still work but the
    # process would be carrying 20 dead fds. Count them instead.
    open_fds = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    await device.open()
    await device.close()
    assert len(os.listdir(f"/proc/{os.getpid()}/fd")) <= open_fds
    assert master  # fixture still owns the master end


async def test_closing_twice_is_harmless(serial_pair):
    """run()'s finally can be reached after close() already ran."""
    _, path = serial_pair
    device = VEDirectDevice("shunt", path)
    await device.open()
    await device.close()
    await device.close()
    assert device._fd is None
