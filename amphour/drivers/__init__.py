"""Device drivers, and the registry that maps a config `driver = "..."` to one.

In-repo drivers behind a common interface, chosen by name in config. There is
deliberately no entry-point discovery or dynamic third-party loading: that
machinery earns its keep when strangers write drivers you do not control, and
costs complexity for nothing when every driver lives in this tree.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..device import Device
from ..fields import Field

_BUILDERS: dict[str, Callable[..., Device]] = {}
_FIELDS: dict[str, tuple[Field, ...]] = {}


def register(
    name: str, fields: tuple[Field, ...]
) -> Callable[[Callable[..., Device]], Callable[..., Device]]:
    """Register a driver builder AND what that driver can report.

    The fields are registered here rather than looked up by the caller so that
    there is exactly one place mapping a driver name to its field table. cli.py
    used to keep its own driver -> module dict for --list-fields; a third
    driver would have gone missing from it silently, which is the parallel-list
    failure registers.py opens by warning about.
    """

    def decorate(builder: Callable[..., Device]) -> Callable[..., Device]:
        if name in _BUILDERS:
            raise ValueError(f"driver {name!r} is already registered")
        _BUILDERS[name] = builder
        _FIELDS[name] = fields
        return builder

    return decorate


def fields_for(driver: str) -> tuple[Field, ...]:
    """What `driver` can report. Raises KeyError naming the alternatives."""
    _load_all()
    try:
        return _FIELDS[driver]
    except KeyError:
        raise KeyError(
            f"unknown driver {driver!r}; available: {', '.join(sorted(_BUILDERS))}"
        ) from None


def available() -> tuple[str, ...]:
    _load_all()
    return tuple(sorted(_BUILDERS))


def build(driver: str, **kwargs: Any) -> Device:
    _load_all()
    try:
        builder = _BUILDERS[driver]
    except KeyError:
        raise KeyError(
            f"unknown driver {driver!r}; available: {', '.join(sorted(_BUILDERS))}"
        ) from None
    return builder(**kwargs)


def _load_all() -> None:
    # Imported for their registration side effects. Kept in one place so a new
    # driver is added by editing exactly this list plus its own module.
    from . import eg4_lifepower4, renogy, victron_ble, victron_vedirect  # noqa: F401
