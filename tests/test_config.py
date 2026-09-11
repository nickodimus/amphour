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


# --- driver registry --------------------------------------------------------


def test_every_registered_driver_reports_its_fields():
    """cli.py --list-fields used to keep its own driver -> module map. A driver
    added without touching that map would have vanished from --list-fields with
    no error, so the registry now carries the field table itself."""
    from amphour import drivers

    available = drivers.available()
    assert available, "no drivers registered"
    for name in available:
        fields = drivers.fields_for(name)
        assert fields, f"{name} registered no fields"
        assert len({f.name for f in fields}) == len(fields), f"{name} has duplicate field names"


def test_fields_for_an_unknown_driver_names_the_alternatives():
    from amphour import drivers

    with pytest.raises(KeyError) as caught:
        drivers.fields_for("nonesuch")
    message = caught.value.args[0]
    assert "nonesuch" in message
    for name in drivers.available():
        assert name in message


def test_the_example_config_actually_parses():
    """config.example.toml is documentation that can rot silently. Parsing it
    here means an option documented but not implemented - or implemented and
    renamed - fails the suite instead of misleading whoever copies the file."""
    import tomllib
    from pathlib import Path

    from amphour.config import from_dict

    raw = tomllib.loads((Path(__file__).parent.parent / "config.example.toml").read_text())
    config = from_dict(raw)
    assert config.devices, "the example should configure at least one device"
    assert {d.driver for d in config.devices} <= set(_available_drivers())


def _available_drivers():
    from amphour import drivers

    return drivers.available()
