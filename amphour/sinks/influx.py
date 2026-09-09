"""InfluxDB 1.x sink: line protocol over HTTP, no client library.

Deliberately dependency-light - see amphour/line_protocol.py for why neither
InfluxData client is a good choice for a 1.x server in 2026.

Writes are buffered and flushed on a point count or a deadline, whichever comes
first. `requests` is synchronous, so the actual POST runs in a worker thread to
keep it off the event loop.

This sink is inert unless configured. A failure to reach InfluxDB is logged and
the buffer is dropped rather than growing without bound - metrics are better
lost than allowed to exhaust memory on a 1 GB board.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..line_protocol import to_line
from ..reading import Reading
from ..registers import DEFAULT_EXPORTED, FIELDS

log = logging.getLogger(__name__)


class InfluxSink:
    name = "influxdb"

    def __init__(
        self,
        *,
        url: str,
        database: str,
        measurement: str = "amphour",
        username: str | None = None,
        password: str | None = None,
        tags: dict[str, str] | None = None,
        include_unverified: bool = False,
        batch_size: int = 60,
        flush_interval: float = 60.0,
        timeout: float = 10.0,
        retention_policy: str | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.database = database
        self.measurement = measurement
        self.tags = tags or {}
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.timeout = timeout
        self.retention_policy = retention_policy
        self._exported = {f.name for f in FIELDS} if include_unverified else set(DEFAULT_EXPORTED)
        self._buffer: list[str] = []
        self._last_flush = 0.0
        self._lock = asyncio.Lock()

        self._session = requests.Session()
        if username is not None and password is not None:
            # Basic auth, not the u=/p= query parameters: InfluxData's own docs
            # warn that query-string credentials land in server logs.
            self._session.auth = (username, password)
        self._session.mount(
            "http://",
            HTTPAdapter(
                max_retries=Retry(
                    total=2,
                    backoff_factor=0.5,
                    status_forcelist=(500, 502, 503, 504),
                    allowed_methods=frozenset(["POST"]),
                )
            ),
        )

    def _params(self) -> dict[str, str]:
        params = {"db": self.database, "precision": "s"}
        if self.retention_policy:
            params["rp"] = self.retention_policy
        return params

    async def publish(self, reading: Reading) -> None:
        fields: dict[str, Any] = {
            name: value for name, value in reading.values.items() if name in self._exported
        }
        if not fields:
            return
        line = to_line(self.measurement, fields, self.tags, reading.taken_at, precision="s")
        async with self._lock:
            self._buffer.append(line)
            now = asyncio.get_running_loop().time()
            if self._last_flush == 0.0:
                self._last_flush = now
            due = (
                len(self._buffer) >= self.batch_size
                or now - self._last_flush >= self.flush_interval
            )
            if due:
                await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._buffer:
            return
        payload = "\n".join(self._buffer).encode()
        count = len(self._buffer)
        self._buffer.clear()
        self._last_flush = asyncio.get_running_loop().time()
        try:
            await asyncio.to_thread(self._post, payload)
            log.debug("wrote %d points to influxdb", count)
        except requests.RequestException as exc:
            log.warning("influxdb write of %d points failed, dropped: %s", count, exc)

    def _post(self, payload: bytes) -> None:
        response = self._session.post(
            f"{self.url}/write",
            params=self._params(),
            data=payload,
            timeout=self.timeout,
        )
        if response.status_code != 204:
            raise requests.RequestException(
                f"influxdb returned {response.status_code}: {response.text[:200]}"
            )

    async def aclose(self) -> None:
        async with self._lock:
            await self._flush_locked()
        self._session.close()
