"""InfluxDB sink wiring. No server is contacted; these are construction tests.

The sink's HTTP path has never been integration-tested against a real 1.x
server - see the handoff. What IS testable without one is that the session is
wired the way the docstring claims.
"""

from __future__ import annotations

from amphour.fields import Field
from amphour.sinks.influx import InfluxSink


class FakeDevice:
    name = "shunt"
    fields = (Field("battery_voltage", "volts", "confirmed", "test field"),)

    async def run(self, emit):  # pragma: no cover - never started here
        raise NotImplementedError


def _sink(url: str) -> InfluxSink:
    return InfluxSink([FakeDevice()], url=url, database="amphour")


def test_retries_are_configured_for_https_as_well_as_http():
    """Only http:// was mounted, so an https:// url silently got requests'
    default adapter and no retries."""
    sink = _sink("https://influx.example:8086")
    for scheme in ("http://", "https://"):
        adapter = sink._session.get_adapter(scheme)
        retries = adapter.max_retries
        assert retries.total == 2, f"{scheme} has no retry policy"
        assert 500 in retries.status_forcelist
        assert "POST" in retries.allowed_methods


def test_a_trailing_slash_on_the_url_does_not_double_up():
    assert _sink("http://influx.example:8086/").url == "http://influx.example:8086"
