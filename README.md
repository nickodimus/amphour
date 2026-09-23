# amphour

A monitor for DC power-system gear. It reads **Renogy** charge controllers over
Bluetooth (BT-1 / BT-TH, Modbus RTU over a BLE characteristic, via bleak) and
**Victron** devices over VE.Direct (SmartShunt, SmartSolar MPPT, and anything
else with a VE.Direct port), and **Mopeka** propane tank pucks over BLE
advertisements, decodes them, and exports the readings to **Prometheus** and
optionally **InfluxDB**.

It is a modern, maintained replacement for the ageing `solar-bt-monitor` /
renogy-bt lineage, which is built on the unmaintained `gatt` Python library —
amphour talks BLE through **bleak** instead. See [Why this exists](#why-this-exists)
for the specific bugs it fixes.

Runs on Python 3.11+. No vendored libraries, no dead dependencies. Currently
running in production against a Victron SmartShunt and a Renogy BT-TH.

## Drivers

| driver | speaks to | how |
|---|---|---|
| `renogy_bt1` | Renogy BT-1 / BT-TH module fronting a Renogy charge controller | Modbus RTU over a BLE characteristic (via bleak), polled |
| `victron_vedirect` | Victron SmartShunt, SmartSolar MPPT, and anything else with a VE.Direct port | plain-text serial, streamed |
| `mopeka_ble` | Mopeka Pro Check tank-level puck (propane) | BLE advertisements, listened for — never connected to |

Drivers are in-repo and chosen by name in config. There is deliberately no
entry-point discovery or dynamic third-party loading: that machinery earns its
keep when strangers write drivers you do not control, and costs complexity for
nothing when every driver lives in this tree.

Each device runs as its own task, so one failing and retrying never stalls
another — a BLE module that has wandered off must not hold up a serial shunt
that is working perfectly.

### Listening, not connecting

The Mopeka driver never opens a BLE connection. The puck broadcasts its reading
in the advertisement itself, so amphour only listens — which means it cannot
lock the device out, and staleness shows up directly as silence.

That also means every Mopeka puck in radio range is talking at once, under one
manufacturer id. **`address` is a filter, not a convenience.** A puck retired
with a dead ultrasonic transducer keeps advertising a perfectly well formed
reading of zero, and may well be louder at the collector than the tank you care
about. Matching on manufacturer id alone would quietly monitor the wrong tank.

A reading whose signal quality is below `min_quality` publishes **no level at
all** — not a zero, not a NaN. Temperature, battery and quality keep publishing,
so a puck that cannot see the liquid surface stays visibly alive rather than
drawing an empty tank. (NaN in particular is load-bearing: InfluxDB rejects a
NaN point, and a rejected point can sit in a write buffer and poison later
writes.)

One more thing that is invisible from the code: **a puck this collector cannot
hear is not a puck that has moved on.** Where two collectors each cover pucks
the other cannot reach, that is radio geography, and the split is deliberate
rather than a migration half-finished. If you find one, look for the note
explaining it before tidying it away.

## One vocabulary across drivers

**Two drivers reporting the same physical quantity use the same field name**,
and every metric carries a `device` label. So they land on one metric with two
label values:

```
amphour_battery_state_of_charge{device="renogy"}      9.0
amphour_battery_state_of_charge{device="smartshunt"} 99.7
amphour_battery_voltage{device="renogy"}             11.4
amphour_battery_voltage{device="smartshunt"}         11.128
```

That is real output from this repo's fixtures, and it is the point of the
convention. Two independent measurements of one battery disagreeing by ninety
points is a thing you want to see in one query — not something split across two
metric names that never meet. The voltages agreeing to within 0.27 V in the
same breath cross-validates both readings.

The convention cuts the other way too. A shunt's `battery_current` is net
current into and out of the bank, signed; a charge controller's
`battery_charging_current` is only what that controller is delivering. Same
units, different quantities, so deliberately different names.

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
inside the BLE notification handler, blocking the `gatt` library's event loop for the whole
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
amphour                              # run against /etc/amphour/config.toml
amphour --config ./config.toml       # somewhere else
amphour --once                       # one reading from each device, then exit
amphour --list-drivers               # what drivers exist
amphour --list-fields renogy_bt1     # a driver's fields and confidence levels
amphour --decode ff0344...           # decode a captured Renogy frame offline
```

`--once` is the commissioning check: it proves every configured device is
reachable and decoding before anything is installed as a service. It works for
polling and streaming drivers alike — each device runs normally and is stopped
as soon as it has produced one reading.

## Configuration

See [`config.example.toml`](config.example.toml). The essentials:

```toml
[[device]]                       # note the DOUBLE brackets
name = "smartshunt"              # becomes the `device` label; must be unique
driver = "victron_vedirect"
port = "/dev/ttyUSB0"

[[device]]
name = "renogy"
driver = "renogy_bt1"
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

## The Renogy register map, and how much of it to trust

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

Everything is captured from live hardware. Nothing is synthesised.

| fixture | contents | conditions |
|---|---|---|
| `night-2026-09-09` | 11 Renogy frames | array dark; every PV, load and discharge field a legitimate zero |
| `day-2026-09-09` | 19 Renogy frames | overcast with variable cloud, array producing 180–270 W, charging in `mppt` |
| `vedirect-smartshunt-2026-09-09` | 12 VE.Direct blocks | a live SmartShunt 500A/50mV, both block types |
| `vedirect-corrupt-sample` | one corrupt fragment | from a genuinely corrupt region of the same capture |

The Renogy fixtures give **540 field comparisons against the previous
implementation, with no mismatches.** The VE.Direct field meanings were each
cross-checked against the `description` and `units` an independent
implementation recorded for the same labels on the same physical device.

The daylight capture closed the gap that mattered: PV voltage, PV current, PV
power, battery charging current and a non-zero `charging_state` were all
structurally zero at night and so completely unexercised. `test_reading.py`
asserts that coverage explicitly, so losing or replacing the daylight fixture
with dark data fails the suite rather than quietly halving what it tests.

Still not covered by the automated suite, and honestly:

- **The tests never touch a live transport — production now does.** The protocol
  layers are exercised against real captured bytes; the serial and BLE transports
  are not covered by the suite. In the field, though, both drivers now read live
  hardware continuously — a Victron SmartShunt over serial and a Renogy BT-TH
  over BLE, feeding Prometheus and InfluxDB. The BLE connect needed a retry loop
  to ride out BlueZ returning HCI `0x3e` ("connection failed to be established")
  when the module's narrow advertising window is missed — common on a Raspberry
  Pi's built-in Bluetooth radio.
- **The top of the range.** The daylight capture was taken under overcast, not
  at peak output. Scaling is linear and the largest raw value in play is nowhere
  near a `u16` ceiling, so this is not a correctness risk — but nothing here has
  seen a bright day.
- **Everything load-side.** `load_voltage`, `load_current`, `load_power` and
  every discharge counter read zero in both Renogy captures, because that
  controller has nothing wired to its load terminals.
- **Sub-zero temperatures.** The sign-bit convention is implemented from
  documentation; no capture has been below freezing.
- **The VE.Direct capture is a SmartShunt only.** An MPPT controller emits a
  different label set over the identical protocol; those labels are declared but
  have not been seen on the wire here.

## Licence

MIT.
