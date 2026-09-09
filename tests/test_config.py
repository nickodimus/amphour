from __future__ import annotations

import pytest

from amphour.config import ConfigError, from_dict

MINIMAL = {"device": {"address": "F0:F8:F2:65:00:0C"}}


def test_minimal_config_gets_sensible_defaults():
    config = from_dict(MINIMAL)
    assert config.poll.interval == 30.0
    assert config.prometheus.enabled is True
    assert config.influxdb.enabled is False
    assert config.prometheus.metric_prefix == "amphour_"


def test_device_address_is_required():
    with pytest.raises(ConfigError, match="address is required"):
        from_dict({})


def test_a_misspelled_option_is_an_error_not_a_silent_default():
    """A typo that is quietly ignored is how a setting appears to be applied
    without being applied."""
    with pytest.raises(ConfigError, match="intervall"):
        from_dict({**MINIMAL, "poll": {"intervall": 5}})


def test_influx_enabled_without_a_destination_is_rejected():
    with pytest.raises(ConfigError, match="url and database"):
        from_dict({**MINIMAL, "influxdb": {"enabled": True}})


def test_configuring_no_sinks_at_all_is_rejected():
    with pytest.raises(ConfigError, match="go nowhere"):
        from_dict({**MINIMAL, "prometheus": {"enabled": False}})


def test_zero_interval_is_rejected():
    with pytest.raises(ConfigError, match="greater than zero"):
        from_dict({**MINIMAL, "poll": {"interval": 0}})


def test_environment_overrides_file_credentials(monkeypatch):
    monkeypatch.setenv("AMPHOUR_INFLUX_USERNAME", "env_user")
    monkeypatch.setenv("AMPHOUR_INFLUX_PASSWORD", "env_pass")
    config = from_dict(
        {
            **MINIMAL,
            "influxdb": {
                "enabled": True,
                "url": "http://db:8086",
                "database": "x",
                "username": "file_user",
                "password": "file_pass",
            },
        }
    )
    assert config.influxdb.username == "env_user"
    assert config.influxdb.password == "env_pass"


def test_empty_credential_strings_become_none():
    config = from_dict(
        {**MINIMAL, "influxdb": {"url": "http://db:8086", "database": "x", "username": ""}}
    )
    assert config.influxdb.username is None


def test_tags_are_coerced_to_strings():
    config = from_dict(
        {
            **MINIMAL,
            "influxdb": {"url": "http://db:8086", "database": "x", "tags": {"n": 1}},
        }
    )
    assert config.influxdb.tags == {"n": "1"}
