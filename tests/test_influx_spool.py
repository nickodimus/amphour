"""The InfluxDB sink's disk spool. No server is contacted.

The POST is replaced by a script, so what is tested is the decision the sink
makes about each batch: delivered, spooled, replayed, or dropped - and why.
That decision is the whole feature. On 2026-11-04 the path to the server was
gone for 19.7 hours and every reading in it was taken, logged and thrown away.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
import requests

from amphour.config import ConfigError, from_dict
from amphour.fields import Field
from amphour.reading import Reading
from amphour.sinks.influx import InfluxRefused, InfluxSink
from amphour.sinks.spool import Spool, describe

T0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


class FakeDevice:
    name = "shunt"
    fields = (Field("battery_voltage", "volts", "confirmed", "test field"),)

    async def run(self, emit):  # pragma: no cover - never started here
        raise NotImplementedError


class ScriptedSink(InfluxSink):
    """An InfluxSink whose server is a function: payload -> exception or None."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.posts: list[bytes] = []
        self.server = lambda payload: None

    def _post(self, payload: bytes) -> None:
        exc = self.server(payload)
        if exc is not None:
            raise exc
        self.posts.append(payload)


def unreachable(payload: bytes) -> Exception:
    return requests.ConnectionError("no route to host")


def refused(payload: bytes) -> Exception:
    return InfluxRefused("influxdb returned 400: field type conflict")


def sink(tmp_path=None, **kwargs) -> ScriptedSink:
    # batch_size=1: every publish is a flush, so each reading is one decision.
    kwargs.setdefault("batch_size", 1)
    kwargs.setdefault("clock_ok", lambda: True)
    if tmp_path is not None:
        kwargs.setdefault("spool_dir", str(tmp_path / "spool"))
    return ScriptedSink(
        [FakeDevice()], url="http://influx.example:8086", database="amphour", **kwargs
    )


def reading(n: int) -> Reading:
    return Reading("shunt", {"battery_voltage": 50.0 + n}, taken_at=T0 + timedelta(seconds=30 * n))


def line(n: int) -> bytes:
    stamp = int((T0 + timedelta(seconds=30 * n)).timestamp())
    return f"amphour,device=shunt battery_voltage={50.0 + n!r} {stamp}".encode()


def spooled(tmp_path) -> list[bytes]:
    """Every spooled line, in replay order."""
    files = sorted((tmp_path / "spool").glob("*.lp"), key=lambda p: int(p.stem))
    return [ln for f in files for ln in f.read_bytes().splitlines()]


# --- off by default --------------------------------------------------------


async def test_without_a_spool_dir_a_failed_write_is_dropped_as_before(tmp_path, caplog):
    s = sink()
    s.server = unreachable
    with caplog.at_level(logging.WARNING):
        await s.publish(reading(0))
    assert "influxdb write of 1 points failed, dropped" in caplog.text
    s.server = lambda payload: None
    await s.publish(reading(1))
    assert s.posts == [line(1)], "the lost reading must not reappear from anywhere"
    assert list(tmp_path.iterdir()) == []


# --- spool and replay ------------------------------------------------------


async def test_an_unreachable_server_spools_the_batch_instead_of_dropping_it(tmp_path, caplog):
    s = sink(tmp_path)
    s.server = unreachable
    with caplog.at_level(logging.WARNING):
        await s.publish(reading(0))
        await s.publish(reading(1))
    assert spooled(tmp_path) == [line(0), line(1)]
    assert s.posts == []
    # The guard publisher counts lines containing these words; an outage must
    # stay visible on the wall even though the data survived it.
    assert caplog.text.count("influxdb write of 1 points failed, spooled") == 2


async def test_the_spool_is_replayed_once_the_server_answers(tmp_path, caplog):
    s = sink(tmp_path)
    s.server = unreachable
    await s.publish(reading(0))
    await s.publish(reading(1))
    s.server = lambda payload: None
    with caplog.at_level(logging.INFO):
        await s.publish(reading(2))
    # Live first, then the backlog: the reading being taken now is never
    # queued behind an outage.
    assert s.posts == [line(2), line(0) + b"\n" + line(1) + b"\n"]
    assert spooled(tmp_path) == []
    assert "replayed 2 points (2026-11-21T12:00:00Z to 2026-11-21T12:00:30Z)" in caplog.text
    assert "influxdb spool drained: replayed 2 points" in caplog.text


async def test_replay_is_oldest_first_and_one_segment_per_live_flush(tmp_path):
    s = sink(tmp_path)
    s._spool.segment_bytes = 1  # every batch its own segment
    s.server = unreachable
    for n in range(3):
        await s.publish(reading(n))
    assert len(list((tmp_path / "spool").glob("*.lp"))) == 3

    s.server = lambda payload: None
    await s.publish(reading(10))
    assert s.posts == [line(10), line(0) + b"\n"]
    await s.publish(reading(11))
    assert s.posts[2:] == [line(11), line(1) + b"\n"]
    assert spooled(tmp_path) == [line(2)]


async def test_a_replay_that_fails_keeps_the_segment_for_next_time(tmp_path):
    s = sink(tmp_path)
    s.server = unreachable
    await s.publish(reading(0))

    # The live write gets through and the replay does not: the link dropped
    # between the two.
    s.server = lambda payload: None if payload == line(1) else unreachable(payload)
    await s.publish(reading(1))
    assert s.posts == [line(1)]
    assert spooled(tmp_path) == [line(0)]

    s.server = lambda payload: None
    await s.publish(reading(2))
    assert s.posts == [line(1), line(2), line(0) + b"\n"]
    assert spooled(tmp_path) == []


async def test_a_batch_spooled_during_a_replay_is_not_lost_with_the_replayed_segment(tmp_path):
    s = sink(tmp_path)
    s.server = unreachable
    await s.publish(reading(0))
    s.server = lambda payload: None
    await s.publish(reading(1))  # replays and deletes the segment holding 0
    s.server = unreachable
    await s.publish(reading(2))  # must start a new segment, not write into a deleted one
    assert spooled(tmp_path) == [line(2)]


async def test_a_spool_left_by_an_earlier_run_is_replayed_by_the_next(tmp_path, caplog):
    first = sink(tmp_path)
    first.server = unreachable
    await first.publish(reading(0))

    with caplog.at_level(logging.INFO):
        second = sink(tmp_path)
    assert "holds 1 segment(s)" in caplog.text
    await second.publish(reading(1))
    assert second.posts == [line(1), line(0) + b"\n"]
    assert spooled(tmp_path) == []


async def test_a_new_run_never_appends_to_or_overwrites_an_old_segment(tmp_path):
    first = sink(tmp_path)
    first.server = unreachable
    await first.publish(reading(0))
    second = sink(tmp_path)
    second.server = unreachable
    await second.publish(reading(1))
    names = sorted(p.name for p in (tmp_path / "spool").glob("*.lp"))
    assert names == ["0000000000.lp", "0000000001.lp"]
    assert spooled(tmp_path) == [line(0), line(1)]


# --- what is deliberately not spooled --------------------------------------


async def test_a_batch_the_server_refused_is_dropped_not_spooled(tmp_path, caplog):
    """A refused point would be refused on every replay, in front of the queue."""
    s = sink(tmp_path)
    s.server = refused
    with caplog.at_level(logging.WARNING):
        await s.publish(reading(0))
    assert spooled(tmp_path) == []
    assert "influxdb write of 1 points refused by the server, dropped" in caplog.text


async def test_a_spooled_segment_the_server_refuses_is_discarded_not_retried(tmp_path, caplog):
    s = sink(tmp_path)
    s._spool.segment_bytes = 1
    s.server = unreachable
    await s.publish(reading(0))
    await s.publish(reading(1))

    s.server = lambda payload: refused(payload) if payload == line(0) + b"\n" else None
    with caplog.at_level(logging.WARNING):
        await s.publish(reading(2))
    assert "0000000000.lp (1 points" in caplog.text and "discarded" in caplog.text
    # The bad segment is gone and the one behind it is not wedged.
    await s.publish(reading(3))
    assert s.posts == [line(2), line(3), line(1) + b"\n"]
    assert spooled(tmp_path) == []


async def test_nothing_is_spooled_while_the_clock_has_not_been_set(tmp_path, caplog):
    """No RTC, rebooted offline: these timestamps are wrong, and a replay would
    write them over real points."""
    s = sink(tmp_path, clock_ok=lambda: False)
    s.server = unreachable
    with caplog.at_level(logging.WARNING):
        await s.publish(reading(0))
    assert spooled(tmp_path) == []
    assert "clock not set since boot" in caplog.text
    assert "influxdb write of 1 points failed, dropped" in caplog.text


async def test_a_spool_that_cannot_be_written_drops_and_does_not_raise(tmp_path, caplog):
    s = sink(tmp_path)

    def full(payload: bytes) -> int:
        raise OSError(28, "No space left on device")

    s._spool.append = full
    s.server = unreachable
    with caplog.at_level(logging.WARNING):
        await s.publish(reading(0))  # must not raise: the poll loop depends on it
    assert "spool write failed" in caplog.text


# --- which answers count as refused ----------------------------------------


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "body"


class _Session:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def post(self, *args, **kwargs) -> _Response:
        return _Response(self.status_code)


@pytest.mark.parametrize("status", [400, 413])
def test_bad_data_answers_are_refusals(status):
    s = InfluxSink([FakeDevice()], url="http://influx.example:8086", database="amphour")
    s._session = _Session(status)
    with pytest.raises(InfluxRefused):
        s._post(b"x")


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503, 200])
def test_every_other_answer_keeps_the_data(status):
    """Auth, a missing database, a proxy's 200: all fixable without the data
    being wrong, so none of them may discard it."""
    s = InfluxSink([FakeDevice()], url="http://influx.example:8086", database="amphour")
    s._session = _Session(status)
    with pytest.raises(requests.RequestException) as caught:
        s._post(b"x")
    assert not isinstance(caught.value, InfluxRefused)


def test_204_is_the_only_success():
    s = InfluxSink([FakeDevice()], url="http://influx.example:8086", database="amphour")
    s._session = _Session(204)
    s._post(b"x")


# --- the spool itself ------------------------------------------------------


def test_the_cap_deletes_the_oldest_segments_and_keeps_the_newest(tmp_path):
    spool = Spool(tmp_path, max_bytes=25, segment_bytes=1)
    assert spool.append(b"a" * 9) == 0  # 10 bytes with the newline
    assert spool.append(b"b" * 9) == 0  # 20
    assert spool.append(b"c" * 9) == 1  # 30 > 25: the oldest goes
    kept = [p.read_bytes() for p in sorted(tmp_path.glob("*.lp"))]
    assert kept == [b"b" * 9 + b"\n", b"c" * 9 + b"\n"]
    assert spool.pending() == (2, 20)


def test_one_batch_larger_than_the_cap_is_still_kept(tmp_path):
    spool = Spool(tmp_path, max_bytes=5, segment_bytes=1)
    spool.append(b"x" * 50)
    assert spool.pending() == (1, 51)


def test_batches_share_a_segment_until_it_is_full_and_stay_whole_lines(tmp_path):
    spool = Spool(tmp_path, max_bytes=10_000, segment_bytes=30)
    spool.append(b"one 1\ntwo 2")
    spool.append(b"three 3")
    spool.append(b"x" * 40)
    spool.append(b"four 4")
    files = sorted(tmp_path.glob("*.lp"))
    assert [f.read_bytes() for f in files] == [
        b"one 1\ntwo 2\nthree 3\n" + b"x" * 40 + b"\n",
        b"four 4\n",
    ]


def test_files_that_are_not_segments_are_left_alone(tmp_path):
    (tmp_path / "notes.txt").write_text("mine")
    (tmp_path / "abc.lp").write_text("not numbered")
    spool = Spool(tmp_path, max_bytes=1, segment_bytes=1)
    spool.append(b"a")
    spool.append(b"b")
    assert spool.oldest().name == "0000000001.lp"
    assert (tmp_path / "notes.txt").exists() and (tmp_path / "abc.lp").exists()


def test_an_unwritable_spool_directory_fails_at_construction(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("in the way")
    with pytest.raises(OSError):
        Spool(blocker / "spool", max_bytes=1)


def test_describe_counts_points_and_reads_the_time_span():
    assert describe(b"m v=1 100\nm v=2 160\n") == (2, 100, 160)
    assert describe(b"") == (0, None, None)
    assert describe(b"m v=1\n") == (1, None, None)  # a line with no timestamp


# --- configuration ---------------------------------------------------------

DEVICE = {"device": [{"name": "a", "driver": "renogy_bt1", "address": "AA:BB:CC:DD:EE:FF"}]}


def test_the_spool_is_off_unless_configured():
    config = from_dict(DEVICE)
    assert config.influxdb.spool_dir is None
    assert config.influxdb.spool_max_mb == 64


def test_an_empty_spool_dir_means_off_not_the_current_directory():
    config = from_dict({**DEVICE, "influxdb": {"spool_dir": ""}})
    assert config.influxdb.spool_dir is None


def test_spool_options_are_read():
    config = from_dict({**DEVICE, "influxdb": {"spool_dir": "/var/lib/x", "spool_max_mb": 8}})
    assert (config.influxdb.spool_dir, config.influxdb.spool_max_mb) == ("/var/lib/x", 8)


@pytest.mark.parametrize("bad", [0, -1, 1.5, "64", True])
def test_a_nonsense_spool_cap_is_a_configuration_error(bad):
    with pytest.raises(ConfigError, match="spool_max_mb"):
        from_dict({**DEVICE, "influxdb": {"spool_max_mb": bad}})


def test_an_unusable_spool_dir_stops_startup_with_a_configuration_error(tmp_path):
    """Found when amphour starts, not during the first outage."""
    from amphour.cli import _build_sinks

    blocker = tmp_path / "file"
    blocker.write_text("in the way")
    config = from_dict(
        {
            **DEVICE,
            "prometheus": {"enabled": False},
            "influxdb": {
                "enabled": True,
                "url": "http://influx.example:8086",
                "database": "amphour",
                "spool_dir": str(blocker / "spool"),
            },
        }
    )
    with pytest.raises(ConfigError, match="spool_dir"):
        _build_sinks(config, [FakeDevice()])


def test_a_configured_spool_dir_reaches_the_sink(tmp_path):
    from amphour.cli import _build_sinks

    config = from_dict(
        {
            **DEVICE,
            "prometheus": {"enabled": False},
            "influxdb": {
                "enabled": True,
                "url": "http://influx.example:8086",
                "database": "amphour",
                "spool_dir": str(tmp_path / "spool"),
                "spool_max_mb": 3,
            },
        }
    )
    sinks, _ = _build_sinks(config, [FakeDevice()])
    assert (tmp_path / "spool").is_dir()
    assert sinks[0]._spool.max_bytes == 3 * 1024 * 1024
