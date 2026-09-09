"""Transport abstraction: how request bytes reach a device and reply bytes return.

Splitting this out from the protocol is what lets the poll loop be tested
without a radio - see ReplayTransport in tests. The BLE implementation lives in
devices/renogy_bt1.py.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TransportError(Exception):
    """Transport could not complete a request. Retryable."""


class TransportTimeout(TransportError):
    """No complete reply arrived within the deadline."""


@runtime_checkable
class Transport(Protocol):
    """Minimal request/response channel to one device."""

    async def connect(self) -> None: ...

    async def request(self, payload: bytes, expected_len: int) -> bytes:
        """Send `payload`, return exactly `expected_len` bytes of reply."""
        ...

    async def disconnect(self) -> None: ...

    @property
    def is_connected(self) -> bool: ...
