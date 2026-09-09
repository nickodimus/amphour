from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# The predecessor's field names, mapped onto ours. The captured fixtures carry
# the values the OLD app independently reported for the same instant, which is
# what makes them a real cross-check rather than a restatement of our own
# assumptions.
OLD_TO_NEW = {
    "battery_percentage": "battery_state_of_charge",
    "battery_voltage": "battery_voltage",
    "battery_current": "battery_charging_current",
    "controller_temperature": "controller_temperature",
    "battery_temperature": "battery_temperature",
    "load_voltage": "load_voltage",
    "load_current": "load_current",
    "load_power": "load_power",
    "pv_voltage": "pv_voltage",
    "pv_current": "pv_current",
    "pv_power": "pv_power",
    "max_charging_power_today": "charging_power_max_today",
    "max_discharging_power_today": "discharging_power_max_today",
    "charging_amp_hours_today": "charging_amp_hours_today",
    "discharging_amp_hours_today": "discharging_amp_hours_today",
    "power_generation_today": "power_generation_today",
    "power_generation_total": "power_generation_total",
    "charging_status": "charging_state",
}


def _load(name: str) -> list[dict]:
    path = FIXTURE_DIR / name
    if not path.exists():
        return []
    return json.loads(path.read_text())


@pytest.fixture(scope="session")
def night_capture() -> list[dict]:
    """11 real frames captured from a live BT-TH-F265000C, 2026-09-09 00:06-00:13.

    Array dark, controller load terminals unused, so every PV, load and
    discharge field is legitimately zero.
    """
    frames = _load("night-2026-09-09.json")
    assert frames, "night fixture is missing - the test suite needs it"
    return frames


@pytest.fixture(scope="session")
def day_capture() -> list[dict]:
    """Daylight frames, with the array producing. Empty until captured.

    Tests using this skip rather than fail while it is absent, so the suite is
    honest about which half of the matrix it covers.
    """
    return _load("day-2026-09-09.json")


@pytest.fixture(scope="session")
def all_frames(night_capture: list[dict], day_capture: list[dict]) -> list[dict]:
    return [*night_capture, *day_capture]
