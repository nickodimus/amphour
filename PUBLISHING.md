# Before making this repository public

This repo starts private. Everything below needs a decision before the
visibility flips.

## 1. The raw BLE capture carries hardware addresses

`tests/fixtures/night-2026-09-09.btsnoop` is the unmodified HCI capture the
fixtures were derived from. It contains:

- the capturing host's Bluetooth adapter address
- the BT-1 module's address and its advertised name

These are not credentials, and a BLE address is only observable by someone
already within radio range — but they are hardware identifiers tied to a
physical location.

`tests/fixtures/night-2026-09-09.json` is the derived fixture the test suite
actually uses. **It contains no addresses.** The tests pass without the
`.btsnoop` file.

Options:
- keep it (provenance for how the fixtures were produced), or
- drop it from the repo and keep it locally, or
- scrub the addresses and keep the frames.

**Decision needed. Nothing else in the tracked tree carries site detail** —
`config.example.toml` and `README.md` use placeholders.

## 2. Check the daylight fixture when it is added

The same applies to any future capture. Derive the JSON, decide separately
about the raw file.

## 3. Confirm the licence

`pyproject.toml` and `README.md` say MIT. The original `solar-bt-monitor` by
Scott Nichol and `renogy-bt1` by Cyril Sebastian were the sources for the
protocol understanding, and their code said "feel free to reuse this code for
any purpose". No code was copied — the register offsets were re-derived from a
live capture — but an acknowledgement in the README is the decent thing and
costs nothing.

## 4. Re-read the register map's confidence claims

`README.md` states which fields are confirmed, probable and unverified. If the
daylight capture changes any of those, the README must change with it.
