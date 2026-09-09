# Before making this repository public

This repo starts private. Everything below needs a decision before the
visibility flips.

## 0. The fixtures profile a real power system  [BIGGEST ITEM]

This was missed on the first pass and is more consequential than the address
question below.

The fixture values are real readings from a real installation, and together
they characterise it: lifetime generation, total operating days, cumulative
charge throughput, lifetime cycle and over-discharge counts, the daily peak
charging power, and the battery's voltage band. Lifetime generation divided by
operating days gives daily yield; peak charging power implies array size.

That is a power-budget profile of a specific site, in a repo intended to be
public.

Tracked files are otherwise clean — no hostnames, IPs, ports or internal names
appear anywhere (verified by grep), the example config uses placeholders, and
the `off-grid` keyword has been removed from `pyproject.toml`. The README still
quotes specific readings as worked examples of the confidence method.

The tension is real: the fixtures are simultaneously the most valuable thing in
the test suite and the disclosure.

Options, roughly in order of how much they cost:

1. **Scrub only the cumulative registers** — lifetime totals, operating days,
   cycle counts — to synthetic values and recompute the CRC. Keeps every parser
   property under test (framing, CRC, offsets, scaling) and keeps the
   cross-check on the instantaneous fields. Loses the cross-check on the four
   cumulative fields. This is the recommended option.
2. Offset every value by a constant and recompute CRCs. Keeps relationships,
   breaks the cross-check entirely.
3. Publish as-is and accept the disclosure.
4. Keep the repo private.

**Decision needed before publication.**

## 1. The raw BLE capture carries hardware addresses

`tests/fixtures/night-2026-09-09.btsnoop` and `day-2026-09-09.btsnoop` are the
unmodified HCI captures the fixtures were derived from. They contain:

- the capturing host's Bluetooth adapter address
- the BT-1 module's address and its advertised name

These are not credentials, and a BLE address is only observable by someone
already within radio range — but they are hardware identifiers tied to a
physical location.

The derived `.json` fixtures are what the test suite actually uses. **They
contain no addresses.** The tests pass without the `.btsnoop` files.

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
