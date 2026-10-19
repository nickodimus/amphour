"""The shared BLE scanner hub.

No real radio: a fake BleakScanner captures the detection callback the hub
registers, and each test drives advertisements through it by hand. This
exercises the fan-out and the freshness rule that let one discovery serve an
advertisement listener (victron/mopeka) and a scan-then-connect driver
(renogy) at once -- the whole reason the hub exists.
"""

from __future__ import annotations

import time

import pytest

from amphour.ble import ScannerHub


class FakeScanner:
    last: FakeScanner | None = None

    def __init__(self, detection_callback=None, adapter=None, **kw):
        self.detection_callback = detection_callback
        self.adapter = adapter
        self.started = False
        self.stopped = False
        FakeScanner.last = self

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeDev:
    def __init__(self, address, name=None):
        self.address = address
        self.name = name


class FakeAdv:
    def __init__(self, local_name=None, manufacturer_data=None):
        self.local_name = local_name
        self.manufacturer_data = manufacturer_data or {}


@pytest.fixture
def fake_scanner(monkeypatch):
    monkeypatch.setattr("amphour.ble.BleakScanner", FakeScanner)
    return FakeScanner


async def test_subscribers_receive_detections_until_they_unsubscribe(fake_scanner):
    hub = ScannerHub("hci0")
    seen: list[tuple[str, str | None]] = []
    handle = hub.subscribe(lambda d, a: seen.append((d.address, a.local_name)))
    await hub.ensure_started()
    assert fake_scanner.last.started and fake_scanner.last.adapter == "hci0"

    fake_scanner.last.detection_callback(FakeDev("AA:BB:CC:DD:EE:FF"), FakeAdv(local_name="x"))
    assert seen == [("AA:BB:CC:DD:EE:FF", "x")]

    hub.unsubscribe(handle)
    seen.clear()
    fake_scanner.last.detection_callback(FakeDev("AA:BB:CC:DD:EE:FF"), FakeAdv())
    assert seen == []

    await hub.aclose()
    assert fake_scanner.last.stopped


async def test_one_raising_subscriber_does_not_stop_the_others(fake_scanner):
    hub = ScannerHub()
    good: list[str] = []

    def boom(d, a):
        raise RuntimeError("subscriber blew up")

    hub.subscribe(boom)
    hub.subscribe(lambda d, a: good.append(d.address))
    await hub.ensure_started()
    fake_scanner.last.detection_callback(FakeDev("AA:BB:CC:DD:EE:FF"), FakeAdv())
    assert good == ["AA:BB:CC:DD:EE:FF"]
    await hub.aclose()


async def test_get_device_returns_the_fresh_device_by_address_and_name(fake_scanner):
    hub = ScannerHub()
    await hub.ensure_started()
    cb = fake_scanner.last.detection_callback

    dev = FakeDev("AA:BB:CC:DD:EE:FF")
    cb(dev, FakeAdv(local_name="BT-TH-F265000C"))

    # case-insensitive address lookup
    assert await hub.get_device("aa:bb:cc:dd:ee:ff", max_age_s=5, timeout=1) is dev
    # the alias path the renogy driver uses
    assert await hub.get_device(name="BT-TH-F265000C", max_age_s=5, timeout=1) is dev
    await hub.aclose()


async def test_get_device_times_out_on_an_unseen_address(fake_scanner):
    hub = ScannerHub()
    await hub.ensure_started()
    got = await hub.get_device("11:22:33:44:55:66", max_age_s=5, timeout=0.3)
    assert got is None
    await hub.aclose()


async def test_get_device_treats_a_stale_sighting_as_absent(fake_scanner):
    hub = ScannerHub()
    await hub.ensure_started()
    # Seen, but longer ago than max_age_s: must not be handed to a connector,
    # which would try to connect to an address BlueZ no longer freshly sees.
    hub._by_addr["99:99:99:99:99:99"] = (FakeDev("99:99:99:99:99:99"), time.monotonic() - 100)
    got = await hub.get_device("99:99:99:99:99:99", max_age_s=5, timeout=0.3)
    assert got is None
    await hub.aclose()


async def test_ensure_started_is_idempotent(fake_scanner):
    hub = ScannerHub()
    await hub.ensure_started()
    first = fake_scanner.last
    await hub.ensure_started()
    assert fake_scanner.last is first  # no second scanner constructed
    await hub.aclose()
