"""Prometheus sink. No HTTP server is started; these read the registry directly.

The sink had no tests at all before 2026-09-11.
"""

from __future__ import annotations

import asyncio

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from amphour.fields import Field
from amphour.reading import Reading
from amphour.sinks.prometheus import PrometheusSink


class FakeDevice:
    def __init__(self, name, fields):
        self.name = name
        self.fields = fields

    async def run(self, emit):  # pragma: no cover - never started here
        raise NotImplementedError


VOLTS_RENOGY = Field("battery_voltage", "volts", "confirmed", "Battery terminal voltage")
VOLTS_SHUNT = Field("battery_voltage", "volts", "confirmed", "Main battery voltage")
SOC_SHUNT = Field("battery_state_of_charge", "percent", "confirmed", "Coulomb-counted charge")
SOC_RENOGY = Field("battery_state_of_charge", "percent", "probable", "Charge inferred from voltage")


def _sink(*devices):
    return PrometheusSink(list(devices), registry=CollectorRegistry(), start_server=False)


def _expose(sink) -> str:
    return generate_latest(sink.registry).decode()


def test_readings_and_failures_are_counters_not_gauges():
    """They only ever go up, and _total is the Counter convention. Declaring a
    monotonic value as a gauge tells tooling it may fall."""
    sink = _sink(FakeDevice("renogy", (VOLTS_RENOGY,)))
    text = _expose(sink)
    assert "# TYPE amphour_readings_total counter" in text
    assert "# TYPE amphour_failures_total counter" in text
    assert "amphour_readings_total_total" not in text, "name got doubled"


def test_a_device_that_has_never_succeeded_reads_zero_rather_than_being_absent():
    """Absent does not alert. Zero does."""
    sink = _sink(FakeDevice("renogy", (VOLTS_RENOGY,)))
    text = _expose(sink)
    assert 'amphour_readings_total{device="renogy"} 0.0' in text
    assert 'amphour_failures_total{device="renogy"} 0.0' in text


def test_counters_advance():
    sink = _sink(FakeDevice("renogy", (VOLTS_RENOGY,)))
    asyncio.run(sink.publish(Reading(source="renogy", values={"battery_voltage": 13.4})))
    sink.record_failure("renogy")
    text = _expose(sink)
    assert 'amphour_readings_total{device="renogy"} 1.0' in text
    assert 'amphour_failures_total{device="renogy"} 1.0' in text


def test_two_devices_share_one_metric_with_a_device_label():
    """The whole point of the shared vocabulary: one query compares them."""
    sink = _sink(FakeDevice("renogy", (VOLTS_RENOGY,)), FakeDevice("shunt", (VOLTS_SHUNT,)))
    asyncio.run(sink.publish(Reading(source="renogy", values={"battery_voltage": 13.4})))
    asyncio.run(sink.publish(Reading(source="shunt", values={"battery_voltage": 13.1})))
    text = _expose(sink)
    assert 'amphour_battery_voltage{device="renogy"} 13.4' in text
    assert 'amphour_battery_voltage{device="shunt"} 13.1' in text


def test_help_names_every_device_when_they_disagree():
    """Regression: the first device to declare a field supplied HELP for all of
    them, so a second device's differing confidence never showed."""
    sink = _sink(FakeDevice("shunt", (SOC_SHUNT,)), FakeDevice("renogy", (SOC_RENOGY,)))
    help_line = next(
        line
        for line in _expose(sink).splitlines()
        if line.startswith("# HELP amphour_battery_state_of_charge ")
    )
    assert "shunt=confirmed" in help_line
    assert "renogy=probable" in help_line
    assert "Coulomb-counted" in help_line
    assert "inferred from voltage" in help_line


def test_help_stays_short_when_the_declarations_agree():
    sink = _sink(FakeDevice("renogy", (VOLTS_RENOGY,)))
    help_line = next(
        line
        for line in _expose(sink).splitlines()
        if line.startswith("# HELP amphour_battery_voltage ")
    )
    assert help_line.endswith("Battery terminal voltage [volts, confidence: confirmed]")


def test_conflicting_units_for_one_field_are_reported(caplog):
    """One metric cannot hold two units. That is a bug in the field tables."""
    celsius = Field("battery_temperature", "celsius", "confirmed", "Battery temperature")
    fahrenheit = Field("battery_temperature", "fahrenheit", "confirmed", "Battery temperature")
    with caplog.at_level("WARNING"):
        _sink(FakeDevice("a", (celsius,)), FakeDevice("b", (fahrenheit,)))
    assert any("conflicting units" in r.getMessage() for r in caplog.records)


def test_an_unverified_field_is_not_exported_by_default():
    guess = Field("register_0x010a", "raw", "unverified", "Unidentified")
    sink = _sink(FakeDevice("renogy", (VOLTS_RENOGY, guess)))
    assert "amphour_register_0x010a" not in _expose(sink)


def test_unverified_fields_appear_when_asked_for():
    guess = Field("register_0x010a", "raw", "unverified", "Unidentified")
    sink = PrometheusSink(
        [FakeDevice("renogy", (VOLTS_RENOGY, guess))],
        registry=CollectorRegistry(),
        start_server=False,
        include_unverified=True,
    )
    assert "amphour_register_0x010a" in _expose(sink)


@pytest.mark.parametrize("bad", ["battery_voltage", "nonsense"])
def test_a_value_a_device_did_not_declare_is_not_published(bad):
    """A driver bug must not invent a series under another device's name."""
    sink = _sink(FakeDevice("shunt", (SOC_SHUNT,)))
    asyncio.run(sink.publish(Reading(source="shunt", values={bad: 1.0})))
    assert f'amphour_{bad}{{device="shunt"}}' not in _expose(sink)


# --- devices that emit several readings -------------------------------------
#
# The EG4 polls two packs on one RS485 line from ONE device block, emitting a
# Reading per pack tagged `<name>-<label>`. That is the shape these cover.


class FakeMultiSourceDevice(FakeDevice):
    def __init__(self, name, fields, labels):
        super().__init__(name, fields)
        self.sources = tuple(f"{name}-{label}" for label in labels)


UNVERIFIED = Field("register_0x0121", "bitfield", "unverified", "A guess")


async def test_readings_and_failures_share_a_label_for_a_multi_pack_device():
    """The numerator and denominator of a failure rate must be comparable.

    readings_total used to be labelled by reading SOURCE and failures_total by
    DEVICE, so `readings_total{device="eg4"}` sat at zero forever while
    `failures_total{device="eg4"}` climbed. Any rate built from the pair was
    nonsense, and it was nonsense silently.
    """
    device = FakeMultiSourceDevice("eg4", (VOLTS_SHUNT,), ("master", "slave"))
    sink = _sink(device)

    await sink.publish(Reading(source="eg4-master", values={"battery_voltage": 53.1}))
    await sink.publish(Reading(source="eg4-slave", values={"battery_voltage": 53.2}))
    sink.record_failure("eg4")

    text = _expose(sink)
    assert 'amphour_readings_total{device="eg4"} 2.0' in text
    assert 'amphour_failures_total{device="eg4"} 1.0' in text
    # and NOT split across the per-pack sources, which is what broke the rate
    assert 'readings_total{device="eg4-master"}' not in text
    assert 'readings_total{device="eg4-slave"}' not in text


async def test_per_source_liveness_is_still_visible():
    # Aggregating the counter must not cost the ability to see ONE pack go
    # quiet. That distinction lives in the read clock, which stays per source.
    device = FakeMultiSourceDevice("eg4", (VOLTS_SHUNT,), ("master", "slave"))
    sink = _sink(device)

    await sink.publish(Reading(source="eg4-master", values={"battery_voltage": 53.1}))

    text = _expose(sink)
    assert 'amphour_last_reading_timestamp_seconds{device="eg4-master"}' in text
    assert 'amphour_battery_voltage{device="eg4-master"} 53.1' in text


async def test_unverified_fields_are_filtered_for_multi_source_devices_too():
    """The allow-list lookup missed entirely for multi-source devices.

    `_allowed` was keyed by device name and looked up by reading source, so for
    the EG4 it returned None - and None means "no filtering". Unverified fields
    were exported regardless of include_unverified.
    """
    device = FakeMultiSourceDevice("eg4", (VOLTS_SHUNT, UNVERIFIED), ("master", "slave"))
    sink = _sink(device)

    await sink.publish(
        Reading(source="eg4-master", values={"battery_voltage": 53.1, "register_0x0121": 4.0})
    )

    text = _expose(sink)
    assert 'amphour_battery_voltage{device="eg4-master"} 53.1' in text
    assert "register_0x0121" not in text


async def test_single_source_devices_are_unchanged():
    # The common case must not move: source and device name are the same string.
    sink = _sink(FakeDevice("renogy", (VOLTS_RENOGY,)))
    await sink.publish(Reading(source="renogy", values={"battery_voltage": 53.4}))
    sink.record_failure("renogy")

    text = _expose(sink)
    assert 'amphour_readings_total{device="renogy"} 1.0' in text
    assert 'amphour_failures_total{device="renogy"} 1.0' in text
