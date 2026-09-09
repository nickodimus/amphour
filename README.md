# amphour

A monitor for DC power-system gear. It polls a device, decodes its registers,
and exports the readings to Prometheus and optionally InfluxDB.

Today it speaks to one device: the **Renogy BT-1 / BT-TH** Bluetooth module
that fronts Renogy charge controllers. The internals are arranged so a second
driver — a Victron VE.Direct shunt, say — drops in beside it without
restructuring.

Runs on Python 3.11+ with `bleak`. No vendored libraries, no dead dependencies.

---

## Why this exists

It replaces a working but ageing script built on `gatt` (unmaintained upstream),
`libscrc`, a vendored logging module and `gpiozero` pulled in to read one
temperature. That stack pinned its host to Python 3.9, and Python 3.9 pinned the
host to an obsolete OS.

Three behaviours changed on purpose, because they were wrong rather than merely
old:

**A truncated frame is an error, not a reading of zero.** The predecessor's
integer reader returned `0` when asked to read past the end of a short buffer,
so a partial frame produced a complete-looking set of zeros. `amphour` raises.

**The device's checksum is actually checked.** Every frame carries a
CRC16/MODBUS. The predecessor computed one for outbound requests and never
verified the inbound one, so a corrupted frame was parsed as truth.

**The wait moved out of the callback.** The predecessor called `time.sleep(30)`
inside the BLE notification handler, blocking the GATT event loop for the whole
interval. Here the interval is an `await` in the poll loop and the callback only
hands over bytes.

---

## Install

```bash
python3 -m venv /opt/amphour
/opt/amphour/bin/pip install git+https://github.com/nickodimus/amphour
sudo install -d /etc/amphour
sudo install -m 640 config.example.toml /etc/amphour/config.toml
sudoedit /etc/amphour/config.toml
```

Then the service:

```bash
sudo install -m 644 amphour.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now amphour
```

BLE access goes through BlueZ over D-Bus, so the service needs no elevated
privileges. It requires **BlueZ 5.55 or newer** (bleak's own floor).

## Use

```bash
amphour                          # run against /etc/amphour/config.toml
amphour --config ./config.toml   # somewhere else
amphour --once                   # one reading, printed, then exit
amphour --list-fields            # the register map and its confidence levels
amphour --decode ff0344...       # decode a captured frame, no hardware needed
```

`--once` is the commissioning check: it proves the address, the adapter and the
device all work before anything is installed as a service.

## Configuration

See [`config.example.toml`](config.example.toml). The essentials:

```toml
[device]
address = "AA:BB:CC:DD:EE:FF"

[prometheus]
enabled = true
port = 5000

[influxdb]
enabled = false
```

Prometheus is on by default; InfluxDB writes nothing and opens no connection
unless enabled. Credentials may be supplied by `AMPHOUR_INFLUX_USERNAME` and
`AMPHOUR_INFLUX_PASSWORD` instead of being written into the file, and the
environment wins when both are present.

An unrecognised option is an error rather than a silently ignored default — a
typo that gets dropped is how a setting appears to be applied without being
applied.

### Migrating from `solar-bt-monitor`

Set `metric_prefix = "solarmon_"` and existing dashboards keep working through
the cutover. Field names differ (`battery_percentage` became
`battery_state_of_charge`, `max_charging_power_today` became
`charging_power_max_today`), so a dashboard referencing those needs updating;
`amphour --list-fields` prints the current set.

---

## The register map, and how much of it to trust

The device answers a single Modbus read of 34 holding registers from `0x0100`.
This map was reverse engineered, so every field records how well established it
is:

| confidence | meaning |
|---|---|
| `confirmed` | decoded from a real captured frame **and** cross-checked against the value the previous implementation independently reported for the same instant |
| `probable` | not independently cross-checked, but triangulates against a confirmed field in a physically coherent way |
| `unverified` | consistent with the published Renogy layout and with the captured bytes, but nothing yet rules out another reading |

18 fields are `confirmed`, 3 `probable`, 10 `unverified`.

**Only `confirmed` and `probable` fields are exported by default.** Set
`include_unverified = true` if you want the rest — but do not build an alarm on
a hypothesis without checking it first.

Two examples of what `probable` means in practice. `0x010B`/`0x010C` read 11.3
and 11.8 V against a live reading of 11.5 V, which is what today's minimum and
maximum should look like. `0x010D` reads 56.53 A, and the independently
confirmed peak charging power of 802 W divided by it gives 14.19 V — a textbook
absorption voltage. Neither is proof; both are better than a guess.

`0x0121` sits immediately after the charging-state register and read `0x0004`
throughout the capture. It is very likely fault/alarm bits, but there is no
verified bit map, so it is exported raw and deliberately **not** decoded into
named faults.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

The suite needs no hardware. It runs against **real frames captured off the
air** from a live BT-TH-F265000C, stored in `tests/fixtures/` alongside the
values the previous implementation produced from those same frames at those
same instants. Agreement across all mapped fields is the strongest check in the
suite: it means this rewrite reproduces what was actually running in
production, not merely that it is self-consistent.

The poll loop is tested too, through a replay transport that can be scripted to
fail — connection failures, mid-run dropouts, corrupt frames, exploding sinks.

### A known gap in the fixtures

The capture was taken at night, with the array dark and the controller's load
terminals unused. Every PV, load and discharge field legitimately reads zero.
That is coherent, but it means **those code paths are unexercised**: the fixture
set proves the parser handles a dark system and proves nothing about a charging
one, which is where a scaling or offset error would actually show.

A daylight capture is needed before the PV and charging-state paths can honestly
be called confirmed. `tests/conftest.py` already has the hook for it, and tests
that need it skip rather than pass while it is absent.

## Licence

MIT.
