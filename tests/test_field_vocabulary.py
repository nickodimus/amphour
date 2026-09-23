"""The shared field vocabulary must actually be shared.

amphour/fields.py states the rule: two drivers reporting the same physical
quantity MUST use the same field name. The corollary is that they must also
agree on its UNIT - one Prometheus metric cannot document two units, and the
sink can only warn about it at runtime, on a machine nobody is watching.

That is not hypothetical. On 2026-09-22 the EG4 driver declared
`battery_current` in "amps" while the VE.Direct driver declared it in
"amperes", and the warning went out on every service start for hours. This
test is the check that would have caught it before it shipped.
"""

from __future__ import annotations

import collections

from amphour import drivers


def test_no_two_drivers_declare_one_field_with_different_units():
    units: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for driver in drivers.available():
        for spec in drivers.fields_for(driver):
            units[spec.name][driver] = spec.unit

    conflicts = {
        name: declarations
        for name, declarations in units.items()
        if len(set(declarations.values())) > 1
    }
    assert not conflicts, (
        "the same field name is declared with different units by different "
        f"drivers, which one metric cannot carry: {conflicts}"
    )


def test_every_driver_declares_at_least_one_field():
    for driver in drivers.available():
        assert drivers.fields_for(driver), f"{driver} declares no fields"
