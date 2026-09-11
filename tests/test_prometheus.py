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
