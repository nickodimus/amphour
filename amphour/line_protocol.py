"""InfluxDB line protocol serialisation.

This exists because both InfluxDB Python clients are dead ends for a 1.x
server: the legacy `influxdb` package is archived, and the maintained v2
client's own README tells 1.x users to go back to the archived one. The write
path itself is one POST of newline-separated points, so the only part worth
owning carefully is the escaping - which is exactly where hand-rolled writers
break. Hence a small module with its own tests.

Escaping rules, from the InfluxDB 1.x line protocol reference:

  measurement          escape , and space           (NOT =)
  tag key / tag value  escape , = and space
  field key            escape , = and space
  string field value   wrap in "", escape " and \\
  integer field        decimal digits with an i suffix
  float field          plain decimal
  boolean field        t / f

Field types must stay consistent per field across writes or the server rejects
the point with a 400.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

FieldValue = float | int | bool | str


def _escape_measurement(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _escape_key(value: str) -> str:
    """Tag keys, tag values and field keys share one escaping rule."""
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _format_value(value: FieldValue) -> str:
    # bool before int: bool is a subclass of int and would otherwise
    # serialise as 1i/0i rather than t/f.
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return repr(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def to_line(
    measurement: str,
    fields: Mapping[str, FieldValue],
    tags: Mapping[str, str] | None = None,
    timestamp: datetime | None = None,
    *,
    precision: str = "s",
) -> str:
    """Serialise one point. Raises ValueError if there are no fields.

    `precision` must match the `precision` query parameter used on the write,
    or the server will place the point at the wrong time.
    """
    if not fields:
        raise ValueError("a point needs at least one field")

    key = _escape_measurement(measurement)
    if tags:
        # Influx documents that sorting tags by key improves write performance.
        rendered = ",".join(
            f"{_escape_key(k)}={_escape_key(v)}" for k, v in sorted(tags.items()) if v != ""
        )
        if rendered:
            key = f"{key},{rendered}"

    body = ",".join(f"{_escape_key(k)}={_format_value(v)}" for k, v in fields.items())

    if timestamp is None:
        return f"{key} {body}"

    epoch = timestamp.timestamp()
    scale = {"s": 1, "ms": 1e3, "u": 1e6, "n": 1e9}
    if precision not in scale:
        raise ValueError(f"unsupported precision {precision!r}")
    return f"{key} {body} {int(epoch * scale[precision])}"
