"""Field definitions shared by every driver.

A driver declares what it can report as a tuple of Field. Decoding, Prometheus
metric creation and InfluxDB field selection are all generated from that one
declaration, so a driver cannot have a field it decodes but does not export, or
export one it never decodes.

**Field names are a shared vocabulary, not per-driver private names.** Two
drivers reporting the same physical quantity MUST use the same name -
`battery_voltage` is `battery_voltage` whether it came from a Renogy charge
controller or a Victron shunt. Metrics carry a `device` label to tell the
sources apart, which means one query compares them. That is how a disagreement
between two independent measurements of the same thing becomes visible instead
of being split across two metric names that never meet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Confidence = Literal["confirmed", "probable", "unverified"]


@dataclass(frozen=True, slots=True)
class Field:
    """One value a driver can report.

    confidence records how well established the mapping is, because parts of
    these protocols were reverse engineered and being honest about which parts
    are guesses is part of the data:

      confirmed   decoded from real captured data AND cross-checked against an
                  independent source that reported the same value
      probable    not independently cross-checked, but triangulates against a
                  confirmed field in a physically coherent way
      unverified  consistent with the published layout and with captured bytes,
                  but nothing yet rules out another reading

    Only confirmed and probable are exported by default. See the drivers for
    worked examples of a hypothesis being promoted and one being disconfirmed.
    """

    name: str
    unit: str
    confidence: Confidence
    help: str


def exported(fields: tuple[Field, ...], *, include_unverified: bool = False) -> set[str]:
    """Which field names a sink should publish."""
    if include_unverified:
        return {f.name for f in fields}
    return {f.name for f in fields if f.confidence in ("confirmed", "probable")}
