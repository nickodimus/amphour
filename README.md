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

18 fields are `confirmed`, 2 `probable`, 11 `unverified`.

**Only `confirmed` and `probable` fields are exported by default.** Set
`include_unverified = true` if you want the rest — but do not build an alarm on
a hypothesis without checking it first.

Two captures anchor this so far: one at night with the array dark, one the
next morning with it producing. The second one both **promoted and demoted**
hypotheses, which is the point of recording confidence at all.

**Promoted — `0x010B`/`0x010C`, today's minimum and maximum battery voltage.**
Night showed 11.3 / 11.8 V bracketing a live 11.5 V. The next morning the pair
had reset to that day's own figures, 9.8 / 12.5 V, still bracketing the live
reading. Resetting overnight is exactly what a daily min/max must do, and the
bracket has held on every frame of both captures. Still `probable` rather than
`confirmed`, because `confirmed` here means cross-checked against another
implementation's output and the predecessor never reported these fields.

**Demoted — `0x010D`, which the night capture made look like today's peak
charging current.** 802 W of confirmed peak charging power divided by 56.53 A
gave 14.19 V, a textbook absorption voltage. It was a real relationship
producing a correctly-shaped number, and it was wrong. The morning capture
disconfirmed it three ways at once: the value did not reset overnight
(5653 → 5616) while `charging_power_max_today` plainly did (802 → 691 W); a
"peak today" of 56.16 A is inconsistent with the 22.76 A actually observed that
morning; and the voltage ratio stopped landing anywhere sensible (12.30 V). It
is now an unnamed raw register, with that history recorded in its `help` text
so nobody re-derives the same wrong answer.

**`0x0121` sits immediately after the charging-state register**, and the
obvious guess was fault/alarm bits. Across 30 frames it instead tracks charging
state exactly — state `0` → `4` on 11 of 11 frames, state `2` → `1` on 19 of 19,
no exceptions. That is a companion status word, not an independent fault
register. Only two charging states have been observed, so this is suggestive
rather than settled. Exported raw, deliberately not decoded into named faults.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

The suite needs no hardware. It runs against **real frames captured off the
air** from a live BT-TH module.

Each fixture entry holds two things that come from **different programs**:

- `frame_hex` — the raw bytes the device put on the air, from a `btmon` capture
- `old_app_log` — what the *previous* implementation reported for that same
  instant, read straight out of its own log file

`tools/extract_fixtures.py` builds these and deliberately **does not decode
anything**, so the fixture cannot end up carrying the extractor's opinion of
what the bytes mean. The test decodes `frame_hex` and asserts it matches
`old_app_log`. Because the two sides come from different programs, that is a
real cross-check rather than a decoder agreeing with itself.

`tests/conftest.py` refuses to load a fixture that is not in this format, so a
future well-meaning change to the extractor cannot quietly make the comparison
circular again.

The poll loop is tested too, through a replay transport that can be scripted to
fail — connection failures, mid-run dropouts, corrupt frames, exploding sinks.

### What the fixtures do and do not cover

Two captures, 30 frames, 24 of them unique:

| | frames | conditions |
|---|---|---|
| `night-2026-09-09` | 11 | array dark; every PV, load and discharge field a legitimate zero |
| `day-2026-09-09` | 19 | overcast with variable cloud, array producing 180–270 W, charging in `mppt` |

Together they give **540 field comparisons against the previous
implementation, with no mismatches.**

The daylight capture closed the gap that mattered: PV voltage, PV current, PV
power, battery charging current and a non-zero `charging_state` were all
structurally zero at night and so completely unexercised. `test_reading.py`
asserts that coverage explicitly, so losing or replacing the daylight fixture
with dark data fails the suite rather than quietly halving what it tests.

Still not covered, and honestly:

- **The top of the range.** The daylight capture was taken under overcast, not
  at peak output. Scaling is linear and the largest raw value in play is
  nowhere near a `u16` ceiling, so this is not a correctness risk — but nothing
  here has seen a bright day.
- **Everything load-side.** `load_voltage`, `load_current`, `load_power` and
  every discharge counter read zero in both captures, because the controller
  under test has nothing wired to its load terminals. Those offsets are taken
  from the register layout and have never returned a non-zero value.
- **Sub-zero temperatures.** The sign-bit convention is implemented from
  documentation; no capture has been below freezing.

## Licence

MIT.
