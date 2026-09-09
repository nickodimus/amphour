from __future__ import annotations

import pytest

from amphour.config import ConfigError, from_dict

MINIMAL = {"device": [{"name": "shunt", "driver": "victron_vedirect", "port": "/dev/ttyUSB0"}]}


def test_minimal_config_gets_sensible_defaults():
    config = from_dict(MINIMAL)
    assert len(config.devices) == 1
    assert config.devices[0].name == "shunt"
    assert config.devices[0].options == {"port": "/dev/ttyUSB0"}
    assert config.prometheus.enabled is True
    assert config.influxdb.enabled is False


def test_at_least_one_device_is_required():
    with pytest.raises(ConfigError, match="at least one"):
        from_dict({})


def test_a_single_device_table_is_a_clear_error_not_a_crash():
    """[device] and [[device]] are one bracket apart and TOML will not warn."""
    with pytest.raises(ConfigError, match=r"\[\[device\]\], not \[device\]"):
        from_dict({"device": {"name": "x", "driver": "y"}})


def test_a_device_needs_a_name_and_a_driver():
    with pytest.raises(ConfigError, match="needs a name"):
        from_dict({"device": [{"driver": "victron_vedirect"}]})
    with pytest.raises(ConfigError, match="needs a driver"):
        from_dict({"device": [{"name": "shunt"}]})


def test_duplicate_device_names_are_rejected():
    """Names become metric labels; two devices sharing one would merge series."""
    with pytest.raises(ConfigError, match="must be unique"):
        from_dict(
            {
                "device": [
                    {"name": "same", "driver": "a"},
                    {"name": "same", "driver": "b"},
                ]
            }
        )


def test_several_devices_keep_their_order_and_options():
    config = from_dict(
        {
            "device": [
                {"name": "shunt", "driver": "victron_vedirect", "port": "/dev/ttyUSB0"},
                {"name": "mppt", "driver": "victron_vedirect", "port": "/dev/ttyUSB1"},
                {"name": "renogy", "driver": "renogy_bt1", "address": "AA:BB:CC:DD:EE:FF"},
            ]
        }
    )
    assert [d.name for d in config.devices] == ["shunt", "mppt", "renogy"]
    assert config.devices[2].options == {"address": "AA:BB:CC:DD:EE:FF"}


def test_a_misspelled_sink_option_is_an_error_not_a_silent_default():
    with pytest.raises(ConfigError, match="prt"):
        from_dict({**MINIMAL, "prometheus": {"prt": 5000}})


def test_influx_enabled_without_a_destination_is_rejected():
    with pytest.raises(ConfigError, match="url and database"):
        from_dict({**MINIMAL, "influxdb": {"enabled": True}})


def test_configuring_no_sinks_at_all_is_rejected():
    with pytest.raises(ConfigError, match="go nowhere"):
        from_dict({**MINIMAL, "prometheus": {"enabled": False}})


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
        {**MINIMAL, "influxdb": {"url": "http://db:8086", "database": "x", "tags": {"n": 1}}}
    )
    assert config.influxdb.tags == {"n": "1"}
