from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..reading import Reading


@runtime_checkable
class Sink(Protocol):
    """Somewhere a Reading can be published.

    publish() must not raise for transient problems - a sink that cannot
    reach its destination logs and drops, so one bad sink never stops the
    poll loop or another sink.
    """

    name: str

    async def publish(self, reading: Reading) -> None: ...

    async def aclose(self) -> None: ...
