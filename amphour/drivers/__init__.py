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

_BUILDERS: dict[str, Callable[..., Device]] = {}


def register(name: str) -> Callable[[Callable[..., Device]], Callable[..., Device]]:
    def decorate(builder: Callable[..., Device]) -> Callable[..., Device]:
        if name in _BUILDERS:
            raise ValueError(f"driver {name!r} is already registered")
        _BUILDERS[name] = builder
        return builder

    return decorate


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
    from . import renogy, victron_vedirect  # noqa: F401
